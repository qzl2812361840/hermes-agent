"""通用持久任务队列（task_queue，C1 吸收自 dsh-superagent Effects 设计）

设计要点（源码依据：opensource-study/dsh-superagent/src/app/effects.ts）：
- SQLite 单表持久，进程重启队列不丢；
- kind+key 合并（upsert 同键保留原队列位置，更新载荷并 generation+1，
  防止在跑的旧代把新载荷删掉）；
- 指数退避重试：delay = min(max_retry, base * 2**attempts)；
- 连续 FAILURES_BEFORE_ERROR(3) 次失败该 kind 标记 error（不阻塞其他 kind），
  下一次成功自动恢复健康；
- 每 kind 并发上限（ordered=True 时严格串行 1）；
- 30 天自动清理完成行，防无限膨胀；
- 纯标准库，线程安全（锁内所有读写）。

本模块只做队列本体，不绑定任何业务；im_relay 离线消息补推后续接入。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "TaskQueue",
    "TaskStatus",
    "QueueConfig",
]

#: 连续失败多少次后标记 kind 为 error（对齐 dsh FAILURES_BEFORE_ERROR=3）
FAILURES_BEFORE_ERROR = 3

#: 完成/错误行保留天数，超过即清理
RETENTION_DAYS = 30

#: 默认退避参数（秒）：首次 2s，封顶 60s
DEFAULT_RETRY_BASE = 2.0
DEFAULT_RETRY_MAX = 60.0


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class QueueConfig:
    """队列行为配置。"""

    retry_base: float = DEFAULT_RETRY_BASE
    retry_max: float = DEFAULT_RETRY_MAX
    max_attempts_before_failed: int = 8  # 连续 8 次失败后落成 failed（不再自动重试）
    retention_days: int = RETENTION_DAYS


@dataclass
class HealthEntry:
    """一个 kind 的健康状态。"""

    status: str = "ok"  # ok / error
    detail: str = ""


class TaskQueue:
    """SQLite 持久任务队列（单进程多线程安全）。

    用法::

        q = TaskQueue("/path/queue.db")
        q.register("reply", handler, limit=2)
        q.enqueue("reply", "msg-1", {"text": "hi"})
        q.due("reply")   # → 行列表（due 且未超并发）
        q.complete(kind, key)  / q.fail(kind, key)
    """

    def __init__(self, path: str, config: Optional[QueueConfig] = None,
                 now: Optional[Callable[[], float]] = None):
        self._config = config or QueueConfig()
        self._now = now or time.time
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS task_queue (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                seq INTEGER NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                due REAL NOT NULL,
                since REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (kind, key)
            )"""
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_queue_kind_status_due "
            "ON task_queue(kind, status, due)"
        )
        self._db.commit()
        self._handlers: Dict[str, Dict[str, Any]] = {}
        self._running: Dict[str, set] = {}
        self._health: Dict[str, HealthEntry] = {}

    # ------------------------------------------------------------------
    # 注册与入队
    # ------------------------------------------------------------------

    def register(self, kind: str, handler: Callable[[str, dict], Any], *,
                 limit: int = 1, ordered: bool = False) -> None:
        """注册 kind 的处理器；ordered=True 时该 kind 并发恒为 1（保序）。

        handler(key, payload) 返回任意值；抛异常 = 本次执行失败。
        """
        with self._lock:
            self._handlers[kind] = {
                "handler": handler,
                "limit": 1 if ordered else max(1, int(limit)),
                "ordered": bool(ordered),
            }
            self._running.setdefault(kind, set())
            self._health.setdefault(kind, HealthEntry())

    def enqueue(self, kind: str, key: str, payload: Optional[dict] = None, *,
                delay: float = 0.0) -> bool:
        """入队/合并。同 (kind, key) 已在等待时：更新载荷、保留原位置、
        generation+1、清空 attempts。返回 True 表示新建，False 表示合并。"""
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        now = self._now()
        with self._lock:
            row = self._db.execute(
                "SELECT status, generation FROM task_queue WHERE kind=? AND key=?",
                (kind, key),
            ).fetchone()
            if row is not None and row[0] in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value):
                self._db.execute(
                    "UPDATE task_queue SET value=?, generation=generation+1, attempts=0, "
                    "status=?, due=?, updated_at=? WHERE kind=? AND key=?",
                    (payload_json, TaskStatus.PENDING.value, now + delay, now, kind, key),
                )
                self._db.commit()
                return False
            seq = self._next_seq()
            self._db.execute(
                "INSERT INTO task_queue(kind, key, value, seq, due, since, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (kind, key, payload_json, seq, now + delay, now, now),
            )
            self._db.commit()
            return True

    def _next_seq(self) -> int:
        row = self._db.execute("SELECT IFNULL(MAX(seq),0)+1 FROM task_queue").fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # 取任务 / 汇报结果
    # ------------------------------------------------------------------

    def due(self, kind: str) -> List[Tuple[str, dict, int]]:
        """返回该 kind 当前可执行的 (key, payload, generation) 列表。

        规则（对齐 dsh dispatch）：
        - 仅 pending 且 due 到点；
        - 同 key 已在跑的跳过；
        - ordered=True 且该 kind 有在跑任务 → 返回空（保序，后来的等）；
        - 受 limit 并发上限约束。
        """
        with self._lock:
            handler = self._handlers.get(kind)
            if handler is None:
                return []
            running = self._running.get(kind, set())
            if handler["ordered"] and running:
                return []
            now = self._now()
            rows = self._db.execute(
                "SELECT key, value, generation FROM task_queue "
                "WHERE kind=? AND status=? AND due<=? ORDER BY seq",
                (kind, TaskStatus.PENDING.value, now),
            ).fetchall()
            out: List[Tuple[str, dict, int]] = []
            for key, value, generation in rows:
                if key in running:
                    continue
                if len(out) >= handler["limit"]:
                    break
                out.append((key, json.loads(value), generation))
            return out

    def claim(self, kind: str, key: str, generation: int) -> bool:
        """认领任务：generation 匹配才算数（防旧代覆盖新载荷）。

        认领成功把行置 RUNNING；不匹配返回 False，调用方跳过。
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE task_queue SET status=? WHERE kind=? AND key=? "
                "AND status=? AND generation=?",
                (TaskStatus.RUNNING.value, kind, key, TaskStatus.PENDING.value, generation),
            )
            self._db.commit()
            if cur.rowcount == 1:
                self._running.setdefault(kind, set()).add(key)
                return True
            return False

    def complete(self, kind: str, key: str) -> None:
        """执行成功：删除行（dsh 语义：成功即消失），恢复健康。"""
        with self._lock:
            self._db.execute("DELETE FROM task_queue WHERE kind=? AND key=?", (kind, key))
            self._db.commit()
            self._running.get(kind, set()).discard(key)
            health = self._health.get(kind)
            if health is not None and health.status == "error":
                health.status, health.detail = "ok", ""

    def fail(self, kind: str, key: str, reason: str = "") -> None:
        """执行失败：attempts+1，指数退避后重试；连续 3 次失败标 error；
        超过 max_attempts 落成 failed 终态。不阻塞同 kind 其他 key。"""
        now = self._now()
        with self._lock:
            row = self._db.execute(
                "SELECT attempts, generation, value, since FROM task_queue WHERE kind=? AND key=?",
                (kind, key),
            ).fetchone()
            self._running.get(kind, set()).discard(key)
            if row is None:
                return
            attempts, generation, value, since = row
            attempts += 1
            if attempts >= self._config.max_attempts_before_failed:
                self._db.execute(
                    "UPDATE task_queue SET status=?, attempts=?, updated_at=? WHERE kind=? AND key=?",
                    (TaskStatus.FAILED.value, attempts, now, kind, key),
                )
                self._db.commit()
                return
            delay = self._retry_delay(attempts)
            self._db.execute(
                "UPDATE task_queue SET status=?, attempts=?, due=?, updated_at=? "
                "WHERE kind=? AND key=? AND generation=?",
                (TaskStatus.PENDING.value, attempts, now + delay, now, kind, key, generation),
            )
            self._db.commit()
            health = self._health.get(kind)
            if health is not None and attempts >= FAILURES_BEFORE_ERROR:
                health.status = "error"
                health.detail = f"{kind}/{key} 连续失败 {attempts} 次: {reason}"

    def _retry_delay(self, attempts: int) -> float:
        base, top = self._config.retry_base, self._config.retry_max
        return min(top, base * (2 ** min(attempts, 16)))

    # ------------------------------------------------------------------
    # 查询 / 维护
    # ------------------------------------------------------------------

    def health(self, kind: str) -> HealthEntry:
        with self._lock:
            entry = self._health.get(kind)
            return HealthEntry(entry.status, entry.detail) if entry else HealthEntry("unknown", "未注册")

    def stats(self, kind: Optional[str] = None) -> Dict[str, int]:
        """等待/在跑/失败计数；kind=None 时全队列合计。"""
        with self._lock:
            where, params = "", []
            if kind is not None:
                where, params = "WHERE kind=?", [kind]
            rows = self._db.execute(
                f"SELECT status, COUNT(*) FROM task_queue {where} GROUP BY status",
                params,
            ).fetchall()
            counts = {row[0]: row[1] for row in rows}
            return {
                "pending": counts.get(TaskStatus.PENDING.value, 0),
                "running": counts.get(TaskStatus.RUNNING.value, 0),
                "failed": counts.get(TaskStatus.FAILED.value, 0),
            }

    def failed_rows(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """终态失败行（供人工/自动重放入队）。"""
        with self._lock:
            where, params = "", []
            if kind is not None:
                where, params = "WHERE kind=?", [kind]
            rows = self._db.execute(
                f"SELECT kind, key, value, attempts, since FROM task_queue "
                f"{where + (' AND' if where else 'WHERE')} status=?",
                [*params, TaskStatus.FAILED.value],
            ).fetchall()
            return [
                {"kind": k, "key": key, "payload": json.loads(v), "attempts": a, "since": s}
                for k, key, v, a, s in rows
            ]

    def requeue_failed(self, kind: str, key: str) -> bool:
        """把终态失败行放回待执行（重放入口）。"""
        with self._lock:
            cur = self._db.execute(
                "UPDATE task_queue SET status=?, attempts=0, due=?, generation=generation+1, "
                "updated_at=? WHERE kind=? AND key=? AND status=?",
                (TaskStatus.PENDING.value, self._now(), self._now(), kind, key,
                 TaskStatus.FAILED.value),
            )
            self._db.commit()
            return cur.rowcount == 1

    def purge_expired(self) -> int:
        """清理超过保留期的终态行（成功行已即时删除，此处主要清 failed）。

        防膨胀：每次调用顺手清，30 天前的终态行不留。返回清理条数。
        """
        cutoff = self._now() - self._config.retention_days * 86400
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM task_queue WHERE status IN (?,?) AND updated_at<?",
                (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value, cutoff),
            )
            self._db.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()

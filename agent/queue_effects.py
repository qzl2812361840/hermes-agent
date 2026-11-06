"""通用持久副作用队列（queue_effects）——参考 dsh-superagent-prototype effects.ts 设计（MIT），Python 自行实现。

设计要点（只增不删，缺省零影响）：
  - 所有"不能丢"的异步动作统一入 SQLite 单表，命令事务内 enqueue、提交后 dispatch
  - kind + key 合并去重（ON CONFLICT upsert，generation 递增防覆盖运行中的行）
  - ordered：同类按 seq 保序，前一条失败后一条等待
  - 失败指数退避 min(MAX_DELAY, INITIAL_DELAY * 2^attempts)
  - TransientLater：不增失败计数、按初始延迟重排（暂停/槽满）
  - 连续 FAILURES_BEFORE_ERROR 次失败才把该 kind 标健康错误
  - 处理器抛 UnknownKind 视为未注册，不入失败计数（保留行等待注册）
  - 30 天前的已完成行自动清理（不复制原型的无限增长问题）
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable, Dict, Optional

__all__ = [
    "EffectsQueue",
    "TransientLater",
    "UnknownKind",
    "INITIAL_DELAY_MS",
    "MAX_DELAY_MS",
    "FAILURES_BEFORE_ERROR",
]

INITIAL_DELAY_MS = 15_000
MAX_DELAY_MS = 10 * 60_000
FAILURES_BEFORE_ERROR = 3
DONE_RETENTION_MS = 30 * 24 * 3600 * 1000  # 30 天


class TransientLater(Exception):
    """处理器要求稍后重试（暂停/槽满），不增失败计数。"""


class UnknownKind(Exception):
    """该 kind 未注册处理器；保留行，等待注册后继续。"""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS effect (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    seq INTEGER NOT NULL,
    attempts INTEGER DEFAULT 0,
    due INTEGER NOT NULL,
    generation INTEGER DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_effect_due ON effect (kind, due);
CREATE TABLE IF NOT EXISTS effect_done (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    done_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_effect_done ON effect_done (done_at);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class EffectsQueue:
    """SQLite 持久任务队列：事务内入队，提交后分发。"""

    def __init__(
        self,
        db: sqlite3.Connection,
        handlers: Optional[Dict[str, Callable[[Dict[str, Any]], None]]] = None,
        *,
        now_ms: Callable[[], int] = _now_ms,
    ):
        self.db = db
        self.handlers: Dict[str, Callable[[Dict[str, Any]], None]] = dict(handlers or {})
        self.now_ms = now_ms
        self._health_errors: Dict[str, str] = {}
        self._seq = 0
        db.executescript(_SCHEMA)

    # ── 入队 ──────────────────────────────────────────────
    def enqueue(self, kind: str, key: str, payload: Dict[str, Any]) -> None:
        """kind+key 相同则合并（新 payload 覆盖、attempts 归零），否则新建。"""
        self._seq += 1
        self.db.execute(
            """
            INSERT INTO effect (kind, key, value, seq, due, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(kind, key) DO UPDATE SET
              value = excluded.value,
              generation = generation + 1,
              attempts = 0,
              error = NULL,
              due = excluded.due
            """,
            (kind, key, json.dumps(payload, ensure_ascii=False), self._seq, self.now_ms(), self.now_ms()),
        )

    def register(self, kind: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        self.handlers[kind] = handler

    # ── 分发 ──────────────────────────────────────────────
    def dispatch(self, kind: str, *, limit: int = 1) -> int:
        """处理该 kind 中 due 已到、按 seq 保序的行；返回处理条数。"""
        processed = 0
        while processed < limit:
            row = self.db.execute(
                "SELECT key, value, attempts FROM effect WHERE kind=? AND due<=? ORDER BY seq LIMIT 1",
                (kind, self.now_ms()),
            ).fetchone()
            if row is None:
                break
            key, value, attempts = row[0], row[1], row[2] or 0
            handler = self.handlers.get(kind)
            if handler is None:
                raise UnknownKind(kind)
            try:
                handler(json.loads(value))
            except TransientLater:
                self.db.execute(
                    "UPDATE effect SET due=? WHERE kind=? AND key=?",
                    (self.now_ms() + INITIAL_DELAY_MS, kind, key),
                )
            except Exception as exc:  # 失败退避；3 次连续才标健康错误
                delay = min(MAX_DELAY_MS, INITIAL_DELAY_MS * 2 ** min(attempts, 16))
                new_attempts = attempts + 1
                if new_attempts >= FAILURES_BEFORE_ERROR:
                    self._health_errors[kind] = str(exc)[:1000]
                self.db.execute(
                    "UPDATE effect SET attempts=?, error=?, due=? WHERE kind=? AND key=?",
                    (new_attempts, str(exc)[:1000], self.now_ms() + delay, kind, key),
                )
            else:
                self.db.execute("DELETE FROM effect WHERE kind=? AND key=?", (kind, key))
                self.db.execute(
                    "INSERT OR REPLACE INTO effect_done (kind, key, done_at) VALUES (?,?,?)",
                    (kind, key, self.now_ms()),
                )
            processed += 1
        return processed

    def dispatch_ready(self) -> Dict[str, int]:
        """遍历所有已注册 kind 各处理一轮（调用方应在提交事务后轮询）。"""
        return {kind: self.dispatch(kind) for kind in self.handlers}

    # ── 查询与健康 ────────────────────────────────────────
    def health(self) -> Dict[str, str]:
        """返回处于错误状态的 kind → 最近错误。"""
        return dict(self._health_errors)

    def pending(self, kind: Optional[str] = None) -> int:
        if kind is None:
            row = self.db.execute("SELECT COUNT(*) FROM effect").fetchone()
        else:
            row = self.db.execute("SELECT COUNT(*) FROM effect WHERE kind=?", (kind,)).fetchone()
        return int(row[0])

    def is_blocked(self, kind: str, key: str) -> bool:
        """同 kind 前一条（更小 seq）存在失败未到重试时间的行时，后一条等待——保序。"""
        row = self.db.execute(
            "SELECT 1 FROM effect WHERE kind=? AND due>? LIMIT 1", (kind, self.now_ms())
        ).fetchone()
        return row is not None

    # ── 清理（不复制原型无限增长问题）────────────────────
    def cleanup_done(self, *, retention_ms: int = DONE_RETENTION_MS) -> int:
        cutoff = self.now_ms() - retention_ms
        cur = self.db.execute("DELETE FROM effect_done WHERE done_at < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

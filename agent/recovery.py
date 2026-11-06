"""长任务断点续传（recovery，B4 吸收自 Mika RunRecovery 设计思想）

与 Mika 的差异（源码依据：wangzan101_Mika/app/src/main/java/com/wangzan101/mika/task/RunRecovery.kt）：
- Mika 「从不从中间续跑」——屏幕状态回不去，只做善后（补送/告知中断）；
- 本实现面向后台长任务（构建/搬运/批量处理），状态可快照，因此
  在 Mika 的「检查点记录+中断善后+一次性防重」思想上，额外支持
  从检查点续跑：resume 时带着保存的阶段上下文重入。
- 「已告知」不另设字段，看送达账本（noticeIds/told 思想原样保留）。

核心概念：
- Run：一次长任务执行，phase: running → interrupted → recovered/failed；
- Checkpoint：阶段+上下文快照，resume 时回传给调用方；
- 善后：中断后通知（通过回调），只通知一次（账本防重）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

__all__ = ["RunRecovery", "RunPhase", "RunRecord", "Checkpoint"]


class RunPhase(str, Enum):
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    RECOVERED = "recovered"
    ENDED = "ended"
    FAILED = "failed"


@dataclass
class Checkpoint:
    """检查点：阶段 + 可序列化上下文。"""

    stage: str
    context: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"stage": self.stage, "context": self.context},
                          ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Checkpoint":
        data = json.loads(raw)
        return cls(stage=data["stage"], context=data.get("context", {}))


@dataclass
class RunRecord:
    run_id: str
    kind: str
    phase: RunPhase
    checkpoint: Optional[Checkpoint] = None
    started_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "phase": self.phase.value,
            "checkpoint": self.checkpoint.to_json() if self.checkpoint else None,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


class RunRecovery:
    """断点续传管理器（SQLite 持久，线程安全）。

    用法::

        rr = RunRecovery("/path/recovery.db")
        rr.begin("sync-1", "ima-upload")
        rr.checkpoint("sync-1", Checkpoint("batch-28", {"done": 2800}))
        # ……进程重启后……
        rr.recover(live_runs={"sync-2"})   # 标记不在 live 集合里的为中断
        rr.resume("sync-1", on_resume=lambda cp: ...)
    """

    def __init__(self, path: str, now: Optional[Callable[[], float]] = None):
        self._now = now or time.time
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                phase TEXT NOT NULL,
                checkpoint TEXT,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        # 善后账本：中断通知只发一次（Mika told() 思想：不另设字段，查账本）
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS notices (
                run_id TEXT NOT NULL,
                notice_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (run_id, notice_id)
            )"""
        )
        self._db.commit()
        self._recovered_once = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def begin(self, run_id: str, kind: str) -> None:
        now = self._now()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO runs(run_id, kind, phase, checkpoint, started_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, kind, RunPhase.RUNNING.value, None, now, now),
            )
            self._db.commit()

    def checkpoint(self, run_id: str, cp: Checkpoint) -> None:
        """做一点、存一点：每完成一个小单元调用一次。"""
        with self._lock:
            self._db.execute(
                "UPDATE runs SET checkpoint=?, phase=?, updated_at=? WHERE run_id=?",
                (cp.to_json(), RunPhase.RUNNING.value, self._now(), run_id),
            )
            self._db.commit()

    def end(self, run_id: str, ok: bool = True) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE runs SET phase=?, updated_at=? WHERE run_id=?",
                (RunPhase.ENDED.value if ok else RunPhase.FAILED.value, self._now(), run_id),
            )
            self._db.commit()

    # ------------------------------------------------------------------
    # 中断恢复
    # ------------------------------------------------------------------

    def recover(self, live_runs: Optional[set] = None) -> List[RunRecord]:
        """进程重启后调用一次：把不在 live 集合里的 running 运行标记中断。

        live_runs：重启后仍在跑的 run_id 集合（后台进程重启后通常为空）。
        返回本次标记为中断的运行列表；重复调用直接返回空（一次性防重）。
        """
        with self._lock:
            if self._recovered_once:
                return []
            self._recovered_once = True
            live = live_runs or set()
            rows = self._db.execute(
                "SELECT run_id, kind, checkpoint, started_at, updated_at FROM runs WHERE phase=?",
                (RunPhase.RUNNING.value,),
            ).fetchall()
            interrupted: List[RunRecord] = []
            for run_id, kind, cp, started, updated in rows:
                if run_id in live:
                    continue
                record = RunRecord(
                    run_id=run_id, kind=kind,
                    phase=RunPhase.INTERRUPTED,
                    checkpoint=Checkpoint.from_json(cp) if cp else None,
                    started_at=started, updated_at=updated,
                )
                self._mark_interrupted(record)
                interrupted.append(record)
            return interrupted

    def _mark_interrupted(self, record: RunRecord) -> None:
        self._db.execute(
            "UPDATE runs SET phase=?, updated_at=? WHERE run_id=?",
            (RunPhase.INTERRUPTED.value, self._now(), record.run_id),
        )
        self._db.commit()

    def resume(self, run_id: str, on_resume: Callable[[Optional[Checkpoint]], Any]) -> Any:
        """从检查点续跑：把最近检查点交给调用方，恢复成功后置 RECOVERED。

        on_resume(checkpoint) 抛异常则视为恢复失败：异常不外抛，
        保持 INTERRUPTED 状态（下次可再试），返回 None。
        """
        with self._lock:
            row = self._db.execute(
                "SELECT phase, checkpoint FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None or row[0] != RunPhase.INTERRUPTED.value:
                return None
            cp = Checkpoint.from_json(row[1]) if row[1] else None
        try:
            result = on_resume(cp)  # 锁外执行，恢复过程可能很长
        except Exception:
            # 恢复失败：保持 INTERRUPTED 状态，下次可再试
            return None
        with self._lock:
            self._db.execute(
                "UPDATE runs SET phase=?, updated_at=? WHERE run_id=?",
                (RunPhase.RECOVERED.value, self._now(), run_id),
            )
            self._db.commit()
        return result

    # ------------------------------------------------------------------
    # 善后通知（账本防重）
    # ------------------------------------------------------------------

    def notify_interrupted(self, run_id: str,
                           deliver: Callable[[str], bool]) -> bool:
        """中断通知只发一次：notice_id = interrupted:<run>（Mika noticeIds 思想）。

        deliver(text) 返回 True 视为送达并落账本；False 不落，下次再试。
        返回 True=本次送达/已送达过，False=送达失败待重试。
        """
        notice_id = f"interrupted:{run_id}"
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM notices WHERE run_id=? AND notice_id=?", (run_id, notice_id)
            ).fetchone()
            if row is not None:
                return True  # 已告知过
        ok = deliver(f"任务 {run_id} 中断，可从检查点续跑")
        if ok:
            with self._lock:
                self._db.execute(
                    "INSERT OR IGNORE INTO notices(run_id, notice_id, created_at) VALUES(?,?,?)",
                    (run_id, notice_id, self._now()),
                )
                self._db.commit()
        return ok

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id, kind, phase, checkpoint, started_at, updated_at "
                "FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return RunRecord(
                run_id=row[0], kind=row[1], phase=RunPhase(row[2]),
                checkpoint=Checkpoint.from_json(row[3]) if row[3] else None,
                started_at=row[4], updated_at=row[5],
            )

    def close(self) -> None:
        self._db.close()

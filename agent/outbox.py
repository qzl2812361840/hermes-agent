"""保序回信队列（outbox）——参考 dsh-superagent-prototype outbox.ts 设计（MIT），Python 自行实现。

语义：
  - 每个通道（channel）按 seq 保序：前一条未送达（且未达重试上限）时，后一条不出
  - 发送成功才标 done；失败退避重试；连续 3 次失败升级为系统问题（留痕不静默）
  - dropped 不删除：带时间戳 + 原因码留痕
  - ack 幂等：已 done 的重复 ack 直接忽略
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

__all__ = ["Outbox", "MAX_ATTEMPTS", "DroppedMessage"]

MAX_ATTEMPTS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_channel ON outbox (channel, seq);
CREATE TABLE IF NOT EXISTS outbox_dropped (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT NOT NULL,
    at_ms INTEGER NOT NULL
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class DroppedMessage:
    channel: str
    payload: str
    reason: str
    at_ms: int


class Outbox:
    def __init__(self, db: sqlite3.Connection, sender: Callable[[str, str], None], *, now_ms: Callable[[], int] = _now_ms):
        self.db = db
        self.sender = sender
        self.now_ms = now_ms
        self.dropped: list[DroppedMessage] = []
        db.executescript(_SCHEMA)

    def push(self, channel: str, payload: str) -> int:
        """入队一条回信（seq 自增保序）。"""
        row = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM outbox WHERE channel=?", (channel,)).fetchone()
        seq = int(row[0])
        cur = self.db.execute(
            "INSERT INTO outbox (channel, seq, payload) VALUES (?,?,?)", (channel, seq, payload)
        )
        return int(cur.lastrowid)

    def flush(self, channel: str) -> int:
        """按序投递该通道的消息；前一条未通则后一条等待。返回投递成功条数。"""
        sent = 0
        while True:
            row = self.db.execute(
                "SELECT id, payload, attempts FROM outbox WHERE channel=? AND status='pending' ORDER BY seq LIMIT 1",
                (channel,),
            ).fetchone()
            if row is None:
                break
            msg_id, payload, attempts = int(row[0]), row[1], int(row[2] or 0)
            try:
                self.sender(channel, payload)
            except Exception as exc:
                if attempts + 1 >= MAX_ATTEMPTS:
                    self.db.execute("DELETE FROM outbox WHERE id=?", (msg_id,))
                    self.db.execute(
                        "INSERT INTO outbox_dropped (channel, payload, reason, at_ms) VALUES (?,?,?,?)",
                        (channel, payload, str(exc)[:500], self.now_ms()),
                    )
                    self.dropped.append(DroppedMessage(channel, payload, str(exc)[:500], self.now_ms()))
                    return sent  # 升级为系统问题：本通道停止继续投递，留痕可见
                self.db.execute(
                    "UPDATE outbox SET attempts=?, error=? WHERE id=?",
                    (attempts + 1, str(exc)[:500], msg_id),
                )
                return sent
            else:
                self.db.execute("UPDATE outbox SET status='done', error=NULL WHERE id=?", (msg_id,))
                sent += 1
        return sent

    def is_acked(self, msg_id: int) -> bool:
        row = self.db.execute("SELECT status FROM outbox WHERE id=?", (msg_id,)).fetchone()
        return row is not None and row[0] == "done"

    def pending(self, channel: Optional[str] = None) -> int:
        if channel:
            row = self.db.execute("SELECT COUNT(*) FROM outbox WHERE channel=? AND status='pending'", (channel,)).fetchone()
        else:
            row = self.db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()
        return int(row[0])

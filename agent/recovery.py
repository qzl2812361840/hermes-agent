"""重启恢复语义（recovery）——参考 dsh-superagent-prototype resume() 设计（MIT），Python 自行实现。

语义：
  - 启动时扫描未完成工单：被中断的 running → 标 assignment_interrupted 并注入 recovery note
  - 恢复顺序：最老的票优先（按 created_at 升序）
  - pending 且不等人的 → 重新入 launch 队列
  - 等人的（waits_for_person）不动，等人的消息或显式重试
  - "stored work"：崩溃前的输入都在持久层里，恢复即续，不凭空补造
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

__all__ = ["RecoveredTicket", "recover"]

RECOVERY_NOTE = "此任务在上次运行中断后恢复，从中断点继续，不重头再来。"


@dataclass
class RecoveredTicket:
    ticket_id: str
    action: str  # resume / relaunch / keep_waiting
    note: str = RECOVERY_NOTE


@dataclass
class PersistedTicket:
    """恢复时的最小票快照（由调用方从持久层读出）。"""

    ticket_id: str
    created_at: int
    running: bool = False
    pending: bool = False
    waiting_person: bool = False
    ended: Optional[str] = None


def recover(tickets: List[PersistedTicket]) -> List[RecoveredTicket]:
    """按最老票优先恢复；返回每票的动作与注记。"""
    out: List[RecoveredTicket] = []
    for t in sorted([t for t in tickets if t.ended is None], key=lambda t: t.created_at):
        if t.waiting_person:
            out.append(RecoveredTicket(t.ticket_id, "keep_waiting"))
        elif t.running:
            out.append(RecoveredTicket(t.ticket_id, "resume", note=f"assignment_interrupted：{RECOVERY_NOTE}"))
        elif t.pending:
            out.append(RecoveredTicket(t.ticket_id, "relaunch"))
        else:
            out.append(RecoveredTicket(t.ticket_id, "resume"))
    return out

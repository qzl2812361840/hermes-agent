"""任务工单状态机（ticket_stage）——参考 dsh-superagent-prototype stage.ts 设计（MIT），Python 自行实现。

阶段表按序首个命中判定（与原原型语义一致）：
  person 系 → waiting → working → queued → idle
关键不变量：
  - person 阶段（等人回复/等报告）不能自续：can_retry 只对 failed/blocked 且未结束生效
  - waits_for_person 谓词决定"自动输入"（机器人/CI/政策变更）是否被扣住
  - add_input 幂等：同 id 只入一次
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

__all__ = ["TicketStage", "Ticket", "TicketInput", "Report", "ticket_stage", "can_retry", "waits_for_person", "add_input"]


class TicketStage:
    PERSON = "person"
    WAITING = "waiting"
    WORKING = "working"
    QUEUED = "queued"
    IDLE = "idle"


PERSON_STATES = {"waiting", "blocked", "failed", "retried", "answered", "delivered"}


@dataclass
class Report:
    """工单的一次回报；awaiting_person 表示球在人手上。"""

    state: str
    awaiting_person: bool = False


@dataclass
class TicketInput:
    """一次输入；human=True 才算人的输入，automatic 输入在 person 阶段会被扣住。"""

    id: str
    text: str = ""
    human: bool = False
    automatic: bool = False


@dataclass
class Ticket:
    id: str
    ended: Optional[str] = None  # 结束原因；None=未结束
    running_assignment: Optional[Any] = None
    pending_assignment: Optional[Any] = None
    report: Optional[Report] = None
    undelivered: List[TicketInput] = field(default_factory=list)
    held: List[TicketInput] = field(default_factory=list)  # 被扣住的自动输入


# 阶段表：按序首个命中
STAGES: List[Tuple[str, Callable[[Ticket], bool]]] = [
    ("person", lambda t: t.report is not None and t.report.awaiting_person and t.report.state in PERSON_STATES),
    ("waiting", lambda t: any(i.human for i in t.undelivered)),
    ("working", lambda t: t.running_assignment is not None),
    ("queued", lambda t: t.pending_assignment is not None),
    ("idle", lambda t: True),
]


def ticket_stage(t: Ticket) -> str:
    """按表序首个命中判定阶段。"""
    return next(name for name, pred in STAGES if pred(t))


def can_retry(t: Ticket) -> bool:
    """未结束且回报为 failed/blocked 才允许重试；person 阶段不能自续。"""
    if t.ended is not None:
        return False
    if t.report is None:
        return False
    return t.report.state in ("failed", "blocked")


def waits_for_person(t: Ticket) -> bool:
    return ticket_stage(t).startswith("person")


def add_input(t: Ticket, inp: TicketInput) -> bool:
    """幂等入输入（按 id 去重）；自动输入在 person 阶段被扣住不入 undelivered。

    返回 True 表示新入/新扣，False 表示重复忽略。
    """
    existing_ids = {i.id for i in t.undelivered} | {i.id for i in t.held}
    if inp.id in existing_ids:
        return False
    if inp.automatic and not inp.human and waits_for_person(t):
        t.held.append(inp)
        return True
    t.undelivered.append(inp)
    return True

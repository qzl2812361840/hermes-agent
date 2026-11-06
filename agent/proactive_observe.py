"""主动干活观察档（proactive_observe）——参考 dsh-superagent-prototype policy.ts 的 observe 语义（MIT），Python 自行实现。

原则：主动接单最大的风险是"乱接活"。accept_mode 缺省 observe：
  - inferred（规则推断）命中时不真接，只记录 would_accept + 候选卡，人复核后再逐规则切 auto
  - explicit（人直接下指令）走正常接单，不经观察档
  - auto 模式才真接
候选历史落在调用方的持久层（本模块只产结果），"先观察、复核后再切 auto"的节奏由人掌握。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = ["AcceptMode", "Outcome", "OutcomeKind", "assessment_outcome"]


class AcceptMode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"  # 缺省：只记录不执行
    AUTO = "auto"


class OutcomeKind(str, Enum):
    ACCEPT = "accept"
    IGNORE = "ignore"


@dataclass
class Outcome:
    kind: OutcomeKind
    would_accept: bool = False
    proposal: Optional[Dict[str, Any]] = None


@dataclass
class Candidate:
    """一条观察档候选卡：接/不接历史供人复核。"""

    at_ms: int
    rule_id: str
    trigger_summary: str
    suggested_action: str
    accepted: Optional[bool] = None  # 人复核后回填


@dataclass
class ObserveLedger:
    """候选卡台账（进程内；调用方可另存持久层）。"""

    entries: List[Candidate] = field(default_factory=list)

    def record(self, outcome: Outcome, *, at_ms: int, suggested_action: str = "") -> Optional[Candidate]:
        if outcome.proposal is None:
            return None
        cand = Candidate(
            at_ms=at_ms,
            rule_id=str(outcome.proposal.get("rule", "")),
            trigger_summary=str(outcome.proposal.get("trigger_summary", "")),
            suggested_action=suggested_action or str(outcome.proposal.get("suggested_action", "")),
        )
        self.entries.append(cand)
        return cand


def assessment_outcome(matched_rule: Optional[Any], mode: AcceptMode, *, direction_allowed: bool = True) -> Outcome:
    """三档判定：explicit 直通；inferred 按 mode 走 observe/accept/off。

    matched_rule 需带 strength（"explicit"/"inferred"）与 id 字段（鸭子类型，避免耦合调用方模型）。
    """
    if matched_rule is None:
        return Outcome(kind=OutcomeKind.IGNORE)
    strength = getattr(matched_rule, "strength", "inferred")
    rule_id = str(getattr(matched_rule, "id", ""))
    if strength == "explicit":
        return Outcome(kind=OutcomeKind.ACCEPT) if direction_allowed else Outcome(kind=OutcomeKind.IGNORE)
    # inferred：只有 auto 才真接
    if mode is AcceptMode.AUTO and direction_allowed:
        return Outcome(kind=OutcomeKind.ACCEPT)
    return Outcome(
        kind=OutcomeKind.IGNORE,
        would_accept=True,
        proposal={"rule": rule_id, "mode": mode.value},
    )

"""点后核验强化（post_action_verifier，B6）

缺口：设备/UI 动作（点击、滑动、输入）执行后，退出码只代表"指令发出去了"，
不代表"效果真的发生了"。本模块统一补上效果核验：
  执行 → 探测（期望条件）→ 未达则按阶梯重试 → 仍不达则如实上报
阶梯：立即探测 → 重试动作+探测（默认 2 次，间隔退避）→ 上报失败（附每轮证据）。
- 探测函数读不到现状（抛异常/返回 None）按未达成处理，不瞎判成功；
- 超时单独归 TIMED_OUT，不与失败混报（沿用 mutation_verifier 的三分级口径）；
- 默认零影响：只有调用方主动把动作包进 verify_action 才生效。

吸收 Droidance ScreenActionBatch / ModelCoordinateMapper 的"动作后回看验证"思想，自行 Python 实现。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

__all__ = ["PostActionOutcome", "ActionStatus", "verify_action"]


class ActionStatus(str, Enum):
    VERIFIED = "VERIFIED"      # 动作效果经探测确认
    RETRY_VERIFIED = "RETRY_VERIFIED"  # 首轮未达，重试后确认
    FAILED = "FAILED"          # 重试耗尽仍未达成
    TIMED_OUT = "TIMED_OUT"    # 总预算耗尽
    REFUSED = "REFUSED"        # 动作本身执行被拒（如审批拦截）


@dataclass
class PostActionOutcome:
    """一次点后核验的完整结果，含每轮探测证据。"""

    status: ActionStatus
    attempts: int = 0                       # 实际执行动作的次数
    checks: list = field(default_factory=list)  # 每轮探测证据 [{attempt, probe_ok, detail}]
    failure_code: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (ActionStatus.VERIFIED, ActionStatus.RETRY_VERIFIED)

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "attempts": self.attempts,
            "checks": self.checks,
            "failure_code": self.failure_code,
            "elapsed_ms": self.elapsed_ms,
        }


def verify_action(
    action: Callable[[], Any],
    probe: Callable[[], Optional[bool]],
    *,
    max_retries: int = 2,
    interval_s: float = 0.5,
    backoff: float = 2.0,
    budget_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    action_refused: bool = False,
) -> PostActionOutcome:
    """执行动作并按阶梯核验效果。

    action: 执行设备/UI 动作（可重入；抛异常视为该轮动作失败）。
    probe: 探测期望效果，返回 True/False；None 或抛异常=读不到现状，按未达成处理。
    action_refused: 调用方已知动作被审批层拒绝时直接置 REFUSED，不执行不重试。
    """
    start = clock()

    def _elapsed() -> int:
        return int((clock() - start) * 1000)

    if action_refused:
        return PostActionOutcome(status=ActionStatus.REFUSED, failure_code="action_refused",
                                 elapsed_ms=_elapsed())

    deadline = start + budget_s
    interval = interval_s
    attempts = 0
    checks: list = []

    def _probe_once(attempt: int) -> tuple[bool, Any]:
        try:
            seen = probe()
        except Exception as exc:  # 读不到现状按未达成
            return False, f"probe_error:{type(exc).__name__}"
        if seen is None:
            return False, "probe_none"  # 读不到现状，不瞎判成功
        return bool(seen), None

    for attempt in range(1, max_retries + 2):  # 首轮 + 重试
        if clock() >= deadline:
            return PostActionOutcome(status=ActionStatus.TIMED_OUT, attempts=attempts,
                                     checks=checks, failure_code="budget_exhausted",
                                     elapsed_ms=_elapsed())
        try:
            action()  # 每轮都执行动作（首轮+重试）
            attempts += 1
        except Exception as exc:
            attempts += 1  # 动作发出去了但出错，也算一次尝试
            checks.append({"attempt": attempt, "probe_ok": False,
                           "detail": f"action_error:{type(exc).__name__}"})
            if clock() + interval <= deadline:
                sleeper(interval)
                interval *= backoff
            continue

        ok, detail = _probe_once(attempt)
        checks.append({"attempt": attempt, "probe_ok": ok, "detail": detail})
        if ok:
            status = ActionStatus.VERIFIED if attempt == 1 else ActionStatus.RETRY_VERIFIED
            return PostActionOutcome(status=status, attempts=attempts, checks=checks,
                                     elapsed_ms=_elapsed())
        if clock() + interval <= deadline:
            sleeper(interval)
            interval *= backoff

    return PostActionOutcome(status=ActionStatus.FAILED, attempts=attempts, checks=checks,
                             failure_code="probe_not_met", elapsed_ms=_elapsed())

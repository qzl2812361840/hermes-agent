"""写操作读回核验包装器（mutation_verifier）

统一模式（设计参考 Eta DeviceStateMutations 的核验流程，自行实现）：
  read(现状) → mutate(执行) → read(回读) → 比对 → 汇报
  - before != after 才算 changed；root/命令退出码决定 verified
  - 读不到当前状态一律拒绝执行（先能读才准写）
  - 退出码三分级：SUCCEEDED / FAILED / TIMED_OUT，超时不等于失败，如实标注
  - 改动没生效如实上报 changed=False，不瞎重试
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

__all__ = [
    "MutationOutcome",
    "OutcomeStatus",
    "verify_mutation",
    "ExitCodePolicy",
]


class OutcomeStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    REFUSED = "REFUSED"  # 读不到现状，拒绝执行


class ExitCodePolicy:
    """退出码三分级：0=成功；超时(由 timeout 标记)单独归 TIMED_OUT；其余=失败。"""

    @staticmethod
    def classify(exit_code: int | None, *, timed_out: bool = False) -> OutcomeStatus:
        if timed_out:
            return OutcomeStatus.TIMED_OUT
        if exit_code == 0:
            return OutcomeStatus.SUCCEEDED
        return OutcomeStatus.FAILED


@dataclass
class MutationOutcome:
    """一次写操作的完整核验结果。"""

    status: OutcomeStatus
    before: Any = None
    after: Any = None
    expected: Any = None
    changed: bool = False
    verified: bool = False
    failure_code: str = ""
    exit_code: int | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "before": self.before,
            "after": self.after,
            "expected": self.expected,
            "changed": self.changed,
            "verified": self.verified,
            "failure_code": self.failure_code,
            "exit_code": self.exit_code,
            "detail": self.detail,
        }


def verify_mutation(
    read_state: Callable[[], Any],
    mutate: Callable[[], Any],
    *,
    expected: Any = None,
    timeout: float | None = None,
) -> MutationOutcome:
    """执行一次带读回核验的写操作。

    read_state : 返回当前状态（可pickle的标量/结构）；抛异常 = 读不到现状
    mutate     : 执行写操作，返回 (exit_code, timed_out) 或仅 exit_code
    expected   : 期望的变更后状态；None 时不做期望比对，仅比对 before/after
    """
    outcome = MutationOutcome(status=OutcomeStatus.REFUSED)

    # 第一步：读现状。读不到 = 拒绝执行。
    try:
        outcome.before = read_state()
    except Exception as exc:  # noqa: BLE001 - 读失败原因多种，如实拒绝
        outcome.failure_code = f"STATE_UNAVAILABLE: {type(exc).__name__}"
        return outcome

    # 第二步：执行
    started = time.monotonic()
    timed_out = False
    try:
        result = mutate()
    except Exception as exc:  # noqa: BLE001
        outcome.status = OutcomeStatus.FAILED
        outcome.failure_code = f"MUTATE_RAISED: {type(exc).__name__}"
        return outcome
    if timeout is not None and time.monotonic() - started > timeout:
        timed_out = True

    if isinstance(result, tuple) and len(result) == 2:
        exit_code, mut_timed_out = result
        timed_out = timed_out or bool(mut_timed_out)
    else:
        exit_code = result if isinstance(result, int) else None

    outcome.exit_code = exit_code if isinstance(exit_code, int) else None
    outcome.status = ExitCodePolicy.classify(outcome.exit_code, timed_out=timed_out)
    outcome.expected = expected

    # 第三步：读回核验。命令成功但读不回，也如实标注 unverified。
    if outcome.status is OutcomeStatus.SUCCEEDED:
        try:
            outcome.after = read_state()
            outcome.changed = outcome.after != outcome.before
            if expected is not None:
                outcome.verified = outcome.changed and outcome.after == expected
            else:
                outcome.verified = outcome.changed
        except Exception as exc:  # noqa: BLE001
            outcome.after = None
            outcome.changed = False
            outcome.verified = False
            outcome.failure_code = f"VERIFY_READ_FAILED: {type(exc).__name__}"
    elif outcome.status is OutcomeStatus.TIMED_OUT:
        outcome.failure_code = "ROOT_COMMAND_TIMEOUT"

    return outcome

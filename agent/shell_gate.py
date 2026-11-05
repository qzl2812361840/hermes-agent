"""shell_policy 接线层（B1 接线）：把三档命令策略挂进执行链。

设计：
- 默认 OFF：未设置 HERMES_SHELL_MODE 时零影响——不多判定、不写审计、不改任何行为。
- DENY → 拦截；CONFIRM → terminal 场景需用户已确认（force=True），
  device_shell 场景交给既有审批门（confirm_pass_through=True 时放行给审批流）。
- 审计只在策略启用时落盘：hermes_home/logs/shell_audit.jsonl（环形 300 条 + JSONL 追加）。
- 判定内部异常时 fail-open 并记录日志（策略故障不得瘫痪执行链），
  与 shell_hooks 的 fail-open 哲学一致。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from agent.shell_policy import (
    CONFIRM,
    ShellAuditLog,
    ShellMode,
    ShellVerdict,
    judge_shell_command,
)

logger = logging.getLogger(__name__)

#: 策略档位环境变量：off / safe / full，未设置或非法值一律按 OFF
MODE_ENV = "HERMES_SHELL_MODE"

_audit: Optional[ShellAuditLog] = None
_audit_lock = threading.Lock()


def current_mode() -> ShellMode:
    """读取当前策略档位；未设置/非法值 = OFF（零影响）。"""
    raw = (os.getenv(MODE_ENV) or "").strip().lower()
    if not raw:
        return ShellMode.OFF
    try:
        return ShellMode(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, shell policy stays OFF", MODE_ENV, raw)
        return ShellMode.OFF


def _get_audit() -> ShellAuditLog:
    """惰性单例：策略首次启用时才建审计（路径定位失败退化为纯内存）。"""
    global _audit
    if _audit is None:
        with _audit_lock:
            if _audit is None:
                path = None
                try:
                    from hermes_constants import get_hermes_home

                    log_dir = Path(get_hermes_home()).expanduser() / "logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    path = str(log_dir / "shell_audit.jsonl")
                except Exception:  # noqa: BLE001 - 审计落盘失败不拦判定
                    logger.warning(
                        "shell audit log path unavailable; audit stays in-memory",
                        exc_info=True,
                    )
                _audit = ShellAuditLog(path=path)
    return _audit


def reset_audit_for_tests() -> None:
    """测试辅助：清掉审计单例（并发测试不应调用）。"""
    global _audit
    with _audit_lock:
        _audit = None


def policy_verdict(command: str) -> Optional[ShellVerdict]:
    """策略启用时返回判定结果；OFF 或内部异常时返回 None（放行）。"""
    mode = current_mode()
    if mode is ShellMode.OFF:
        return None
    try:
        return judge_shell_command(command, mode=mode, audit=_get_audit())
    except Exception:  # noqa: BLE001 - fail-open：策略故障不得瘫痪执行链
        logger.exception(
            "shell_policy judge failed; failing open for command: %s",
            str(command)[:120],
        )
        return None


def policy_block(
    command: str,
    *,
    force: bool = False,
    confirm_pass_through: bool = False,
) -> Optional[dict[str, Any]]:
    """返回拦截说明 dict；None = 放行。

    force=True：terminal 场景用户已确认，CONFIRM 级放行（DENY 仍拦）。
    confirm_pass_through=True：device_shell 场景，CONFIRM 级交给既有审批门处理。
    """
    verdict = policy_verdict(command)
    if verdict is None or verdict.allowed:
        return None
    if verdict.action == CONFIRM and (force or confirm_pass_through):
        return None
    return {
        "status": "error",
        "mode": current_mode().value,
        "action": verdict.action,
        "reason": verdict.reason,
        "error": (
            f"已被命令安全策略拦截（{current_mode().value} 档）：{verdict.reason}。"
            "如确属用户授权的操作，请先向用户说明风险并取得确认，再带确认标记重试。"
        ),
    }


def policy_block_json(
    command: str,
    *,
    force: bool = False,
    confirm_pass_through: bool = False,
) -> Optional[str]:
    """同 policy_block，返回工具层可直接返回的 JSON 串；None = 放行。"""
    block = policy_block(command, force=force, confirm_pass_through=confirm_pass_through)
    if block is None:
        return None
    return json.dumps(block, ensure_ascii=False)

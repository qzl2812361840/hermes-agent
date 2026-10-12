"""ADB 传输层：手机控制工具的公共底座（主人令 2026-10-13）。

只依赖 ``adb`` 可执行文件——USB 连接（手机开 USB 调试）或无线
（``adb connect <ip>:<port>``）连上即可，服务器上不需要 root。
支持两条控制路线：

* 普通 shell：``adb -s <serial> shell <cmd>``（user 权限）
* root：``adb root`` 重启 adbd 后命令以 root 跑（userdebug/eng 固件、
  部分 Root 过的机型；失败自动回退普通 shell）

安全策略：命令命中 blocklist（默认拦 ``mkfs``/``dd if=``/恢复出厂等
毁灭性操作）直接拒绝；blocklist 可经 config ``mobile.blocked_patterns``
或环境变量 ``MOBILE_BLOCKED_PATTERNS``（逗号分隔正则）覆盖。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# 默认拦截的毁灭性命令（正则，大小写不敏感）。设计哲学是"控制每一个软件"，
# 但按几下键盘就把手机变砖不在授权范围内——patterns 可由主人自行放开。
DEFAULT_BLOCKED_PATTERNS = (
    r"\bmkfs\b",
    r"\bmkfs\.\w+",
    r"\bdd\s+if=/dev/(zero|urandom|random)\b",
    r"\bnewfs\b",
    r"factory[_\s-]?reset",
    r"\berase\s+-j\s+/dev/block",
    r"\bfastboot\b",
    r"\bfastboot\.exe\b",
    r">\s*/dev/block/",
    r"\bflash_all\.sh\b",
)

_ADB_CANDIDATE_PATHS = (
    "/usr/bin/adb",
    "/usr/local/bin/adb",
    "/opt/android-sdk/platform-tools/adb",
    "/opt/platform-tools/adb",
    os.path.expanduser("~/platform-tools/adb"),
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
    "/usr/lib/android-sdk/platform-tools/adb",
)


def adb_path() -> Optional[str]:
    """Locate the adb binary: env override, PATH, then common install paths."""
    for env_name in ("MOBILE_ADB_PATH", "ADB_PATH"):
        value = (os.environ.get(env_name) or "").strip()
        if value and (os.path.isfile(value) or shutil.which(value)):
            return value
    found = shutil.which("adb")
    if found:
        return found
    for candidate in _ADB_CANDIDATE_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


def adb_available() -> bool:
    """check_fn for the mobile toolset: an adb binary on this host."""
    return adb_path() is not None


def blocked_patterns() -> list:
    """Blocked command patterns: config/env override, else the built-in list."""
    raw = (os.environ.get("MOBILE_BLOCKED_PATTERNS") or "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        mobile_cfg = cfg.get("mobile") if isinstance(cfg.get("mobile"), dict) else {}
        configured = mobile_cfg.get("blocked_patterns")
        if isinstance(configured, (list, tuple)) and configured:
            return [str(p) for p in configured]
    except Exception:
        logger.debug("mobile_adb: config read for blocked_patterns failed", exc_info=True)

    return list(DEFAULT_BLOCKED_PATTERNS)


def check_command_allowed(command: str) -> Optional[str]:
    """None when the command may run, else the human-readable refusal reason."""
    patterns = blocked_patterns()
    for pattern in patterns:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return f"命令命中手机安全拦截名单（{pattern}），已拒绝执行。如确需执行，请主人调整 mobile.blocked_patterns 配置。"
        except re.error:
            logger.warning("mobile_adb: bad blocked pattern %r ignored", pattern)

    return None


def _run(args: list, timeout: float) -> dict:
    """Run an adb invocation; returns a result dict (never raises)."""
    binary = adb_path()
    if not binary:
        return {"ok": False, "error": "找不到 adb 可执行文件。请安装 Android platform-tools 并确保 adb 在 PATH（或设置 MOBILE_ADB_PATH）。"}
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, timeout=timeout, text=True, errors="replace",
        )

        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"adb 命令超时（{timeout:.0f}s）"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": f"adb 执行失败: {exc}"}


def list_devices() -> list:
    """Connected devices: ``[{"serial", "state", "model"}, ...]``."""
    result = _run(["devices", "-l"], timeout=15)
    if not result.get("ok"):
        return []
    devices = []
    for line in (result.get("stdout") or "").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        entry = {"serial": parts[0], "state": parts[1] if len(parts) > 1 else "unknown", "model": ""}
        for part in parts[2:]:
            if part.startswith("model:"):
                entry["model"] = part[len("model:"):]
        devices.append(entry)

    return devices


def resolve_serial(serial: str = "") -> dict:
    """Pick the target device: explicit serial, else env pin, else the single
    connected device. Returns ``{"serial": ...}`` or ``{"error": ...}``."""
    wanted = (serial or os.environ.get("MOBILE_DEVICE_SERIAL") or "").strip()
    devices = [d for d in list_devices() if d.get("state") == "device"]
    if not devices:
        return {"error": "没有已连接的手机设备。请用 USB 连接并开启 USB 调试，或先 adb connect <ip>:<port> 连接无线 adb。"}
    if wanted:
        for device in devices:
            if device["serial"] == wanted or wanted in (device.get("model") or ""):
                return {"serial": device["serial"]}
        return {"error": f"找不到设备 {wanted!r}。当前已连接: {', '.join(d['serial'] for d in devices)}"}
    if len(devices) == 1:
        return {"serial": devices[0]["serial"]}

    return {
        "error": "检测到多台设备，请用 serial 参数指定。当前已连接: "
        + ", ".join(f"{d['serial']}({d.get('model') or '?'})" for d in devices),
    }


def shell(command: str, serial: str = "", root: bool = False, timeout: float = 60.0) -> dict:
    """Run a shell command on the device (root via ``adb root`` when asked)."""
    refusal = check_command_allowed(command)
    if refusal:
        return {"ok": False, "error": refusal}
    target = resolve_serial(serial)
    if "error" in target:
        return target
    device_serial = target["serial"]

    if root:
        # `adb root` restarts adbd as root (userdebug/eng builds, rooted phones).
        # Failure is fine — the command still runs unprivileged below.
        _run(["-s", device_serial, "root"], timeout=20)

    return _run(["-s", device_serial, "shell", command], timeout=timeout)

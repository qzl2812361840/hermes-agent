#!/usr/bin/env python3
"""手机控制工具集（主人令 2026-10-13）：让嘎妞知道自己能控手机、并且能控。

设计哲学（主人原话）：
  1. "让它知道自己能够控制手机，无非就是 root 和无障碍模式，也就是手机 Shell 命令"
  2. "让它能够控制手机上的每一个软件"
落地为 5 个工具（toolset ``mobile``，默认可用，无 adb 时自动隐藏）：

  mobile_devices     列出已连接设备（USB / 无线 adb）
  mobile_shell       在手机上执行 shell 命令（root=True 走 adb root）
  mobile_screenshot  截屏存盘，返回路径（可接 vision_analyze 看内容）
  mobile_ui_dump     导出当前界面元素树（无障碍节点：text/desc/bounds）
  mobile_input       模拟操作：tap / swipe / text / keyevent / back / home

adb 不在服务器上、或没连任何设备时 check_fn 把整个 toolset 判为不可用——
装好 platform-tools 并连上设备即插即用，无需改代码。（2026-10-16 两项修订：
① 判定加严到"确有设备在线"，避免模型看到点不动的假工具；
② 边界声明：本工具集是**服务器本机** adb 通道，面向开发调试；
   操控**用户的手机**请用 phone 工具集，经嘎妞 App 执行，两者不要混用。）
"""

import logging
import os
import re
from typing import Any, Dict

from tools.registry import registry, tool_error, tool_result
from tools.mobile_adb import adb_available, list_devices, shell

logger = logging.getLogger(__name__)

_SCREENSHOT_DIR_ENV = "MOBILE_SCREENSHOT_DIR"


def _screenshot_dir() -> str:
    """Where screencaps land: env override, else <hermes home>/mobile_shots."""
    override = (os.environ.get(_SCREENSHOT_DIR_ENV) or "").strip()
    if override:
        return override
    try:
        from hermes_constants import get_hermes_home

        return os.path.join(get_hermes_home(), "mobile_shots")
    except Exception:
        return os.path.join(os.path.expanduser("~/.hermes"), "mobile_shots")


def _adb_path():
    from tools.mobile_adb import adb_path

    return adb_path()


# ── handlers ──────────────────────────────────────────────────────────

def mobile_devices_tool(args: Dict[str, Any], **kw) -> str:
    devices = list_devices()
    if not devices:
        return tool_result(
            devices=[],
            hint="没有已连接的设备。USB 连接并开启调试，或 adb connect <ip>:<port> 走无线。",
        )

    return tool_result(devices=devices, count=len(devices))


def mobile_shell_tool(args: Dict[str, Any], **kw) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        return tool_error("command 不能为空")
    result = shell(
        command,
        serial=str(args.get("serial") or ""),
        root=bool(args.get("root")),
        timeout=float(args.get("timeout") or 60),
    )
    if not result.get("ok"):
        return tool_error(result.get("error") or result.get("stderr") or "adb 命令失败")
    out = result.get("stdout") or ""
    err = result.get("stderr") or ""
    if len(out) > 20000:
        out = out[:20000] + "\n…[输出截断]"
    payload: Dict[str, Any] = {"ok": True, "stdout": out}
    if err.strip():
        payload["stderr"] = err[:4000]

    return tool_result(payload)


def mobile_screenshot_tool(args: Dict[str, Any], **kw) -> str:
    import subprocess
    import time

    from tools.mobile_adb import _adb_path, resolve_serial

    target = resolve_serial(str(args.get("serial") or ""))
    if "error" in target:
        return tool_error(target["error"])
    serial = target["serial"]
    binary = _adb_path()
    if not binary:
        return tool_error("找不到 adb 可执行文件")
    directory = _screenshot_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"shot_{serial.replace(':', '_')}_{int(time.time())}.png")
    # exec-out 不做 LF->CRLF 转换，二进制安全；shell 重定向会损坏 PNG。
    try:
        with open(path, "wb") as fh:
            proc = subprocess.run(
                [binary, "-s", serial, "exec-out", "screencap", "-p"],
                stdout=fh, stderr=subprocess.PIPE, timeout=30,
            )
        if proc.returncode != 0 or os.path.getsize(path) == 0:
            return tool_error(f"截屏失败: {(proc.stderr or b'').decode('utf-8', 'replace')[:500]}")
    except Exception as exc:
        return tool_error(f"截屏失败: {exc}")

    return tool_result(path=path, bytes=os.path.getsize(path), hint="用 vision_analyze 查看这张截图的内容")


def mobile_ui_dump_tool(args: Dict[str, Any], **kw) -> str:
    """Accessibility node tree of the current screen (compressed).

    ``uiautomator dump`` writes XML to the device; we ``cat`` it back (no pull
    needed) and keep text/desc/bounds attributes so the model knows WHAT is on
    screen and WHERE to tap — 这是"控制每一个软件"的眼睛。
    """
    remote = "/sdcard/window_dump.xml"
    dumped = shell(f"uiautomator dump {remote}", serial=str(args.get("serial") or ""), timeout=30)
    if not dumped.get("ok"):
        return tool_error(dumped.get("error") or dumped.get("stderr") or "uiautomator dump 失败")
    read = shell(f"cat {remote}", serial=str(args.get("serial") or ""), timeout=30)
    if not read.get("ok"):
        return tool_error(read.get("error") or "读取界面树失败")
    xml = read.get("stdout") or ""
    nodes = re.findall(
        r'<node[^>]*?(?:text|content-desc)="([^"]+)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*?/?>',
        xml,
    )
    elements = [
        {"label": label.strip(), "bounds": f"({x1},{y1})-({x2},{y2})"}
        for label, x1, y1, x2, y2 in nodes
        if label.strip()
    ][:120]
    if not elements:
        return tool_result(elements=[], note="当前界面没有可读的文字元素", raw_bytes=len(xml))

    return tool_result(
        elements=elements,
        count=len(elements),
        hint="点元素中心即可：mobile_input action=tap x=(x1+x2)/2 y=(y1+y2)/2",
    )


_KEY_EVENT_ALIASES = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "power": "KEYCODE_POWER",
    "enter": "KEYCODE_ENTER",
    "delete": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB",
    "menu": "KEYCODE_MENU",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
}


def mobile_input_tool(args: Dict[str, Any], **kw) -> str:
    action = str(args.get("action") or "").strip().lower()
    serial = str(args.get("serial") or "")

    if action == "tap":
        x, y = args.get("x"), args.get("y")
        if x is None or y is None:
            return tool_error("tap 需要 x 和 y")
        command = f"input tap {int(float(x))} {int(float(y))}"
    elif action == "swipe":
        try:
            x1, y1 = int(float(args["x1"])), int(float(args["y1"]))
            x2, y2 = int(float(args["x2"])), int(float(args["y2"]))
        except KeyError as exc:
            return tool_error(f"swipe 缺少坐标: {exc}")
        duration = args.get("duration_ms") or 300
        command = f"input swipe {x1} {y1} {x2} {y2} {int(duration)}"
    elif action == "text":
        text = str(args.get("text") or "")
        if not text:
            return tool_error("text 不能为空")
        # input text 只接受少量转义：空格 -> %s，& | < > ; ( ) 需引号包裹或替换
        escaped = text.replace(" ", "%s").replace("'", "")
        command = f"input text '{escaped}'"
    elif action == "keyevent":
        code = str(args.get("keycode") or "").strip()
        if not code:
            return tool_error("keyevent 需要 keycode")
        code = _KEY_EVENT_ALIASES.get(code.lower(), code)
        command = f"input keyevent {code}"
    elif action in _KEY_EVENT_ALIASES:
        command = f"input keyevent {_KEY_EVENT_ALIASES[action]}"
    else:
        return tool_error(
            f"未知 action: {action}。可用: tap / swipe / text / keyevent / "
            + " / ".join(_KEY_EVENT_ALIASES)
        )

    result = shell(command, serial=serial, timeout=30)
    if not result.get("ok"):
        return tool_error(result.get("error") or result.get("stderr") or "输入失败")

    return tool_result(ok=True, action=action, command=command)


def _mobile_ready() -> bool:
    """adb 可用且至少一台设备在线——避免"可见但点不动"的假工具。

    2026-10-16 修：原来只查 adb 二进制存在，服务器装着 platform-tools
    但没插手机时工具照样可见，模型一调就失败。现在要求 list_devices() 非空。
    """
    if not adb_available():
        return False
    try:
        return bool(list_devices())
    except Exception:
        return False


# ── schemas ───────────────────────────────────────────────────────────

_SERIAL_PROP = {
    "serial": {
        "type": "string",
        "description": "设备序列号（mobile_devices 返回值）。多台设备时必填；单台可省略。",
    }
}

_SCHEMAS = [
    (
        "mobile_devices",
        "列出当前连接的所有手机设备（USB 调试或无线 adb）。控制手机前先调用它确认设备在线。",
        {"type": "object", "properties": {}, "required": []},
        mobile_devices_tool,
        "📱",
    ),
    (
        "mobile_shell",
        "在手机上执行 shell 命令——控制手机上每一个软件的总入口：启动 Activity"
        "（am start）、查看/杀死进程（ps、am force-stop）、读写文件、装应用"
        "（pm install）、读系统属性（getprop）等。root=True 时先 adb root 提权"
        "（需要 userdebug 固件或已 Root 的机型）。毁灭性命令（mkfs/dd/恢复出厂等）"
        "会被安全名单拦截。",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要在手机上执行的 shell 命令"},
                "root": {"type": "boolean", "description": "是否尝试 root 提权（默认 false）"},
                "timeout": {"type": "number", "description": "超时秒数，默认 60"},
                **_SERIAL_PROP,
            },
            "required": ["command"],
        },
        mobile_shell_tool,
        "📱",
    ),
    (
        "mobile_screenshot",
        "给手机截屏并保存为 PNG，返回文件路径。随后用 vision_analyze 分析截图内容。",
        {
            "type": "object",
            "properties": {**_SERIAL_PROP},
            "required": [],
        },
        mobile_screenshot_tool,
        "📸",
    ),
    (
        "mobile_ui_dump",
        "导出当前屏幕的无障碍元素树（每个可交互元素的文字与坐标 bounds）。"
        "想知道屏幕上有什么、某按钮在哪时用它——这是控制任意 App 的眼睛。",
        {
            "type": "object",
            "properties": {**_SERIAL_PROP},
            "required": [],
        },
        mobile_ui_dump_tool,
        "🔍",
    ),
    (
        "mobile_input",
        "在手机上模拟操作：tap（点击坐标）、swipe（滑动）、text（输入文本）、"
        "keyevent（按键码）、back / home / recents / power 等系统键。"
        "坐标可从 mobile_ui_dump 的 bounds 取得。",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["tap", "swipe", "text", "keyevent", "back", "home", "recents", "power", "enter", "delete", "tab", "menu", "volume_up", "volume_down"],
                    "description": "操作类型",
                },
                "x": {"type": "number", "description": "tap 的 x 坐标"},
                "y": {"type": "number", "description": "tap 的 y 坐标"},
                "x1": {"type": "number", "description": "swipe 起点 x"},
                "y1": {"type": "number", "description": "swipe 起点 y"},
                "x2": {"type": "number", "description": "swipe 终点 x"},
                "y2": {"type": "number", "description": "swipe 终点 y"},
                "duration_ms": {"type": "number", "description": "swipe 时长毫秒，默认 300"},
                "text": {"type": "string", "description": "text 操作要输入的文本"},
                "keycode": {"type": "string", "description": "keyevent 的按键码（如 KEYCODE_VOLUME_UP 或 4）"},
                **_SERIAL_PROP,
            },
            "required": ["action"],
        },
        mobile_input_tool,
        "👆",
    ),
]


def _adb_path():
    from tools.mobile_adb import adb_path

    return adb_path()


for _name, _desc, _schema, _handler, _emoji in _SCHEMAS:
    registry.register(
        name=_name,
        toolset="mobile",
        schema={"name": _name, "description": _desc, "parameters": _schema},
        handler=_handler,
        check_fn=_mobile_ready,
        emoji=_emoji,
    )

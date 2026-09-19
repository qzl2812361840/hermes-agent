"""phone_control — 嘎妞手机操控工具（2026-09-19 接真链路版）

链路：本工具 → 后端 /api/v1/phone/control（Hermes Key 鉴权）→ 命令队列
      → 用户手机上的嘎妞 App 每 2 秒轮询取走 → 执行（Root/Shizuku/无障碍）→ 回报结果
      → 本工具把真实结果返回给模型

动作白名单与 App 端 PhoneCommandExecutor 完全一致（不列没实现的动作）：
  open_app / press_home / press_back / swipe / tap / type_text / shell /
  set_wifi / set_bluetooth / set_flashlight / speak / screenshot
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from tools.registry import registry

_ACTIONS = (
    "open_app", "press_home", "press_back", "swipe", "tap", "type_text",
    "shell", "set_wifi", "set_bluetooth", "set_flashlight", "speak", "screenshot",
)


def check_requirements() -> bool:
    """没有后端地址就不暴露这个工具（避免出现点不动的假工具）。"""
    return bool(os.getenv("GANIU_BACKEND_URL"))


def _backend_url() -> str:
    return (os.getenv("GANIU_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")


def _hermes_key() -> str:
    key = os.getenv("HERMES_KEY", "")
    if key:
        return key
    # 兜底：从后端 .env 读（Hermes 进程环境里没配 HERMES_KEY 时）
    for path in ("/d/嘎妞后端/.env", "/root/ganiu-backend/.env"):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                for line in fp:
                    if line.startswith("HERMES_KEY="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return ""


def phone_control(
    action: str = "",
    params: dict | None = None,
    user_id: int | None = None,
    device_name: str = "",
    wait_seconds: int = 20,
    session_id: str | None = None,
    **kwargs: Any,
) -> str:
    """操控主人的手机（经嘎妞 App 执行）。

    Args:
        action: 动作，必须是这些之一：
            open_app（params.package 包名）、press_home、press_back、
            swipe（params.x1,y1,x2,y2）、tap（params.x,y）、
            type_text（params.text 输入文字）、shell（params.cmd 执行命令）、
            set_wifi / set_bluetooth / set_flashlight（params.on true/false）、
            speak（params.text 让手机朗读）、screenshot（截屏并保存到后端）
        params: 动作参数（见上）
        user_id: 目标用户；不填则从会话 id 推断
        device_name: 目标设备名（多设备时用；与 user_id 二选一）
        wait_seconds: 等手机回报结果的秒数（默认 20，最长 120）

    Returns:
        JSON 字符串：{ok, status, output, id, note}
    """
    action = (action or "").strip()
    if action not in _ACTIONS:
        return json.dumps(
            {"ok": False, "error": f"不支持的动作: {action or '(空)'}", "supported": list(_ACTIONS)},
            ensure_ascii=False,
        )

    payload = {
        "action": action,
        "params": params or {},
        "user_id": user_id,
        "device_name": device_name or None,
        "session_id": session_id or kwargs.get("session_id"),
        "wait_seconds": wait_seconds,
    }
    headers = {"X-Hermes-Key": _hermes_key(), "Content-Type": "application/json"}
    try:
        resp = httpx.post(f"{_backend_url()}/api/v1/phone/control", json=payload, headers=headers, timeout=150)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"后端不可达: {exc}"}, ensure_ascii=False)

    if resp.status_code != 200:
        return json.dumps(
            {"ok": False, "error": f"后端返回 {resp.status_code}: {resp.text[:200]}"}, ensure_ascii=False
        )
    try:
        data = resp.json()
    except Exception:
        return json.dumps({"ok": False, "error": f"响应解析失败: {resp.text[:200]}"}, ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)


TOOL_SCHEMA = {
    "name": "phone_control",
    "description": (
        "操控主人手机上的 App/系统（经嘎妞 App 执行，需要手机在线）。"
        "支持：打开应用(open_app)、返回桌面(press_home)、返回键(press_back)、滑动(swipe)、"
        "点击(tap)、输入文字(type_text)、执行 shell(shell)、WiFi/蓝牙/手电筒开关"
        "(set_wifi/set_bluetooth/set_flashlight)、语音朗读(speak)、截屏(screenshot)。"
        "调用后返回手机真实执行结果；status=pending 表示命令已入队但手机还没回报。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS), "description": "要执行的动作"},
            "params": {"type": "object", "description": "动作参数，如 {\"package\":\"com.ss.android.ugc.aweme\"} 或 {\"text\":\"你好\"}"},
            "user_id": {"type": "integer", "description": "目标用户 id（一般不用填，会从会话推断）"},
            "device_name": {"type": "string", "description": "目标设备名（多设备时用）"},
            "wait_seconds": {"type": "integer", "description": "等结果秒数，默认 20"},
        },
        "required": ["action"],
    },
}


registry.register(
    name="phone_control",
    toolset="phone",
    schema=TOOL_SCHEMA,
    handler=lambda args, **kw: phone_control(
        action=args.get("action", ""),
        params=args.get("params") or {},
        user_id=args.get("user_id"),
        device_name=args.get("device_name", ""),
        wait_seconds=args.get("wait_seconds", 20),
        session_id=kw.get("session_id"),
    ),
    check_fn=check_requirements,
    requires_env=["GANIU_BACKEND_URL"],
)

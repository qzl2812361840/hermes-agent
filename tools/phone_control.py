"""phone_control — 嘎妞手机操控工具（2026-09-19 接真链路版）

链路：本工具 → 后端 /api/v1/phone/control（Hermes Key 鉴权）→ 命令队列
      → 用户手机上的嘎妞 App 每 2 秒轮询取走 → 执行（Root/Shizuku/无障碍）→ 回报结果
      → 本工具把真实结果返回给模型

动作白名单与 App 端 PhoneCommandExecutor 完全一致（不列没实现的动作）：
  open_app / press_home / press_back / swipe / tap / type_text / shell /
  set_wifi / set_bluetooth / set_flashlight / speak / screenshot /
  ui_tree / tap_text

2026-10-18 元素树控制方式（与截图识图互补）：
  ④ ui_tree 拿当前界面元素树（来源：无障碍实时树，失败自动切 uiautomator dump 兜底）；
     tap_text 按控件文本定位点击（params.target + 可选 mode=exact/contains，
     a11y 点击失败自动回退树内 center 坐标 input tap）。
     模型操作手机优先 ui_tree → tap_text，只有树不可用/界面纯图片才用截图识图。

2026-10-16 三项修订（对应三处落实）：
  ① 可用性判定从"盲信 GANIU_BACKEND_URL 环境变量"改为"真探测后端可达性"——
     env 没配（k3s 用户容器常态）时自动依次尝试本机 8000 与公网域名，
     任一候选返回 HTTP 响应即视为可用；全不通才隐藏工具。探测走
     /api/v1/phone/status（无鉴权、轻量、且覆盖"域名→官网代理→后端"全链路），
     registry 对 check_fn 有 30s TTL 缓存，探测开销可忽略。
  ② 工具描述写死"截图→看图→点击"闭环，模型看到 schema 就知道怎么用。
  ③ 描述里划清与 adb `mobile` 工具集的边界（本工具控用户手机，mobile 控服务器本机设备）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import httpx

from tools.registry import registry

_ACTIONS = (
    "open_app", "press_home", "press_back", "swipe", "tap", "type_text",
    "shell", "set_wifi", "set_bluetooth", "set_flashlight", "speak", "screenshot",
    "ui_tree", "tap_text",
)

# 探测目标：无鉴权、轻量、且覆盖完整链路（域名 → 官网代理 → 后端 phone API）
_PROBE_PATH = "/api/v1/phone/status"
_PROBE_TIMEOUT = 2.5
_PROBE_TTL = 30.0  # 与 registry 的 check_fn TTL 对齐，双保险

# 公网域名兜底（k3s 用户容器出网只放行 80/443，直连宿主 8000 被 C12 网络策略拦截）
_PUBLIC_FALLBACK = "https://ganiu.paihuai.vip"

_probe_state: dict[str, Any] = {"base": None, "ts": 0.0}


def _backend_candidates() -> list[str]:
    """候选基址：env 优先，其后本机默认，再其后公网域名（去重保序）。"""
    cands: list[str] = []
    env = (os.getenv("GANIU_BACKEND_URL") or "").strip().rstrip("/")
    if env:
        cands.append(env)
    for d in ("http://127.0.0.1:8000", _PUBLIC_FALLBACK):
        if d not in cands:
            cands.append(d)
    return cands


def _probe_backend() -> Optional[str]:
    """返回第一个可达的后端基址；30s 内复用上次结果。任何 HTTP 响应（含 4xx/5xx）都算可达——
    探测证明的是"网络+服务活着"，不是"接口可用"；接口错误由 handler 自己报。"""
    now = time.monotonic()
    if _probe_state["base"] and now - _probe_state["ts"] < _PROBE_TTL:
        return _probe_state["base"]
    for base in _backend_candidates():
        try:
            resp = httpx.get(f"{base}{_PROBE_PATH}", timeout=_PROBE_TIMEOUT)
            if resp.status_code < 500:
                _probe_state["base"] = base
                _probe_state["ts"] = now
                return base
        except Exception:
            continue
    return None


def check_requirements() -> bool:
    """后端可达才暴露工具（真探测，不再盲信 env）。"""
    return _probe_backend() is not None


def _backend_url() -> str:
    """handler 用的基址：探测赢家 → env → 公网域名。"""
    return _probe_backend() or (os.getenv("GANIU_BACKEND_URL") or _PUBLIC_FALLBACK).rstrip("/")


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
            speak（params.text 让手机朗读）、screenshot（截屏并保存到后端）、
            ui_tree（返回当前界面元素树 JSON：nodes[] 含 id/text/desc/rid/cls/clickable/center/bounds，
            source 为 a11y 或 uiautomator；用于按控件文本定位）、
            tap_text（params.target 控件文本，可选 params.mode=exact/contains 默认 contains；
            按文本定位并点击，a11y 失败自动回退树内 center 坐标 input tap）
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
        "操控用户手机（经嘎妞 App 在手机上真实执行，需要手机在线）。"
        "【元素树闭环·首选】要操作界面时：① phone_control(action='ui_tree') 拿当前界面元素树"
        "（nodes[] 带 text/desc/center/bounds）；② 从树里找目标控件文本，"
        "phone_control(action='tap_text', params={'target':'控件文本'}) 按文本点击"
        "（完全匹配不到自动模糊匹配，树里无文本可点才回退 tap 节点 center 坐标）。"
        "【截图识图·兜底】元素树拿不到或界面是图片/画布时：① phone_control(action='screenshot') 截图，"
        "output 里会带截图 URL；② vision_analyze(image_url=<该URL>, "
        "user_prompt='屏幕上有什么？我要操作的元素在哪？给出中心坐标(x,y)') 看画面；"
        "③ phone_control(action='tap', params={'x':..,'y':..}) 点击。坐标不准就重复 ①-③。"
        "【支持的动作】打开应用(open_app)、返回桌面(press_home)、返回键(press_back)、滑动(swipe)、"
        "点击(tap)、输入文字(type_text)、执行 shell(shell)、WiFi/蓝牙/手电筒开关"
        "(set_wifi/set_bluetooth/set_flashlight)、语音朗读(speak)、截屏(screenshot)、"
        "元素树(ui_tree)、按文本点击(tap_text)。"
        "【边界】本工具控的是用户的手机；服务器本机 adb 连接的设备请用 mobile 工具集。"
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
    requires_env=[],
)

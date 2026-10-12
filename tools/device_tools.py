"""device_tools — Windows/Ubuntu 设备插件工具（主人令 2026-10-13）

设计哲学（主人原话）："设计一个插件，Windows 插件、Ubuntu 插件。装到用户
设备上来替代 SSH，直接操控用户设备，就相当于别的厂家设计的电脑 Agent 了。"

链路：本工具 → 后端 /api/v1/device/*（X-Hermes-Key 鉴权）→ 用户电脑上的
      ganiu_device_agent.py 长轮询取命令 → 本机执行 → 回传结果

四个工具（toolset ``devices``，后端不可达时自动隐藏）：
  device_list     列出已配对设备（在线状态/平台/配对时间）
  device_pair     铸造 6 位配对码（5 分钟有效），给用户电脑安装代理用
  device_shell    在指定设备上执行 shell 命令并等待真实结果
  device_revoke   吊销设备（凭证立即失效）

代理脚本下载地址：GET {后端}/api/v1/device/agent.py（单文件、纯标准库）。
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any, Dict
from urllib.parse import urlparse

import httpx

from tools.registry import registry, tool_error, tool_result

_DEFAULT_URL = "http://127.0.0.1:8000"
_ENV_URL = "GANIU_BACKEND_URL"
_PUBLIC_URL_ENV = "GANIU_PUBLIC_URL"
_PUBLIC_URL_FALLBACK = "https://ganiu.paihuai.vip"

_reach_cache: Dict[str, Any] = {"ts": 0.0, "ok": False}
_REACH_TTL = 60.0


def _backend_url() -> str:
    return (os.getenv(_ENV_URL) or _DEFAULT_URL).rstrip("/")


def _public_url() -> str:
    """给用户电脑用的公网地址（Caddy 443 → 8080 网关 → 后端）。"""
    return (os.getenv(_PUBLIC_URL_ENV) or _PUBLIC_URL_FALLBACK).rstrip("/")


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


def _backend_reachable() -> bool:
    """TCP 探活（缓存 60s）：没有可用后端就不暴露这些工具。"""
    now = time.time()
    if now - _reach_cache["ts"] < _REACH_TTL:
        return _reach_cache["ok"]
    ok = False
    try:
        parsed = urlparse(_backend_url())
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=1.5):
            ok = True
    except Exception:
        ok = False
    _reach_cache.update({"ts": now, "ok": ok})
    return ok


def check_requirements() -> bool:
    """后端可达且 HERMES_KEY 可解析时才暴露（避免出现点不动的假工具）。"""
    return _backend_reachable() and bool(_hermes_key())


def _headers() -> Dict[str, str]:
    return {"X-Hermes-Key": _hermes_key(), "Content-Type": "application/json"}


def _call(method: str, path: str, *, payload: Dict[str, Any] | None = None,
          timeout: float = 30.0) -> Dict[str, Any]:
    """统一调用后端 device API，返回 dict；失败时返回 {ok: False, error: ...}。"""
    url = f"{_backend_url()}{path}"
    try:
        resp = httpx.request(method, url, json=payload, headers=_headers(), timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"后端不可达: {exc}"}
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text[:200]
        return {"ok": False, "error": f"后端返回 {resp.status_code}: {detail}"}
    try:
        return resp.json()
    except Exception:
        return {"ok": False, "error": f"响应解析失败: {resp.text[:200]}"}


def _resolve_device(device: str) -> str:
    """device 参数可以是 device_id 或设备名；返回 device_id（找不到返回空串）。"""
    device = (device or "").strip()
    if device.startswith("dev_"):
        return device
    listing = _call("GET", "/api/v1/device/list")
    for item in listing.get("devices", []):
        if item.get("name") == device or item.get("device_id") == device:
            return item.get("device_id", "")
    return ""


# ── handlers ──────────────────────────────────────────────────────────

def device_list_tool(args: Dict[str, Any], **kw) -> str:
    listing = _call("GET", "/api/v1/device/list")
    if not listing.get("devices") and listing.get("count") == 0:
        return tool_result(
            devices=[], count=0,
            hint="还没有已配对设备。让主人在电脑上运行 device_pair 给出的配对命令即可接入。",
        )

    return tool_result(devices=listing.get("devices", []), count=listing.get("count", 0))


def device_pair_tool(args: Dict[str, Any], **kw) -> str:
    name = str(args.get("name") or "未命名设备").strip()
    minted = _call("POST", "/api/v1/device/pair", payload={"name": name})
    if not minted.get("code"):
        return tool_error(minted.get("error") or "配对码铸造失败")
    code = minted["code"]
    public = _public_url()
    install_cmd = (
        f"curl -o ganiu_device_agent.py {public}/api/v1/device/agent.py && "
        f"python ganiu_device_agent.py pair --server {public} --code {code} --name \"{name}\" && "
        f"python ganiu_device_agent.py run"
    )
    return tool_result(
        code=code,
        expires_in=minted.get("expires_in", 300),
        instructions=(
            f"请在您的电脑（Windows/Ubuntu/macOS，需 Python 3.8+，无需 pip install）上运行：\n"
            f"  {install_cmd}\n"
            f"第一步下载并配对（配对码 {code}，5 分钟内有效、一次性）；"
            f"第二步让代理常驻运行（建议配开机自启）。配对成功后用 device_list 即可看到它。"
        ),
    )


def device_shell_tool(args: Dict[str, Any], **kw) -> str:
    device = str(args.get("device") or "").strip()
    command = str(args.get("command") or "").strip()
    if not command:
        return tool_error("command 不能为空")
    if not device:
        return tool_error("device 不能为空（设备名或 device_id，先 device_list 查看）")
    device_id = _resolve_device(device)
    if not device_id:
        return tool_error(f"设备 {device!r} 不存在，先 device_pair 配对或用 device_list 查看")
    timeout = float(args.get("timeout") or 60)
    result = _call(
        "POST", "/api/v1/device/shell",
        payload={"device_id": device_id, "command": command, "timeout": timeout},
        timeout=timeout + 35.0,
    )
    if not result.get("ok") and "stdout" not in result:
        return tool_error(result.get("error") or "命令执行失败")
    out = result.get("stdout") or ""
    err = result.get("stderr") or ""
    if len(out) > 20000:
        out = out[:20000] + "\n…[输出截断]"
    if len(err) > 5000:
        err = err[:5000] + "\n…[输出截断]"
    return tool_result(ok=bool(result.get("ok")), stdout=out, stderr=err,
                       exit_code=result.get("exit_code"), device=device_id)


def device_revoke_tool(args: Dict[str, Any], **kw) -> str:
    device = str(args.get("device") or "").strip()
    if not device:
        return tool_error("device 不能为空（设备名或 device_id）")
    device_id = _resolve_device(device)
    if not device_id:
        return tool_error(f"设备 {device!r} 不存在")
    result = _call("POST", "/api/v1/device/revoke", payload={"device_id": device_id})
    if not result.get("ok"):
        return tool_error(result.get("error") or "吊销失败")
    return tool_result(ok=True, revoked=device_id, hint="该设备凭证已失效，代理将无法再连接")


# ── schemas ───────────────────────────────────────────────────────────

_DEVICE_SCHEMAS = {
    "device_list": {
        "name": "device_list",
        "description": (
            "列出已配对的电脑设备（Windows/Ubuntu/macOS，装嘎妞代理替代 SSH）。"
            "返回每台设备的在线状态、平台、配对时间。没有设备时给出接入指引。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "device_pair": {
        "name": "device_pair",
        "description": (
            "配对新电脑：铸造 6 位配对码（5 分钟有效）并给出用户电脑上要运行的安装命令。"
            "当主人说'连接我的电脑/给你我的电脑权限/控制我电脑'时调用本工具，"
            "然后把 instructions 里的命令原样转告主人执行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "设备备注名，如'我的台式机'、'公司笔记本'"},
            },
            "required": [],
        },
    },
    "device_shell": {
        "name": "device_shell",
        "description": (
            "在主人的电脑上执行 shell 命令（Windows 走 cmd，其余走 bash）并等待真实结果。"
            "这是直接操控用户设备的总入口：装软件、改配置、跑脚本、看文件都可以。"
            "device 填设备名或 device_id（先 device_list）。毁灭性命令会被服务端拦截。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "目标设备名或 device_id"},
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"},
            },
            "required": ["device", "command"],
        },
    },
    "device_revoke": {
        "name": "device_revoke",
        "description": "吊销一台已配对设备：凭证立即失效，代理无法再连接。主人说'断开/吊销某台电脑'时用。",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "要吊销的设备名或 device_id"},
            },
            "required": ["device"],
        },
    },
}

_HANDLERS = {
    "device_list": device_list_tool,
    "device_pair": device_pair_tool,
    "device_shell": device_shell_tool,
    "device_revoke": device_revoke_tool,
}

for _name, _schema in _DEVICE_SCHEMAS.items():
    registry.register(
        name=_name,
        toolset="devices",
        schema=_schema,
        handler=_HANDLERS[_name],
        check_fn=check_requirements,
    )

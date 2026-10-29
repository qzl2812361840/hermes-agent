"""device_tools — Windows/Ubuntu 设备插件工具（主人令 2026-10-13）

设计哲学（主人原话）："设计一个插件，Windows 插件、Ubuntu 插件。装到用户
设备上来替代 SSH，直接操控用户设备，就相当于别的厂家设计的电脑 Agent 了。"

链路：本工具 → 后端 /api/v1/device/*（X-Hermes-Key 鉴权）→ 用户电脑上的
      ganiu_device_agent.py 长轮询取命令 → 本机执行 → 回传结果

五个工具（toolset ``devices``，后端不可达时自动隐藏）：
  device_list        列出已配对设备（在线状态/平台/配对时间）
  device_pair        铸造 6 位配对码（5 分钟有效），给用户电脑安装代理用
  device_shell       在指定设备上执行 shell 命令并等待真实结果
  device_desktop     操控用户电脑桌面：UIA 元素路线（默认）+ cua-driver 视觉兜底
                     （2026-10-25 新增；2026-10-29 元素优先双路线，学 Windows-MCP/微软 UFO²）
  device_revoke      吊销设备（凭证立即失效）

代理脚本下载地址：GET {后端}/api/v1/device/agent.py（单文件、纯标准库）。

device_desktop 设计原则（与 Hermes computer_use 同构）：
  · 动作白名单：只读（capture/list/get_screen_size/wait）免审批；
    写入（click/type/key/...）走 tools.approval 审批门
    （device:<device_id>:<action> scope；无审批环境时降级放行并记日志）
  · device_shell 同样过审批门（2026-10-26 补豁口，此前仅服务端 blocklist）：
    命令级 scope（device:shell:<device_id>:<命令哈希>）；服务端毁灭性 blocklist
    保留为硬拦截，与用户级审批两层并存
  · 截图以 _multimodal 形式回传（image_url data URI），绝不把 base64 当文本
    喂模型——那是上下文炸弹
  · cua-driver 未安装时设备端返回官方安装引导，原样转告主人手动安装
    （绝不自动静默安装）
  · 凭证操作决策权门（2026-10-26 主人令）：type/set_value 带 is_credential=true
    时先查 /api/v1/credential-authority/status——建议模式退回给主人自己输；
    agent 档+有效授权才代填；支付/银行/证券硬线一律不碰。授权管理用
    credential_authority 工具（“密码你来填”→ grant session）
  · 人接管围栏（2026-10-26 主人令）：一旦因凭证交还主人，该设备进入围栏——
    存现场快照（断点）、接管期 Agent 禁回看（不截屏）禁操作；主人说“好了”后
    带 takeover_done=true 解围栏并拿回断点，从断点续跑。10 分钟无响应自动解围
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import httpx

from tools.registry import registry, tool_error, tool_result

_DEFAULT_URL = "http://127.0.0.1:8000"
_ENV_URL = "GANIU_BACKEND_URL"
_PUBLIC_URL_ENV = "GANIU_PUBLIC_URL"
_PUBLIC_URL_FALLBACK = "https://ganiu.paihuai.vip"

# 桌面写入类动作（需审批）；只读动作见后端 DESKTOP_READONLY_ACTIONS
_DESKTOP_WRITE_ACTIONS = {
    "click", "double_click", "right_click", "type", "key",
    "drag", "scroll", "set_value", "focus_app",
    "uia_act",  # UIA 元素写入（invoke/toggle/expand/collapse/select/focus/set_value）
}

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
    # 2026-10-26 补豁口：shell 命令也过审批门（此前只有服务端 blocklist，无审批）。
    # 服务端毁灭性 blocklist 保留（那是硬拦截）；本条是用户级授权，两层并存。
    denial = _request_shell_approval(device_id, device, command)
    if denial is not None:
        return denial
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


def _request_desktop_approval(device_id: str, device_name: str, action: str, params: Dict[str, Any]) -> str | None:
    """写入类桌面动作的审批门。None = 放行；返回 JSON 错误串 = 拒绝。

    复用 Hermes 共享审批门 tools.approval._run_approval_gate（与 computer_use 同构）；
    导入失败（老版本 Hermes）时降级：放行但打日志——不因审批模块缺失破坏设备链路。
    """
    try:
        from tools.approval import _run_approval_gate
    except Exception:
        return None  # 无审批环境：降级放行（与 device_shell 现状一致）
    summary = json.dumps(params, ensure_ascii=False)[:200] if params else ""
    result = _run_approval_gate(
        pattern_key=f"device:{device_id}:{action}",
        description=f"Allow device_desktop to perform `{action}` on {device_name!r}?",
        display_target=f"device_desktop: {action} {summary}".strip(),
        subject=f"device_desktop `{action}` on {device_name!r} requires approval",
        noun="desktop actions",
        advice="Find an alternative approach that avoids driving the user's desktop.",
        fail_closed_when_no_human=True,
        no_human_block_message=(
            f"BLOCKED: device_desktop `{action}` requires approval but no interactive user or "
            "gateway is present to approve it."
        ),
    )
    if result.get("approved"):
        return None
    return json.dumps({"error": result.get("message") or "denied by user", "action": action})


def _request_shell_approval(device_id: str, device_name: str, command: str) -> str | None:
    """device_shell 的审批门（2026-10-26 补豁口）。None = 放行；返回 JSON 错误串 = 拒绝。

    与 device_desktop 同构：复用 Hermes 共享审批门；cron/无人上下文由审批体系
    自身识别（_is_cron_approval_context），不在此特殊处理。
    粒度：命令级（同一设备同一条命令一个 scope，key = 设备 + 命令哈希）——
    永久批准后该命令在此设备上不再问；嫌烦可配永久白名单 glob 免打扰。
    """
    try:
        from tools.approval import _run_approval_gate
    except Exception:
        return None  # 无审批环境：降级放行（维持既有链路不中断）
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    result = _run_approval_gate(
        pattern_key=f"device:shell:{device_id}:{digest}",
        description=f"Allow device_shell to run a command on {device_name!r}?",
        display_target=f"device_shell on {device_name!r}: {command[:200]}",
        subject=f"device_shell command on {device_name!r} requires approval",
        noun="shell commands on your device",
        advice="Find an alternative approach that avoids running commands on the user's device.",
        autoapprove_log_prefix="device_shell in non-interactive non-gateway context",
        fail_closed_when_no_human=True,
        no_human_block_message=(
            "BLOCKED: device_shell requires approval but no interactive user or "
            "gateway is present to approve it."
        ),
    )
    if result.get("approved"):
        return None
    return json.dumps({"error": result.get("message") or "denied by user", "command": command[:200]})


def _session_ctx() -> tuple[str, str]:
    """(user_id, session_key) — user_id 优先取 HERMES_SESSION_USER_ID，
    否则从嘎妞 session_key（profile:user_id:agent_id:session_id）解析第二段。"""
    uid = skey = ""
    try:
        from gateway.session_context import get_session_env
        uid = get_session_env("HERMES_SESSION_USER_ID", "") or ""
        skey = get_session_env("HERMES_SESSION_KEY", "") or ""
    except Exception:
        uid = os.getenv("HERMES_SESSION_USER_ID", "") or ""
        skey = os.getenv("HERMES_SESSION_KEY", "") or ""
    if not uid and skey:
        parts = skey.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            uid = parts[1]
    return uid, skey


def _check_credential_authority(target: str) -> Optional[Dict[str, Any]]:
    """凭证操作（输密码/验证码）前的决策权门（2026-10-26 主人令）。

    None = 放行（agent 档 + 有效授权 + 非支付硬线）；返回 dict = 交还主人。
    查不到用户上下文或后端异常时按建议模式处理（安全侧兜底：拿不准就不代填）。
    """
    uid, skey = _session_ctx()
    if not uid:
        return {
            "error": "拿不到用户上下文，无法确认决策权；按建议模式处理：请主人自己输入密码/验证码",
            "credential_denied": True,
            "instruction": "请把这一步交还主人本人输入。若主人说“密码你来填”，即有授权，可重试。",
        }
    q = f"/api/v1/credential-authority/status?user_id={uid}&session_key={quote(skey)}"
    if target:
        q += f"&target={quote(target)}"
    resp = _call("GET", q)
    if not resp.get("ok"):
        return {
            "error": f"决策权服务异常（{resp.get('error')}）；按建议模式处理：请主人自己输入密码/验证码",
            "credential_denied": True,
            "instruction": "请把这一步交还主人本人输入。",
        }
    if resp.get("may_fill"):
        return None
    reason = resp.get("reason") or "当前不允许 Agent 代填密码/验证码"
    denied = "支付" in reason or "硬线" in reason
    return {
        "error": reason,
        "credential_denied": True,
        "hardline": denied,
        "instruction": (
            "这是支付/银行/证券类页面：主人令硬线，任何授权都不可覆盖，请主人本人输入。"
            if denied else
            "请把这一步交还主人：告诉主人现在需要他本人输入密码/验证码；"
            "若主人说“密码你来填/你来替我输入”，即有授权，可继续代填。"
        ),
    }


# ── 人接管围栏（2026-10-26 主人令：接管存现场快照 / Done 断点续跑 / 接管期禁回看） ──
# 语义：一次凭证交还 = 主人接管这台设备。接管期间 Agent 不许看（截屏）也不许动
# （点击/输入）——主人在输密码，回看=偷窥。主人说"好了/填好了/继续"后，Agent 带
# takeover_done=true 复位围栏，同时拿回交还时的现场快照（断点）接着做。
_takeover_fences: Dict[str, Dict[str, Any]] = {}   # device_id -> 快照
_TAKEOVER_FENCE_TTL = 10 * 60                      # 10 分钟自动过期，防主人不回来卡死


def _fence_snapshot(device_id: str) -> Optional[Dict[str, Any]]:
    """该设备是否处于人接管围栏中；返回现场快照（过期自动清除）。"""
    snap = _takeover_fences.get(device_id)
    if snap and time.time() - snap["at"] > _TAKEOVER_FENCE_TTL:
        _takeover_fences.pop(device_id, None)
        return None
    return snap


def _fence_enter(device_id: str, action: str, target: str, params: Dict[str, Any]) -> None:
    """交还主人 = 进入接管围栏，存现场快照（断点）。"""
    _takeover_fences[device_id] = {
        "action": action, "target": target,
        "params": {k: v for k, v in (params or {}).items() if k != "text"},  # 快照不存待输文本
        "at": time.time(),
    }


def _fence_clear(device_id: str) -> Optional[Dict[str, Any]]:
    """Done 断点续跑：清围栏并取回快照。"""
    return _takeover_fences.pop(device_id, None)


def device_desktop_tool(args: Dict[str, Any], **kw) -> str:
    """操控用户电脑桌面：UIA 元素路线（uia_snapshot/uia_act，默认）+ cua-driver 视觉兜底。
    Windows 上元素优先（快、省、准），UIA 拿不到元素（UIA_MISS）时落回截图视觉。"""
    device = str(args.get("device") or "").strip()
    action = str(args.get("action") or "").strip()
    params = args.get("params") or {}
    if not device:
        return tool_error("device 不能为空（设备名或 device_id，先 device_list 查看）")
    if not action:
        return tool_error("action 不能为空（capture/click/type/key/drag/scroll/focus_app/list_windows/...）")
    if not isinstance(params, dict):
        return tool_error("params 必须是对象（driver 工具参数，如 {\"x\": 100, \"y\": 200}）")
    device_id = _resolve_device(device)
    if not device_id:
        return tool_error(f"设备 {device!r} 不存在，先 device_pair 配对或用 device_list 查看")

    # ── 人接管围栏（2026-10-26 主人令） ──
    # 围栏中：主人正在这台设备上操作（多半在输密码），Agent 不看（禁截屏）也不动。
    # 唯一的通行证是 takeover_done=true（主人说"好了/填好了/继续"后才允许发）——
    # 它解除围栏，本次动作照常执行，并把交还时的现场快照（断点）挂回结果里。
    _resume_from: Optional[Dict[str, Any]] = None
    if args.get("takeover_done"):
        snap = _fence_clear(device_id)
        if snap is not None:
            _resume_from = {"action": snap.get("action"), "target": snap.get("target"),
                            "handed_over_seconds_ago": int(time.time() - snap["at"])}
    elif _fence_snapshot(device_id) is not None:
        snap = _fence_snapshot(device_id)
        return json.dumps({
            "error": "主人正在接管这台设备（上一步交还他在输入），围栏中：不截屏、不操作",
            "takeover_active": True,
            "checkpoint": {"action": snap.get("action"), "target": snap.get("target")},
            "instruction": (
                "等主人明确说「好了/填好了/继续」后，才带 takeover_done=true 重试本工具；"
                "在那之前不要对这台设备做任何操作（包括截屏）。"
            ),
        }, ensure_ascii=False)

    # 凭证操作决策权门（2026-10-26 主人令）：要输密码/验证码时先查授权态。
    # 只拦 is_credential=true 的 type/set_value——普通文本输入不打扰，
    # 是否涉密由模型自报（它决定要输什么，最清楚）。
    # 2026-10-29：uia_act 的 set_value 子动作同样能填输入框，一并纳入决策权门
    #（否则元素路线会绕过凭证门——堵死这个口子）。
    _is_cred_action = action in ("type", "set_value") or (
        action == "uia_act" and str(params.get("act") or "invoke") == "set_value"
    )
    if _is_cred_action and args.get("is_credential"):
        denial = _check_credential_authority(str(args.get("authority_target") or ""))
        if denial is not None:
            # 交还主人 = 进入接管围栏（存现场快照）；主人弄完说"好了"才 takeover_done 复位
            _fence_enter(device_id, action, str(args.get("authority_target") or ""), params)
            denial["takeover_active"] = True
            denial["instruction"] += "（已进入接管围栏：主人操作完成前，不要对这台设备截屏或操作；主人说「好了」后带 takeover_done=true 继续）"
            return json.dumps(denial, ensure_ascii=False)

    # 写入类动作审批门（只读动作 capture/list/get_screen_size/wait 免审批）
    if action in _DESKTOP_WRITE_ACTIONS:
        denial = _request_desktop_approval(device_id, device, action, params)
        if denial is not None:
            return denial

    timeout = float(args.get("timeout") or 60)
    result = _call(
        "POST", "/api/v1/device/desktop",
        payload={"device_id": device_id, "action": action, "params": params, "timeout": timeout},
        timeout=timeout + 35.0,
    )
    if not result.get("ok") and "stdout" not in result:
        return tool_error(result.get("error") or "桌面动作执行失败")
    out = result.get("stdout") or ""
    err = result.get("stderr") or ""
    if len(out) > 20000:
        out = out[:20000] + "\n…[输出截断]"
    if len(err) > 5000:
        err = err[:5000] + "\n…[输出截断]"
    image_b64 = result.get("image_b64") or ""
    image_mime = result.get("image_mime") or "image/png"

    # 截图走 _multimodal（data URI），绝不把 base64 当文本——那是上下文炸弹
    if image_b64:
        text = out or f"已截取 {device!r} 的屏幕（{action}）。"
        summary = (
            f"{text}\n[截图 {len(image_b64) * 3 // 4 // 1024}KB 已作为图像随附；"
            "若模型看不到图，请改用 ax/元素索引方式操作，不要重试 capture]"
        )
        if _resume_from:
            summary += f"\n[续跑断点] 交还前你在: {_resume_from['action']} → {_resume_from['target'] or '?'}"
        return {
            "_multimodal": True,
            "content": [
                text,
                f"data:{image_mime};base64,{image_b64}",
            ],
            "text_summary": summary,
        }
    extra = {"checkpoint": _resume_from} if _resume_from else {}
    return tool_result(ok=bool(result.get("ok")), action=action, stdout=out, stderr=err,
                       exit_code=result.get("exit_code"), device=device_id, **extra)


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
    "device_desktop": {
        "name": "device_desktop",
        "description": (
            "操控主人电脑的桌面（GUI），两套路线并存（Windows 双路线/其他平台视觉路线）：\n"
            "【元素路线·默认】uia_snapshot 读前台窗口的控件树——每个元素带序号/类型/名称/"
            "automationId/可执行能力(p 字段)/坐标，零截图零视觉开销，又快又准；"
            "再用 uia_act 按名称(find_name 子串)或 automation_id(find_automation_id)定位元素，"
            "执行 invoke/toggle/expand/collapse/select/focus/set_value。"
            "标准 Windows 界面（记事本/计算器/资源管理器/Office/系统对话框等）优先走这条。\n"
            "【视觉路线·兜底】元素树拿不到（UIA_MISS/UIA_ERROR/超时，Electron/自绘/游戏界面常见）"
            "时，用 snapshot(params.capture=true) 或 capture 截图走视觉："
            "看图判断坐标再 click/type/scroll（cua-driver 后台执行，不抢主人鼠标焦点）。\n"
            "当主人说'点一下/打开某个软件/帮我操作电脑界面/看看我屏幕'等 GUI 意图时用本工具"
            "（命令行能办的仍优先 device_shell）。"
            "只读动作 capture/list_windows/list_apps/get_screen_size/wait/snapshot/uia_snapshot 免审批；"
            "写入动作（click/type/key/.../uia_act）需主人审批。"
            "【操作节奏】先 uia_snapshot 拿前台窗口元素树（Windows 默认路线）；"
            "目标软件不在前台时先 focus_app，再动手；元素路线失败再视觉兜底。"
            "首次使用若设备端没装 cua-driver 且需要视觉路线，会把官方安装命令原样转告主人手动安装"
            "（元素路线不依赖 cua-driver，没装也能用）。\n"
            "【密码/验证码】要输入密码或验证码时：is_credential 必须传 true，"
            "并尽量把 authority_target 填上当前软件名/窗口标题/网址。系统会先查决策权——"
            "默认建议模式下会退回给主人自己输（你就照实告诉主人）；支付/银行/证券类"
            "页面永远是主人自己输。若主人刚说过“密码你来填”，即有授权，可正常代填。\n"
            "【人接管与续跑】一旦因密码/验证码交还主人，这台设备就进入接管围栏："
            "主人操作完成前，不要对它做任何操作（包括截屏——他在输密码，不回看）。"
            "主人明确说「好了/填好了/继续」后，带 takeover_done=true 调一次本工具"
            "解除围栏并拿回断点快照，再从断点接着做。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "目标设备名或 device_id（先 device_list）"},
                "action": {
                    "type": "string",
                    "enum": [
                        "uia_snapshot", "uia_act",
                        "capture", "click", "double_click", "right_click",
                        "type", "key", "drag", "scroll", "set_value",
                        "focus_app", "list_apps", "list_windows",
                        "get_screen_size", "wait", "snapshot",
                    ],
                    "description": (
                        "桌面动作。uia_snapshot=读前台窗口控件树（Windows 默认第一步，免审批）；"
                        "uia_act=按元素操作（写入，需审批）；"
                        "capture=截屏带回图；click/type/key 等视觉路线写入操作（需审批）；"
                        "list_windows/list_apps/get_screen_size 只读；"
                        "snapshot=一体化现场快照（前台窗口+窗口列表+应用+屏幕规格一次拿全，"
                        "params.capture=true 顺带截屏）。"
                        "顺序：uia_snapshot → uia_act，拿不到元素再 capture/视觉。"
                    ),
                },
                "params": {
                    "type": "object",
                    "description": (
                        "参数按 action 透传。\n"
                        "uia_snapshot：{\"scope\":\"foreground\"}(默认)|\"desktop\"，"
                        "可选 max_items/budget_ms/max_depth。\n"
                        "uia_act：{\"find_name\":\"确定\"}(名称子串,不区分大小写) 或 "
                        "{\"find_automation_id\":\"btnOk\"}(精确)；命中多个用 find_index(默认0)选；"
                        "act=\"invoke\"(默认)|\"toggle\"|\"expand\"|\"collapse\"|\"select\"|\"focus\"|\"set_value\"；"
                        "set_value 时 {\"value\":\"要写入的文本\"}。"
                        "注意 uia_act 用名称/automationId 重新定位元素，别传坐标。\n"
                        "视觉路线：click {\"x\":100,\"y\":200}；type {\"text\":\"hello\"}；"
                        "key {\"keys\":\"ctrl+c\"}；scroll {\"x\":0,\"y\":-3}；"
                        "drag {\"x1\":0,\"y1\":0,\"x2\":100,\"y2\":100}；"
                        "focus_app {\"app\":\"Chrome\"}；capture {\"max_width\":1280}。"
                    ),
                },
                "is_credential": {
                    "type": "boolean",
                    "description": (
                        "本次 type/set_value 要输入的是密码或验证码时传 true——"
                        "系统会先查决策权（建议模式退回给主人；支付硬线一律不代填）。"
                        "普通文本输入不要传（传了也不会拦，但别滥用）。"
                    ),
                },
                "authority_target": {
                    "type": "string",
                    "description": (
                        "is_credential=true 时尽量填：当前软件名/窗口标题/网址"
                        "（如 \"Chrome\"、\"网上银行\"、\"mail.163.com\"），"
                        "用于支付硬线判定，避免误伤正常密码。"
                    ),
                },
                "takeover_done": {
                    "type": "boolean",
                    "description": (
                        "人接管复位开关：上一步因密码/验证码交还主人后，只有主人明确说"
                        "「好了/填好了/继续」时才传 true——解除该设备的接管围栏并返回"
                        "断点快照，从断点续跑。主人没表态前绝不传（围栏会拒绝任何操作）。"
                    ),
                },
                "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"},
            },
            "required": ["device", "action"],
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
    "device_desktop": device_desktop_tool,
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

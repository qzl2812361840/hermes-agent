"""三档终端命令权限策略（shell_policy）

为 Hermes 的 shell 类工具提供分级管控：
- OFF  ：策略未启用（默认，保持既有行为，零影响）
- SAFE ：白名单 + 路径围栏（设计借鉴 dsh-go ShellGuard 的防绕过思路，自行实现）
- FULL ：全放开，但永久禁止组仍然拦截

防绕过规则（SAFE 档）：
 1. 命令替换 $( ) ${} 与反引号一律拒绝（要跑脚本请升档并确认）
 2. 按 ; && || | 拆段，逐段独立判定
 3. 路径统一规范化后再判定；写类命令出现 `..` 直接拒绝
 4. cd 目标限定在安全目录白名单内
 5. 重定向目标按写操作检查
 6. 受保护目录（相册/媒体/应用私有数据）纯只读，禁止写入与搬出
 7. 源路径同样检查（mv/cp 从受保护目录搬出 = 变相删除）
 8. find -delete/-exec、pm clear 等高危单命令单独封禁
 9. /data 根目录整体禁区，仅放行 /data/local/tmp 与应用私有目录

审计：环形队列保留最近 300 条判定记录，可查最近 N 条。
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from .policy_version import policy_version

__all__ = [
    "ShellMode",
    "ShellVerdict",
    "ALLOW",
    "DENY",
    "CONFIRM",
    "judge_shell_command",
    "ShellAuditLog",
    "WhitelistStore",
    "SHELL_RISK_PATTERNS",
]

ALLOW = "allow"
DENY = "deny"
CONFIRM = "confirm"


class ShellMode(str, Enum):
    OFF = "off"
    SAFE = "safe"
    FULL = "full"


# ---------------------------------------------------------------------------
# 命令目录
# ---------------------------------------------------------------------------

#: 永久禁止组：不可逆/破坏性命令，任何档位不放行
LOCKED_COMMANDS = frozenset(
    {
        "dd", "mkfs", "mkfs.ext4", "mkfs.vfat", "mke2fs",
        "fastboot", "reboot", "shutdown", "halt", "poweroff",
        "busybox", "flash_image", "erase_image",
    }
)

#: 脚本解释器：SAFE 档每次需用户确认
SCRIPT_COMMANDS = frozenset(
    {"sh", "bash", "dash", "zsh", "python", "python3", "node", "perl", "awk", "xargs", "eval", "source"}
)

#: 高危单命令：SAFE 档默认禁止（即使语义像只读）
SAFE_LOCKED_SINGLETONS = frozenset(
    {"pm", "am", "find", "chmod", "chown", "mount", "umount", "setprop", "getenforce", "setenforce"}
)
# 说明：pm/am 在原设计中单独封禁的是 `pm clear` 等破坏性子命令；find 封禁 -delete/-exec。
# 其余列入 SAFE_LOCKED_SINGLETONS 的命令默认不在白名单，需显式开启。

#: 白名单目录：按组分类，SAFE 档默认只开 readonly 组
CATALOG: dict[str, dict[str, list[str]]] = {
    "readonly": {
        "label": "只读查看",
        "default_on": True,
        "commands": [
            "ls", "cat", "head", "tail", "grep", "egrep", "wc", "file", "stat",
            "du", "df", "ps", "top", "uptime", "free", "whoami", "id", "uname",
            "getprop", "dumpsys", "logcat", "dmesg", "printenv", "env", "date",
            "which", "ping", "ip", "netstat",
        ],
    },
    "files": {
        "label": "文件读写",
        "default_on": False,
        "commands": [
            "touch", "mkdir", "cp", "mv", "rm", "rmdir", "ln",
            "echo", "tee", "sed", "truncate", "tar", "zip", "unzip", "gzip", "gunzip",
        ],
    },
    "system": {
        "label": "系统控制",
        "default_on": False,
        "commands": ["svc", "settings", "cmd", "input", "screencap", "screenrecord", "wm", "am", "pm", "monkey"],
    },
    "network": {
        "label": "网络传输",
        "default_on": False,
        "commands": ["curl", "wget", "scp", "rsync", "ssh", "ftpget", "ftpput"],
    },
    "packages": {
        "label": "包管理",
        "default_on": False,
        "commands": ["apt", "apt-get", "dpkg", "pip", "pip3", "npm", "pkg"],
    },
    "scripts": {
        "label": "脚本执行",
        "default_on": False,
        "commands": sorted(SCRIPT_COMMANDS),
    },
}

#: SAFE 档默认锁死的整组
SAFE_LOCKED_GROUPS = frozenset({"scripts"})

#: cd 允许进入的安全目录前缀
SAFE_CD_PREFIXES = ("/sdcard", "/storage/emulated/0", "/data/local/tmp", "/tmp", "/home")

#: 受保护目录前缀：纯只读（相册、媒体与应用私有数据）
PROTECTED_READONLY_PREFIXES = (
    "/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Movies", "/sdcard/Music",
    "/storage/emulated/0/DCIM", "/storage/emulated/0/Pictures",
    "/storage/emulated/0/Movies", "/storage/emulated/0/Music",
    "/data/data", "/data/media",
)

#: /data 根目录禁区中仅放行的前缀
DATA_ALLOWED_PREFIXES = ("/data/local/tmp", "/data/data/com.termux", "/data/data/")

#: 写类命令集合（涉及这些命令时触发写路径检查）
WRITE_COMMANDS = frozenset(
    {"touch", "mkdir", "cp", "mv", "rm", "rmdir", "ln", "echo", "tee", "sed",
     "truncate", "tar", "zip", "unzip", "gzip", "gunzip", "chmod", "chown",
     "mount", "umount", "setprop", "dd"}
)

#: 脚本/词法风险模式（借鉴 Droidance ToolRiskPolicy.assessShell 的词法审查思路，规则表自行维护）
SHELL_RISK_PATTERNS: list[tuple[str, str]] = [
    ("命令替换", r"\$\(|\$\{|`"),
    ("设备写入", r"\bdd\b|\bmkfs|\bfastboot|\breboot\b|\bshutdown\b"),
    ("权限自改", r"\bchmod\s+777|\bchown\s+root"),
    ("卸载/清数据", r"\bpm\s+clear\b|\bpm\s+uninstall\b"),
    ("递归强删", r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)"),
    ("分区/引导", r"\bmount\s+-o\s+remount|\bflash_"),
    ("发短信/打电话", r"\bam\s+start.*sms|content://sms|tel:"),
]


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------

@dataclass
class ShellVerdict:
    action: str  # allow / deny / confirm
    reason: str = ""
    segments: list = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _split_segments(cmd: str) -> list[list[str]]:
    """按 ; && || | 拆段（shlex 连标点模式），返回 token 列表的列表。"""
    try:
        tokens = list(shlex.shlex(cmd, posix=True, punctuation_chars=True))
    except ValueError:
        # 引号不闭合等：交由上层拒绝
        raise
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and all(c in ";|&" for c in tok):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments or [[]]


def _norm(path: str) -> str:
    return posixpath.normpath(path)


def _is_under(path: str, prefixes: Iterable[str]) -> bool:
    p = _norm(path)
    return any(p == pre or p.startswith(pre.rstrip("/") + "/") for pre in prefixes)


def _base_command(tokens: list[str]) -> str:
    if not tokens:
        return ""
    raw = tokens[0]
    # env 前缀穿透：FOO=bar cmd ...
    if "=" in raw and not raw.startswith("/"):
        for tok in tokens:
            if "=" in tok and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
                continue
            raw = tok
            break
    return posixpath.basename(raw)


def _paths_in(tokens: list[str]) -> list[str]:
    """粗提取 token 中像绝对路径的元素。"""
    return [t for t in tokens[1:] if t.startswith("/") or t.startswith(("./", "../"))]


def _has_redirect(tokens: list[str]) -> Optional[str]:
    for i, tok in enumerate(tokens):
        if tok in (">", ">>") and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _segment_verdict(tokens: list[str], mode: ShellMode, allowed: frozenset) -> ShellVerdict:
    base = _base_command(tokens)

    # 规则：永久禁止组
    if base in LOCKED_COMMANDS:
        return ShellVerdict(DENY, f"永久禁止命令: {base}")

    if mode is ShellMode.FULL:
        return ShellVerdict(ALLOW, "full 档")

    # 以下均为 SAFE 档
    if base in SAFE_LOCKED_SINGLETONS and base not in allowed:
        return ShellVerdict(DENY, f"默认封禁命令（可白名单开启）: {base}")

    # 高危子命令细分
    if base == "find":
        if "-delete" in tokens or "-exec" in tokens:
            return ShellVerdict(DENY, "find -delete/-exec 禁止")
        if base not in allowed:
            return ShellVerdict(DENY, "find 未在白名单")
    if base == "pm" and len(tokens) > 1 and tokens[1] in ("clear", "uninstall"):
        return ShellVerdict(DENY, "pm clear/uninstall 禁止")

    # 脚本解释器：确认制
    if base in SCRIPT_COMMANDS:
        if base not in allowed:
            return ShellVerdict(CONFIRM, f"脚本命令需确认: {base}")

    # 白名单校验
    if base not in allowed:
        return ShellVerdict(DENY, f"命令不在白名单: {base}")

    is_write = base in WRITE_COMMANDS or base in ("mv", "cp", "tar", "zip")
    paths = _paths_in(tokens)
    redir = _has_redirect(tokens)
    if redir:
        paths.append(redir)
        # 规则 5：重定向目标 = 写操作
        if _is_under(redir, PROTECTED_READONLY_PREFIXES):
            return ShellVerdict(DENY, f"重定向目标位于受保护目录: {redir}")

    for p in paths:
        # 规则 3：路径含 ..
        if ".." in p.split("/"):
            if is_write or redir or base in ("mv", "cp"):
                return ShellVerdict(DENY, f"写操作路径含 .. : {p}")
            p = _norm(p)
        # 规则 9：/data 禁区
        if p.startswith("/data") and not _is_under(p, DATA_ALLOWED_PREFIXES):
            return ShellVerdict(DENY, f"/data 禁区路径: {p}")
        # 规则 6/7：受保护目录只准看——写入、搬出、打包一律拒绝
        if base in ("mv", "cp", "tar", "zip") and _is_under(p, PROTECTED_READONLY_PREFIXES):
            return ShellVerdict(DENY, f"受保护目录禁止搬动/写入: {p}")
        if is_write and _is_under(p, PROTECTED_READONLY_PREFIXES):
            return ShellVerdict(DENY, f"受保护目录禁止写操作: {p}")

    # 规则 4：cd 限定安全目录
    if base == "cd" and len(tokens) > 1:
        target = tokens[1]
        if target.startswith("/") and not _is_under(target, SAFE_CD_PREFIXES):
            return ShellVerdict(DENY, f"cd 目标超出安全目录: {target}")

    # 词法风险模式（confirm 级提示）
    joined = " ".join(tokens)
    for label, pat in SHELL_RISK_PATTERNS:
        if re.search(pat, joined):
            return ShellVerdict(CONFIRM, f"风险特征: {label}")

    return ShellVerdict(ALLOW, "白名单放行")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def judge_shell_command(
    cmd: str,
    mode: ShellMode | str = ShellMode.OFF,
    allowed: Iterable[str] | None = None,
    *,
    audit: Optional["ShellAuditLog"] = None,
) -> ShellVerdict:
    """判定一条 shell 命令。

    mode=OFF 时直接放行（策略未启用，保持既有行为）。
    allowed 为 SAFE/FULL 档白名单集合；为 None 时加载 WhitelistStore 默认值。
    """
    if isinstance(mode, str):
        try:
            mode = ShellMode(mode)
        except ValueError:
            mode = ShellMode.OFF

    if mode is ShellMode.OFF:
        return ShellVerdict(ALLOW, "策略未启用")

    # 快速整体风险特征（含命令替换）
    for label, pat in SHELL_RISK_PATTERNS[:1]:  # 命令替换
        if re.search(pat, cmd):
            verdict = ShellVerdict(DENY if mode is ShellMode.SAFE else ALLOW,
                                   f"命令替换: {label}" if mode is ShellMode.SAFE else "full 档")
            if audit is not None:
                audit.record(cmd, mode, verdict)
            return verdict

    if allowed is None:
        store = WhitelistStore()
        allowed = frozenset(store.allowed_for(mode))

    try:
        segments = _split_segments(cmd)
    except ValueError as exc:
        verdict = ShellVerdict(DENY, f"命令解析失败: {exc}")
        if audit is not None:
            audit.record(cmd, mode, verdict)
        return verdict

    for tokens in segments:
        if not tokens:
            continue
        verdict = _segment_verdict(tokens, mode, frozenset(allowed))
        if not verdict.allowed:
            verdict.segments = [" ".join(t) for t in segments]
            if audit is not None:
                audit.record(cmd, mode, verdict)
            return verdict

    verdict = ShellVerdict(ALLOW, "全部段放行")
    if audit is not None:
        audit.record(cmd, mode, verdict)
    return verdict


# ---------------------------------------------------------------------------
# 审计与白名单存储
# ---------------------------------------------------------------------------

class ShellAuditLog:
    """环形审计队列：保留最近 300 条判定。可选 JSONL 落盘。"""

    CAPACITY = 300

    def __init__(self, path: str | None = None, capacity: int = CAPACITY):
        self._entries: deque = deque(maxlen=capacity)
        self._path = path

    def record(self, cmd: str, mode: ShellMode, verdict: ShellVerdict) -> dict:
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode.value if isinstance(mode, ShellMode) else str(mode),
            "command": cmd[:500],
            "action": verdict.action,
            "reason": verdict.reason,
        }
        self._entries.append(entry)
        if self._path:
            try:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return entry

    def recent(self, limit: int = 100) -> list[dict]:
        entries = list(self._entries)
        return entries[-limit:] if limit < len(entries) else entries

    def __len__(self) -> int:
        return len(self._entries)


class WhitelistStore:
    """按档位持久化白名单（JSON 文件，缺省 ~/.hermes/shell_policy_whitelist.json）。

    2026-11-06 C5 接线（规则版本化，学 dsh-superagent policyVersion/revalidate 设计）：
    规则集每次变更计算稳定哈希版本号并留变更历史（最多 10 条）；
    政策变更不吊销在办任务——续做方拿 needs_revalidate(旧版本) 判断是否重验。
    """

    #: 参与版本哈希之外的元数据键（自身不参与哈希，防递归）
    _META_KEYS = ("version", "version_history")

    def __init__(self, path: str | None = None):
        if path is None:
            base = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
            path = os.path.join(base, "shell_policy_whitelist.json")
        self.path = path
        self._data: dict = self._load()

    def _rules_view(self) -> dict:
        """规则内容视图（剔除版本元数据，只保留真正影响判定的规则集）。"""
        return {k: v for k, v in self._data.items() if k not in self._META_KEYS}

    def current_version(self) -> str:
        """当前规则集的稳定版本号（键序无关哈希）。"""
        return policy_version(self._rules_view())

    def version_history(self) -> list[dict]:
        """最近规则变更记录（时间+新版本号），最多 10 条，新在前。"""
        return list(self._data.get("version_history") or [])

    def needs_revalidate(self, mandate_version: str) -> bool:
        """续做边界用：任务接单时冻结的版本 != 当前版本 → 要求重验。"""
        return mandate_version != self.current_version()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            new_version = self.current_version()
            if self._data.get("version") != new_version:
                history = self._data.get("version_history") or []
                history = [{"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "version": new_version}] + list(history)
                self._data["version_history"] = history[:10]
                self._data["version"] = new_version
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    @staticmethod
    def default_safe() -> frozenset:
        enabled: set = set()
        for group, spec in CATALOG.items():
            if spec["default_on"] and group not in SAFE_LOCKED_GROUPS:
                enabled.update(spec["commands"])
        return frozenset(enabled - SAFE_LOCKED_SINGLETONS)

    def allowed_for(self, mode: ShellMode | str) -> list[str]:
        if isinstance(mode, str):
            mode = ShellMode(mode)
        if mode is ShellMode.FULL:
            return []
        key = "safe"
        saved = self._data.get(key)
        if saved is None:
            return sorted(self.default_safe())
        return list(saved)

    def set_safe(self, commands: Iterable[str]) -> None:
        self._data["safe"] = sorted(set(commands))
        self._save()

    def catalog(self) -> dict:
        return CATALOG

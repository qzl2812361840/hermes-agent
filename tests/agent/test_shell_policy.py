"""shell_policy 单元测试：三档权限与九条防绕过规则。"""
import json

from agent.shell_policy import (  # noqa: E402
    ALLOW, CONFIRM, DENY, ShellAuditLog, ShellMode, WhitelistStore,
    judge_shell_command,
)

SAFE = ShellMode.SAFE
FULL = ShellMode.FULL
OFF = ShellMode.OFF
DEFAULT = WhitelistStore.default_safe()


def j(cmd, mode=SAFE, allowed=None):
    return judge_shell_command(cmd, mode, allowed if allowed is not None else DEFAULT)


# ---- 档位行为 ----

def test_off_mode_allows_everything():
    assert j("rm -rf /", OFF).action == ALLOW
    assert j("dd if=/dev/zero of=/dev/sda", OFF).action == ALLOW


def test_full_blocks_locked_group():
    assert j("dd if=/dev/zero of=/dev/sda", FULL).action == DENY
    assert j("mkfs.ext4 /dev/block/sda", FULL).action == DENY
    assert j("reboot", FULL).action == DENY


def test_full_allows_others():
    assert j("ls /data", FULL).action == ALLOW


# ---- 规则 1：命令替换 ----

def test_command_substitution_denied_in_safe():
    for cmd in ["echo $(rm -rf /data)", "echo ${HOME}", "echo `id`"]:
        assert j(cmd).action == DENY, cmd


# ---- 规则 2：拆段逐段判定 ----

def test_segments_judged_independently():
    assert j("ls && cat /etc/hostname").action == ALLOW
    assert j("ls; reboot").action == DENY
    assert j("ls || rm -rf /sdcard/DCIM").action == DENY


# ---- 规则 3：.. 规范化折叠后按直写规则判定 ----

def test_dotdot_write_denied():
    # 读路径折叠后与直接路径同规则（相册可读）
    assert j("cat /sdcard/../DCIM/a.jpg").action == ALLOW
    # 折叠后指向的写操作一律拒绝
    assert j("rm /sdcard/DCIM/../DCIM/a.jpg").action == DENY
    assert j("echo hi > /tmp/../../etc/passwd").action == DENY


# ---- 规则 4：cd 限定 ----

def test_cd_confined():
    assert j("cd /data/local/tmp", allowed=DEFAULT | {"cd"}).action == ALLOW
    assert j("cd /data", allowed=DEFAULT | {"cd"}).action == DENY


# ---- 规则 5：重定向按写检查 ----

def test_redirect_target_checked():
    assert j("cat /sdcard/DCIM/a.jpg > /sdcard/DCIM/b.jpg").action == DENY
    assert j("echo hi > /data/local/tmp/x.txt", allowed=DEFAULT | {"echo"}).action == ALLOW


# ---- 规则 6/7：受保护目录只准看 ----

def test_protected_readonly():
    assert j("ls /sdcard/DCIM").action == ALLOW
    assert j("rm /sdcard/DCIM/a.jpg").action == DENY
    assert j("cp /sdcard/DCIM/a.jpg /sdcard/Download/").action == DENY
    assert j("mv /sdcard/Pictures/a.png /sdcard/Download/").action == DENY
    assert j("tar czf /tmp/a.tgz /sdcard/DCIM").action == DENY


# ---- 规则 8：高危单命令封禁 ----

def test_high_risk_singletons():
    assert j("find / -delete").action == DENY
    assert j("find / -exec rm {} \\;").action == DENY
    assert j("pm clear com.android.chrome").action == DENY


# ---- 白名单机制 ----

def test_whitelist_gate():
    assert j("ls /sdcard").action == ALLOW
    assert j("curl http://example.com").action == DENY  # network 组默认关
    assert j("curl http://example.com", allowed=DEFAULT | {"curl"}).action == ALLOW


def test_script_interpreter_confirms():
    assert j("sh /tmp/a.sh", allowed=DEFAULT - {"sh"}).action == CONFIRM


# ---- 风险词法模式 ----

def test_risk_patterns_confirm():
    assert j("chmod 777 /sdcard/a", allowed=DEFAULT | {"chmod"}).action == CONFIRM


# ---- 审计环形队列 ----

def test_audit_ring_capacity():
    log = ShellAuditLog(capacity=300)
    for i in range(350):
        judge_shell_command(f"ls /tmp/{i}", SAFE, DEFAULT, audit=log)
    assert len(log) == 300
    recent = log.recent(100)
    assert len(recent) == 100
    assert recent[-1]["command"].endswith("349")


# ---- 白名单存储 ----

def test_whitelist_store_roundtrip(tmp_path):
    p = tmp_path / "wl.json"
    store = WhitelistStore(str(p))
    base = set(store.default_safe())
    base.add("curl")
    store.set_safe(base)
    store2 = WhitelistStore(str(p))
    assert "curl" in store2.allowed_for(SAFE)
    with open(p, encoding="utf-8") as fh:
        assert isinstance(json.load(fh), dict)

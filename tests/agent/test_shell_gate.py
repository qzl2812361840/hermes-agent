"""shell_gate 接线层测试：默认 OFF 零影响、档位解析、拦截/确认/永久禁止、审计。"""

import pytest

from agent import shell_gate
from agent.shell_gate import policy_block
from agent.shell_policy import ShellAuditLog, ShellMode


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(shell_gate.MODE_ENV, raising=False)
    shell_gate.reset_audit_for_tests()
    yield
    shell_gate.reset_audit_for_tests()


# --- 默认 OFF：零影响 -------------------------------------------------------

def test_default_off_is_noop():
    assert shell_gate.current_mode() is ShellMode.OFF
    # OFF 档连 rm -rf 都放行——策略未启用就完全不介入
    assert policy_block("rm -rf /sdcard/DCIM") is None
    assert shell_gate._audit is None  # 不建审计单例


def test_mode_parsing(monkeypatch):
    monkeypatch.setenv(shell_gate.MODE_ENV, "safe")
    assert shell_gate.current_mode() is ShellMode.SAFE
    monkeypatch.setenv(shell_gate.MODE_ENV, "FULL")
    assert shell_gate.current_mode() is ShellMode.FULL
    monkeypatch.setenv(shell_gate.MODE_ENV, "bogus")
    assert shell_gate.current_mode() is ShellMode.OFF
    monkeypatch.setenv(shell_gate.MODE_ENV, "  safe  ")
    assert shell_gate.current_mode() is ShellMode.SAFE


# --- SAFE 档 ----------------------------------------------------------------

def test_safe_allows_readonly_whitelist(monkeypatch):
    monkeypatch.setenv(shell_gate.MODE_ENV, "safe")
    assert policy_block("ls /sdcard") is None
    assert policy_block("cat /data/local/tmp/a.txt") is None
    assert policy_block("ps -ef | grep hermes") is None  # 管道拆段逐段判定


def test_safe_blocks_nonwhitelisted_write(monkeypatch):
    monkeypatch.setenv(shell_gate.MODE_ENV, "safe")
    block = policy_block("rm -rf /sdcard/DCIM/camera")
    assert block is not None
    assert block["action"] == "deny"
    assert block["status"] == "error"
    assert "策略拦截" in block["error"]


def test_safe_blocks_command_substitution(monkeypatch):
    monkeypatch.setenv(shell_gate.MODE_ENV, "safe")
    for cmd in ("echo $(reboot)", "echo `reboot`", "echo ${HOME}"):
        block = policy_block(cmd)
        assert block is not None, cmd
        assert block["action"] == "deny"


def test_safe_confirm_requires_force(monkeypatch):
    monkeypatch.setenv(shell_gate.MODE_ENV, "safe")
    # python 属脚本解释器 → CONFIRM
    assert policy_block("python -c 'print(1)'") is not None
    # 用户已确认（force）→ 放行
    assert policy_block("python -c 'print(1)'", force=True) is None
    # device_shell 场景：CONFIRM 交给既有审批门
    assert policy_block("python -c 'print(1)'", confirm_pass_through=True) is None


def test_safe_denies_even_with_force(monkeypatch):
    monkeypatch.setenv(shell_gate.MODE_ENV, "safe")
    # DENY 级不因 force 放行——force 只覆盖 CONFIRM
    assert policy_block("pm clear com.example", force=True) is not None


# --- FULL 档 ----------------------------------------------------------------

def test_full_blocks_locked_only(monkeypatch):
    monkeypatch.setenv(shell_gate.MODE_ENV, "full")
    assert policy_block("curl http://example.com") is None
    assert policy_block("python /tmp/script.py") is None
    block = policy_block("dd if=/dev/zero of=/dev/block/sda")
    assert block is not None
    assert block["action"] == "deny"
    assert "永久禁止" in block["reason"]


# --- 审计 --------------------------------------------------------------------

def test_audit_records_verdicts_when_enabled(monkeypatch):
    audit = ShellAuditLog()
    monkeypatch.setattr(shell_gate, "_audit", audit)
    monkeypatch.setenv(shell_gate.MODE_ENV, "safe")
    policy_block("rm -rf /sdcard/x")
    policy_block("ls /sdcard")
    assert len(audit) == 2
    entries = audit.recent()
    assert entries[0]["action"] == "deny"
    assert entries[1]["action"] == "allow"


def test_audit_singleton_not_created_when_off():
    policy_block("rm -rf /")
    assert shell_gate._audit is None


# --- fail-open ---------------------------------------------------------------

def test_fail_open_on_internal_error(monkeypatch):
    monkeypatch.setenv(shell_gate.MODE_ENV, "safe")

    def _boom(*a, **kw):
        raise RuntimeError("policy exploded")

    monkeypatch.setattr(shell_gate, "judge_shell_command", _boom)
    assert policy_block("ls") is None  # 策略故障放行，不瘫痪执行链

"""mutation_verifier 单元测试：读回核验与退出码三分级。"""
import pytest

from agent.mutation_verifier import (
    ExitCodePolicy,
    MutationOutcome,
    OutcomeStatus,
    verify_mutation,
)


def test_success_changed_verified():
    state = {"v": 1}

    def read():
        return state["v"]

    out = verify_mutation(read, lambda: (state.__setitem__("v", 2), 0)[1], expected=2)
    assert out.status is OutcomeStatus.SUCCEEDED
    assert out.before == 1 and out.after == 2
    assert out.changed is True and out.verified is True


def test_success_but_unchanged_reports_honestly():
    state = {"v": 1}
    out = verify_mutation(lambda: state["v"], lambda: 0)  # 命令成功但啥也没改
    assert out.status is OutcomeStatus.SUCCEEDED
    assert out.changed is False and out.verified is False


def test_unreadable_state_refuses_execution():
    def read():
        raise RuntimeError("cannot read")

    called = {"mutated": False}

    def mutate():
        called["mutated"] = True
        return 0

    out = verify_mutation(read, mutate)
    assert out.status is OutcomeStatus.REFUSED
    assert called["mutated"] is False  # 读不到就不准写
    assert "STATE_UNAVAILABLE" in out.failure_code


def test_failed_exit_code():
    state = {"v": 1}
    out = verify_mutation(lambda: state["v"], lambda: 1)
    assert out.status is OutcomeStatus.FAILED
    assert out.after is None


def test_timeout_is_not_failure():
    state = {"v": 1}
    out = verify_mutation(lambda: state["v"], lambda: (0, True))  # (exit, timed_out)
    assert out.status is OutcomeStatus.TIMED_OUT
    assert out.failure_code == "ROOT_COMMAND_TIMEOUT"


def test_exit_code_policy_classification():
    assert ExitCodePolicy.classify(0) is OutcomeStatus.SUCCEEDED
    assert ExitCodePolicy.classify(1) is OutcomeStatus.FAILED
    assert ExitCodePolicy.classify(0, timed_out=True) is OutcomeStatus.TIMED_OUT
    assert ExitCodePolicy.classify(None, timed_out=True) is OutcomeStatus.TIMED_OUT


def test_verify_read_failed_after_success():
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        if calls["n"] < 2:
            return "on"
        raise OSError("readback boom")

    out = verify_mutation(read, lambda: 0)
    assert out.status is OutcomeStatus.SUCCEEDED
    assert out.changed is False and out.verified is False
    assert "VERIFY_READ_FAILED" in out.failure_code


def test_to_dict_shape():
    out = MutationOutcome(status=OutcomeStatus.REFUSED, failure_code="x")
    d = out.to_dict()
    assert d["status"] == "REFUSED" and d["failure_code"] == "x"

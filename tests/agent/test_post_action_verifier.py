"""B6 点后核验强化单测：阶梯重试、超时分级、REFUSED、证据留痕。"""
from agent.post_action_verifier import ActionStatus, verify_action


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_first_probe_ok_verified():
    calls = []

    def action():
        calls.append(1)

    outcome = verify_action(action, lambda: True)
    assert outcome.ok and outcome.status == ActionStatus.VERIFIED
    assert outcome.attempts == 1 and len(calls) == 1  # 首轮通过不重试动作
    assert outcome.checks[0]["probe_ok"] is True


def test_retry_then_verified():
    state = {"n": 0, "done": False}

    def action():
        state["n"] += 1
        if state["n"] >= 2:  # 第二次动作后效果才出现
            state["done"] = True

    def probe():
        return state["done"]

    outcome = verify_action(action, probe, interval_s=0.0, sleeper=lambda s: None)
    assert outcome.status == ActionStatus.RETRY_VERIFIED
    assert outcome.attempts == 2
    assert outcome.checks[0]["probe_ok"] is False and outcome.checks[1]["probe_ok"] is True


def test_retries_exhausted_failed_with_evidence():
    outcome = verify_action(lambda: None, lambda: False, max_retries=2,
                            interval_s=0.0, sleeper=lambda s: None)
    assert outcome.status == ActionStatus.FAILED
    assert not outcome.ok
    assert outcome.attempts == 3  # 首轮+2次重试
    assert len(outcome.checks) == 3
    assert outcome.checks[-1]["detail"] is None  # probe 返回 False 属正常未达成


def test_probe_none_never_counts_success():
    outcome = verify_action(lambda: None, lambda: None, max_retries=1,
                            interval_s=0.0, sleeper=lambda s: None)
    assert outcome.status == ActionStatus.FAILED
    assert all(not c["probe_ok"] for c in outcome.checks)


def test_probe_exception_treated_unmet():
    def bad_probe():
        raise RuntimeError("screen gone")

    outcome = verify_action(lambda: None, bad_probe, max_retries=1,
                            interval_s=0.0, sleeper=lambda s: None)
    assert outcome.status == ActionStatus.FAILED
    assert outcome.checks[0]["detail"] == "probe_error:RuntimeError"


def test_action_refused_skips_everything():
    executed = []

    outcome = verify_action(lambda: executed.append(1), lambda: True, action_refused=True)
    assert outcome.status == ActionStatus.REFUSED
    assert outcome.attempts == 0 and executed == []


def test_budget_timeout():
    clock = FakeClock()

    def action():
        clock.t += 10  # 每次动作耗 10s

    outcome = verify_action(action, lambda: False, max_retries=5, budget_s=25.0,
                            interval_s=0.0, sleeper=lambda s: None, clock=clock)
    assert outcome.status == ActionStatus.TIMED_OUT
    assert outcome.failure_code == "budget_exhausted"
    assert outcome.attempts <= 3  # 预算内只跑了 3 轮（0/10/20）


def test_action_exception_recorded_and_retry_continues():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("device busy")

    def probe():
        return state["n"] >= 3

    outcome = verify_action(flaky, probe, max_retries=3, interval_s=0.0,
                            sleeper=lambda s: None)
    assert outcome.status == ActionStatus.RETRY_VERIFIED
    assert outcome.attempts == 3
    # 动作异常的轮次留了证据
    assert any(c["detail"] == "action_error:RuntimeError" for c in outcome.checks)


def test_timeout_never_mixed_with_success():
    """总预算刚好卡在探测成功前：宁可 TIMED_OUT，不把未核验的成功误报。"""
    clock = FakeClock()

    def probe():
        return False

    outcome = verify_action(lambda: None, probe, max_retries=1, budget_s=0.0,
                            interval_s=0.0, sleeper=lambda s: None, clock=clock)
    assert outcome.status == ActionStatus.TIMED_OUT
    assert not outcome.ok

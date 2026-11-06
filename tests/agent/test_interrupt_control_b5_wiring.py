"""B5 接线测试：SteeringQueue 挂进 interrupt_control（主人令开源吸收第二批）。

覆盖：
- steer() 经队列入队，_drain_pending_steer() FIFO 拼接返回（旧契约不变）；
- 同 id 去重：message_id 相同的第二句不产生双份；
- 上限 5 条丢最旧：第 6 句挤掉第 1 句，最新一句保留；
- 重排队回队头：drain 后 requeue 的旧话排在封口后新来插话之前；
- hard_cancel 一键打断：队列与旧字符串视图一起清空；
- _requeue_pending_steer 队列优先（agent_runtime_helpers）。
"""
import threading

import pytest

from agent.steering_queue import SteeringQueue
from agent.interrupt_control import InterruptControlMixin
from agent.agent_runtime_helpers import _requeue_pending_steer


class _StubAgent(InterruptControlMixin):
    """带 steer/drain 所需属性的最小桩（对齐 test_terminal_yield_to_background 的做法）。"""

    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self._pending_redirect = None
        self._pending_redirect_lock = threading.Lock()
        self._interrupt_requested = False
        self._interrupt_message = None
        self._interrupt_thread_signal_pending = False
        self._execution_thread_id = None
        self._hard_interrupt_requested = threading.Event()
        self._tool_interrupt_reason = None


def test_steer_drain_fifo_joined():
    agent = _StubAgent()
    assert agent.steer("第一句") is True
    assert agent.steer("第二句") is True
    # 旧字符串视图同步：直接读槽位也能看到完整待交付内容
    assert agent._pending_steer == "第一句\n第二句"
    assert agent._drain_pending_steer() == "第一句\n第二句"
    assert agent._drain_pending_steer() is None  # 取完即空


def test_same_id_dedup():
    agent = _StubAgent()
    assert agent.steer("重发", message_id="m1") is True
    assert agent.steer("重发", message_id="m1") is False  # 同 id 去重
    assert agent._drain_pending_steer() == "重发"


def test_cap_five_drops_oldest():
    agent = _StubAgent()
    for i in range(1, 7):
        assert agent.steer(f"第{i}句") is True
    drained = agent._drain_pending_steer()
    assert "第1句" not in drained  # 最旧被挤掉
    assert drained.split("\n")[-1] == "第6句"  # 最新一句保留


def test_requeue_front_orders_before_new_steers():
    agent = _StubAgent()
    agent.steer("旧话")
    assert agent._drain_pending_steer() == "旧话"
    _requeue_pending_steer(agent, "旧话")  # 交付失败回队
    agent.steer("新话")  # 封口后来的是下一代
    assert agent._drain_pending_steer() == "旧话\n新话"


def test_hard_cancel_wipes_queue_and_view():
    agent = _StubAgent()
    agent.steer("会被打断的话")
    assert agent._pending_steer == "会被打断的话"
    assert agent.clear_interrupt(hard_cancel=True) is True
    assert agent._pending_steer is None
    assert agent._drain_pending_steer() is None


def test_queue_max_pending_unit():
    q = SteeringQueue(max_pending=2)
    assert q.steer("a", "a") is True
    assert q.steer("b", "b") is True
    assert q.steer("c", "c") is True  # a 被挤出
    assert [s.text for s in q.drain()] == ["b", "c"]


def test_queue_requeue_front_unit():
    q = SteeringQueue()
    q.steer("1", "first")
    q.drain()
    assert q.requeue_front("first") is True
    q.steer("2", "second")
    assert [s.text for s in q.drain()] == ["first", "second"]

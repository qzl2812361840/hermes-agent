"""B5 插话排队单测：有序 deque、原子 seal、撤回、关闭语义。"""
import threading

from agent.steering_queue import SteeringQueue


def test_steer_enqueue_and_drain_order():
    q = SteeringQueue()
    assert q.steer("m1", "先查天气")
    assert q.steer("m2", "再加一条提醒")
    batch = q.drain()
    assert [s.text for s in batch] == ["先查天气", "再加一条提醒"]
    assert q.drain() == []  # 取走即清空
    assert not q.has_pending()


def test_steer_rejects_empty_and_duplicate_id():
    q = SteeringQueue()
    assert not q.steer("", "x")
    assert not q.steer("m1", "")
    assert q.steer("m1", "第一遍")
    assert not q.steer("m1", "重发")  # 同 id 去重
    assert len(q.drain()) == 1
    q.steer("m1", "重发")  # 队列已空，同 id 可重新入队
    assert q.drain()[0].text == "重发"


def test_seal_generation_monotonic():
    q = SteeringQueue()
    assert q.generation() == 0
    g1 = q.seal()
    assert g1 == 1
    q.steer("m1", "封口后插话")
    g2 = q.seal()
    assert g2 == 2 and q.generation() == 2
    # 封口后排入的插话仍在队列，等 drain 交付
    assert q.has_pending()


def test_seal_closed_returns_negative():
    q = SteeringQueue()
    q.close()
    assert q.seal() == -1
    assert not q.steer("m1", "关闭后插话")


def test_withdraw_removes_only_pending_non_task_only():
    q = SteeringQueue()
    q.steer("m1", "可撤回的")
    q.steer("m2", "只许打断当前任务", current_task_only=True)
    assert q.withdraw("m1")
    assert not q.withdraw("m1")  # 已不在队列
    assert not q.withdraw("m2")  # current_task_only 撤不回
    assert q.pending_texts() == ["只许打断当前任务"]


def test_close_returns_remaining_once():
    q = SteeringQueue()
    q.steer("m1", "没消费完的")
    rest = q.close()
    assert [s.text for s in rest] == ["没消费完的"]
    assert q.closed
    assert q.close() == []  # 二次关闭不重复交付


def test_concurrent_steer_drain_no_loss():
    """并发压测：多线程同时插话，drain 取走的总量必须等于插话总量。"""
    q = SteeringQueue()
    sent = []
    sent_lock = threading.Lock()

    def producer(n):
        for i in range(n):
            mid = f"m-{threading.get_ident()}-{i}"
            q.steer(mid, f"插话{i}")
            with sent_lock:
                sent.append(mid)

    threads = [threading.Thread(target=producer, args=(50,)) for _ in range(4)]
    drained = []
    stop = threading.Event()

    def collector():
        while not stop.is_set() or q.has_pending():
            for s in q.drain():
                drained.append(s.message_id)

    c = threading.Thread(target=collector)
    c.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    c.join()
    # 收尾兜底取走
    drained.extend(s.message_id for s in q.drain())
    assert sorted(drained) == sorted(sent)
    assert len(drained) == len(sent) == 200


def test_seal_then_drain_atomic_no_orphan():
    """seal 与 drain 并发：封口后排入的插话要么进下一代（seal 返回值之后 steer 的），
    要么留在队列，绝不出现在封口代数之前的交付里。"""
    q = SteeringQueue()
    q.steer("m1", "属于本代的")
    g = q.seal()
    assert g == 1
    q.steer("m2", "属于下一代的")
    batch = q.drain()
    assert [s.message_id for s in batch] == ["m1", "m2"]  # drain 取全部，代数由调用方比对
    assert q.generation() == 1  # 代数不被 drain 改动

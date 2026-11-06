"""Steering queue: 用户在运行中插话的有序队列 + 原子封口（B5，吸收 Mika RunInbox / Droidance 设计，自行实现）。

缺口：interrupt_control 的 steer/redirect 是单槽位——连续多句插话只有最后一句能被下一轮看到。
本模块提供：
- 有序 deque：每句插话逐字保留、按序交付，不丢不重；
- 原子 seal：封口操作返回代数（generation），封口瞬间排入的插话要么进本次交付、要么算入下一代，
  不存在"封口与插话并发导致一句悬空"的竞态；
- 丢弃（withdraw）：只撤还在队列里的插话；
- close：收尾后未消费的插话整体交还宿主作为下一任务。
线程安全：所有读写持同一把锁。默认零影响，只有调用方主动接入才生效。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Steer:
    """一句用户插话：messageId 支持撤回与去重；current_task_only 标记只许打断当前任务。"""

    message_id: str
    text: str
    current_task_only: bool = False


class SteeringQueue:
    """运行中插话的唯一入口。seal() 与 drain() 配合实现"要么进这批、要么留下轮"的原子语义。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steers: deque = deque()
        self._closed = False
        self._generation = 0  # 每次封口+1；调用方用代数判断自己负责的范围

    # ------------------------------------------------------------------
    # 写入口
    # ------------------------------------------------------------------

    def steer(self, message_id: str, text: str, *, current_task_only: bool = False) -> bool:
        """排入一句插话。队列已关闭返回 False（调用方转投下一任务），否则 True。"""
        if not message_id or not text:
            return False
        with self._lock:
            if self._closed:
                return False
            if any(s.message_id == message_id for s in self._steers):
                return False  # 同 id 去重：重发不产生双份
            self._steers.append(Steer(message_id, text, current_task_only))
            return True

    def withdraw(self, message_id: str) -> bool:
        """撤回还在队列里的插话；current_task_only 的撤不回（归属端自管）。"""
        with self._lock:
            if self._closed:
                return False
            for i, s in enumerate(self._steers):
                if s.message_id == message_id:
                    if s.current_task_only:
                        return False
                    del self._steers[i]
                    return True
            return False

    def close(self) -> List[Steer]:
        """收尾：交还未消费的插话给宿主作为下一任务；关闭后 steer 一律拒绝。"""
        with self._lock:
            if self._closed:
                return []
            self._closed = True
            remaining = list(self._steers)
            self._steers.clear()
            return remaining

    # ------------------------------------------------------------------
    # 封口与交付（原子语义核心）
    # ------------------------------------------------------------------

    def seal(self) -> int:
        """原子封口：返回封口时的代数。封口后排入的插话属于下一代，不会混进本代交付。"""
        with self._lock:
            if self._closed:
                return -1
            self._generation += 1
            return self._generation

    def drain(self) -> List[Steer]:
        """原子取走当前全部插话（模型调用前或一批工具完成后调用）。"""
        with self._lock:
            out = list(self._steers)
            self._steers.clear()
            return out

    def generation(self) -> int:
        """当前代数：调用方交付完一代后与 seal() 比对，判断期间是否又来了新插话。"""
        with self._lock:
            return self._generation

    def has_pending(self) -> bool:
        """只看不取：批量执行在子步之间据此决定是否停下。"""
        with self._lock:
            return bool(self._steers)

    def pending_texts(self) -> List[str]:
        """快照当前插话文本（不取走），供日志与展示。"""
        with self._lock:
            return [s.text for s in self._steers]

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

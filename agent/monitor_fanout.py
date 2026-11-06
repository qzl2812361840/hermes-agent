"""订阅直通扇出（monitor_fanout）——参考 dsh-superagent-prototype intake.ts 订阅直通设计（MIT），Python 自行实现。

语义：
  - 已订阅主题的新内容绕过完整评估（快筛+慢判），直达该主题的处理流程
  - 未订阅的走常规评估路径（返回 None 由调用方接手）
  - 订阅关系只增不删；重复订阅幂等
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

__all__ = ["Subscriptions", "fanout"]


@dataclass
class Subscriptions:
    """topic → 处理回调；线程安全性由调用方保证。"""

    _handlers: Dict[str, List[Callable[[str], None]]] = field(default_factory=dict)

    def subscribe(self, topic: str, handler: Callable[[str], None]) -> bool:
        """幂等订阅：同一 topic+handler 只挂一次。"""
        lst = self._handlers.setdefault(topic, [])
        if handler in lst:
            return False
        lst.append(handler)
        return True

    def is_subscribed(self, topic: str) -> bool:
        return bool(self._handlers.get(topic))


def fanout(subs: Subscriptions, topic: str, content: str) -> bool:
    """已订阅 → 直通该主题处理链（返回 True）；未订阅 → 返回 False 走常规评估。"""
    handlers = subs._handlers.get(topic)
    if not handlers:
        return False
    for h in handlers:
        h(content)
    return True

"""C4 保序回信补投（学 dsh-superagent outbox.ts 设计，MIT 原型，Python 自行实现）。

叠加式挂载在 DeliveryRouter 投递路径上：
  - 正常发送成功零开销（不进 outbox，不碰台账）
  - 投递失败才由 delivery.py 留痕进 outbox 台账（per-channel seq 保序）
  - 本模块的后台循环按序补投：前一条未通则后一条不出；连续 3 次失败升级为 dropped 留痕（不静默）
台账 db 放 Hermes home 的 gateway/outbox_retry.db，网关重启不丢。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from hermes_cli.config import get_hermes_home

from .config import Platform
from .delivery import DeliveryRouter, DeliveryTransport, resolve_delivery_transport, _send_result_error

logger = logging.getLogger(__name__)

# 补投循环的轮询间隔（秒）：投递失败是低频事件，一分钟看一眼足够。
FLUSH_INTERVAL_SECONDS = 60.0


class OutboxRetry:
    """outbox 台账的持有者 + 补投执行器；台账复用 agent.outbox 的表结构与语义。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        # sync sender 不参与补投（flush_async 绕开它），占位以满足构造签名。
        from agent.outbox import Outbox
        self.outbox = Outbox(self.db, sender=lambda channel, payload: None)

    def record_failure(self, platform: str, chat_id: str, content: str,
                       metadata: Optional[dict] = None) -> int:
        """投递失败留痕：channel = 平台:会话，payload 带原文与投递元数据，补投时原样重发。"""
        channel = f"{platform}:{chat_id}"
        payload = json.dumps({"content": content, "metadata": dict(metadata or {})}, ensure_ascii=False)
        msg_id = self.outbox.push(channel, payload)
        self.db.commit()
        return msg_id

    async def flush_once(self, asend: Callable[[str, str], Awaitable[None]]) -> int:
        """扫一遍所有积压通道逐个补投；返回本次实际发出的条数。"""
        sent = 0
        for channel in self.outbox.pending_channels():
            sent += await self.outbox.flush_async(channel, asend)
            self.db.commit()
        return sent

    async def loop(self, asend: Callable[[str, str], Awaitable[None]],
                   stop: Optional[threading.Event] = None,
                   interval: float = FLUSH_INTERVAL_SECONDS) -> None:
        """常驻补投循环：到点扫一遍台账；stop 置位后在 1 秒内退出。"""
        while not (stop is not None and stop.is_set()):
            try:
                sent = await self.flush_once(asend)
                if sent:
                    logger.info("[outbox] 补投发出 %d 条", sent)
            except Exception as exc:  # 补投失败不拖垮循环，下个周期再试
                logger.warning("[outbox] 补投异常（下个周期重试）: %s", exc)
            for _ in range(max(1, int(interval))):
                if stop is not None and stop.is_set():
                    return
                await asyncio.sleep(1.0)

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:  # 关闭失败无需处理，进程退出由系统回收
            pass


async def _outbox_send(router: DeliveryRouter, channel: str, payload: str) -> None:
    """补投的单条发送：按台账 channel 还原平台+会话，走与正常投递相同的 transport。"""
    platform_str, chat_id = channel.split(":", 1)
    platform = Platform(platform_str)
    transport: Optional[DeliveryTransport] = resolve_delivery_transport(platform, router.config, router.adapters)
    if transport is None:
        raise RuntimeError(f"[outbox] 补投时无可用适配器: {platform_str}")
    data = json.loads(payload)
    result = await transport.send(platform, chat_id, data.get("content", ""), metadata=data.get("metadata") or None)
    error = _send_result_error(result)
    if error:
        raise RuntimeError(error)


def start_outbox_retry_task(router: DeliveryRouter, stop: threading.Event) -> asyncio.Task:
    """在当前事件循环上启动补投任务（网关启动序列里与 cron/housekeeping 同段挂载）。"""
    retry = router.outbox_retry  # 懒加载实例化台账
    return asyncio.get_running_loop().create_task(retry.loop(_outbox_send_partial(router), stop=stop))


def _outbox_send_partial(router: DeliveryRouter) -> Callable[[str, str], Awaitable[None]]:
    """把 router 绑进单条发送函数，形成 asend(channel, payload) 签名。"""

    async def asend(channel: str, payload: str) -> None:
        await _outbox_send(router, channel, payload)

    return asend

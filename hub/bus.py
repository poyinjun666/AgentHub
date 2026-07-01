"""
EventBus - 基于 asyncio.Queue 的 topic 路由消息总线
- 每个 agent 一个 inbox queue（按 agent_id 路由）
- 支持点对点、广播、hub 专用队列
- 支持请求-响应模式（request/response）
特点：单进程内零依赖、原生异步、天然背压（queue 满会等待）
"""
import asyncio
import logging
from typing import Callable, Optional
from .protocol import AgentMessage, MsgType

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self, max_queue_size: int = 256):
        self._queues: dict[str, asyncio.Queue] = {}
        self._max_size = max_queue_size
        self._lock = asyncio.Lock()
        # 订阅广播的回调（用于审计、看板）
        self._tap_callbacks: list[Callable[[AgentMessage], None]] = []

    def _get_or_create_queue(self, agent_id: str) -> asyncio.Queue:
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue(maxsize=self._max_size)
        return self._queues[agent_id]

    async def register(self, agent_id: str):
        async with self._lock:
            self._get_or_create_queue(agent_id)
            logger.debug(f"EventBus registered: {agent_id}")

    async def publish(self, msg: AgentMessage):
        """发布消息。to_agent='broadcast' 时投递给所有已注册 agent（除发送方）。"""
        # tap（审计/看板）
        for cb in self._tap_callbacks:
            try:
                cb(msg)
            except Exception as e:
                logger.warning(f"tap callback error: {e}")

        if msg.to_agent == "broadcast":
            targets = [aid for aid in self._queues if aid != msg.from_agent]
            for aid in targets:
                await self._get_or_create_queue(aid).put(msg)
            logger.info(f"[BUS] broadcast {msg.msg_id} → {len(targets)} agents")
        elif msg.to_agent == "*":
            # 路由占位符（如 review_result 不知道发给谁时），调用方应先填好 to_agent
            logger.warning(f"[BUS] to_agent='*' 未解析，丢弃 {msg.msg_id}")
        else:
            q = self._get_or_create_queue(msg.to_agent)
            await q.put(msg)
            logger.info(f"[BUS] {msg.from_agent}→{msg.to_agent} "
                        f"type={msg.type} task={msg.task_id}")

    async def subscribe(self, agent_id: str) -> AgentMessage:
        """阻塞等待本 agent 的下一条消息"""
        q = self._get_or_create_queue(agent_id)
        return await q.get()

    def tap(self, callback: Callable[[AgentMessage], None]):
        """注册一个旁路回调，每条消息都会触发（用于审计、看板推送）"""
        self._tap_callbacks.append(callback)

    async def request(self, msg: AgentMessage, timeout: float = 30.0) -> AgentMessage:
        """请求-响应模式：发一条 request，挂起等待 reply_to=msg_id 的 response。

        注意：响应会投递到原请求发送方（msg.from_agent）的队列，因此这里订阅
        的是 from_agent 对应的队列，而不是 to_agent。
        """
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        def _waiter():
            async def _inner():
                while True:
                    m = await self.subscribe(msg.from_agent)
                    if m.type == MsgType.RESPONSE and m.reply_to == msg.msg_id:
                        future.set_result(m)
                        return
                    # 不是自己等的，放回队列尾部
                    await self._get_or_create_queue(msg.from_agent).put(m)
                    if future.done():
                        return
            asyncio.create_task(_inner())

        _waiter()
        await self.publish(msg)
        return await asyncio.wait_for(future, timeout=timeout)
"""
Feishu WebSocket 长连接 Server 启动入口

适用场景：
  飞书后台使用"长连接"模式接收事件，无需公网域名。

流程：
  1. 校验配置
  2. 启动 EventBus、Store
  3. 启动 PM + QA + FE/BE agents（同 asyncio 事件循环）
  4. 在后台线程启动 lark-oapi 的 WebSocket 长连接客户端
  5. 收到消息事件后创建任务并路由给 PM
  6. EventBus tap：监听 PM 的汇总/通知消息，通过飞书 API 回传到原群聊

运行前：
  cp .env.example .env
  # 填入 LLM key 和飞书 app_id/app_secret
  pip install -r requirements.txt
  python examples/feishu_ws_server.py
"""
import asyncio
import json
import logging
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lark_oapi as lark
from lark_oapi import EventDispatcherHandler, JSON, LogLevel

import config
from examples.demo import BECoder, FECoder, PMAgent, QAAgent
from hub.bus import EventBus
from hub.feishu_client import send_text_message
from hub.protocol import AgentMessage, MsgType
from hub.state import TaskState
from hub.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("feishu_ws_server")


def build_event_handler(bus: EventBus, store: Store, loop: asyncio.AbstractEventLoop):
    """构造飞书长连接事件处理器"""

    def _on_im_message(data: lark.im.v1.P2ImMessageReceiveV1):
        """收到消息事件"""
        try:
            data_dict = json.loads(JSON.marshal(data))
            event = data_dict.get("event", {})
            message = event.get("message", {})
            sender = event.get("sender", {})

            chat_type = message.get("chat_type", "")  # group / p2p
            chat_id = message.get("chat_id", "")
            msg_type = message.get("message_type", "")
            raw_content = message.get("content", "{}")
            user_open_id = sender.get("sender_id", {}).get("open_id", "")

            # 只处理文本消息
            if msg_type != "text":
                logger.info(f"[feishu_ws] ignore non-text msg_type={msg_type}")
                return

            try:
                content = json.loads(raw_content)
                user_text = content.get("text", "").strip()
            except json.JSONDecodeError:
                user_text = raw_content.strip()

            # 去掉 @机器人的文本前缀
            user_text = _strip_mentions(user_text)

            if not user_text:
                logger.info("[feishu_ws] empty text after strip, ignore")
                return

            logger.info(f"[feishu_ws][group={chat_type}] chat={chat_id}: {user_text[:80]}...")

            # 只有群聊/私聊发给 PM 机器人的消息才处理
            # 创建任务
            task = store.create_task(
                title=f"[Feishu/pm] {user_text[:40]}",
                spec={
                    "source": "feishu",
                    "bot": "pm",
                    "user_text": user_text,
                    "chat_id": chat_id,
                    "user_open_id": user_open_id,
                    "chat_type": chat_type,
                },
                assignee="pm",
                actor="feishu",
            )
            store.transition(task["task_id"], TaskState.SUBMITTED, actor="feishu")

            msg = AgentMessage(
                from_agent="feishu",
                to_agent="pm",
                type=MsgType.REQUEST,
                task_id=task["task_id"],
                trace_id=task["trace_id"],
                payload={
                    "requirement": user_text,
                    "source": "feishu",
                    "chat_id": chat_id,
                    "user_open_id": user_open_id,
                },
            )
            # 在 WS 线程里安全地发布到 asyncio EventBus
            asyncio.run_coroutine_threadsafe(bus.publish(msg), loop)
            logger.info(f"[feishu_ws] task created and sent to pm: {task['task_id']}")

        except Exception as e:
            logger.exception(f"[feishu_ws] handle message failed: {e}")

    return EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(_on_im_message) \
        .build()


def _strip_mentions(text: str) -> str:
    """去掉飞书消息里的 @机器人 标记，保留用户真实输入"""
    import re
    # 去掉 <at user_id="...">@xxx</at> 这种格式
    text = re.sub(r"<at[^>]*>[^<]*</at>", "", text)
    return text.strip()


def start_feishu_ws_client(bus: EventBus, store: Store, loop: asyncio.AbstractEventLoop):
    """在后台线程启动飞书 WebSocket 长连接客户端"""
    bot_cfg = config.FEISHU_BOTS.get("pm")
    if not bot_cfg or not bot_cfg.get("app_id") or not bot_cfg.get("app_secret"):
        logger.error("[feishu_ws] PM bot config missing, cannot start WS client")
        return

    event_handler = build_event_handler(bus, store, loop)
    cli = lark.ws.Client(
        bot_cfg["app_id"],
        bot_cfg["app_secret"],
        event_handler=event_handler,
        log_level=LogLevel.INFO,
    )
    logger.info("[feishu_ws] starting WebSocket client...")
    cli.start()


def setup_pm_notify_tap(bus: EventBus, store: Store):
    """注册 EventBus tap，把 PM 的汇总消息回传到飞书群"""
    def _on_pm_notify(msg: AgentMessage):
        if msg.type != MsgType.EVENT or msg.from_agent != "pm":
            return

        task_id = msg.task_id
        if not task_id:
            return

        task = store.get_task(task_id)
        if not task:
            return

        spec = task.get("spec", {}) or {}
        if spec.get("source") != "feishu":
            return

        chat_id = spec.get("chat_id")
        bot_name = spec.get("bot")
        if not chat_id or bot_name != "pm":
            return

        bot_cfg = config.FEISHU_BOTS.get(bot_name)
        if not bot_cfg:
            return

        payload = msg.payload
        text = payload.get("text", str(payload))

        try:
            send_text_message(bot_cfg, chat_id, text)
            logger.info(f"[feishu_ws] PM notify sent to chat {chat_id}")
        except Exception as e:
            logger.exception(f"[feishu_ws] failed to send PM notify: {e}")

    bus.tap(_on_pm_notify)


async def main():
    config.validate()
    logger.info("=" * 60)
    logger.info("AgentHub Feishu WS Server 启动中...")
    logger.info("=" * 60)

    bus = EventBus()
    store = Store()

    # 初始化所有角色
    pm = PMAgent("pm", bus, store)
    qa = QAAgent("qa", bus, store)
    coder_fe = FECoder("coder_fe", bus, store)
    coder_be = BECoder("coder_be", bus, store)

    # 启动 agents
    await pm.start()
    await qa.start()
    await coder_fe.start()
    await coder_be.start()

    # 注册 PM 通知 tap
    setup_pm_notify_tap(bus, store)

    logger.info("AgentHub agents 已启动，准备接收飞书长连接消息...")

    # 在后台线程启动飞书 WS 客户端
    loop = asyncio.get_event_loop()
    ws_thread = threading.Thread(
        target=start_feishu_ws_client,
        args=(bus, store, loop),
        daemon=True,
    )
    ws_thread.start()

    # 保持 asyncio 主循环存活
    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())

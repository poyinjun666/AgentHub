"""
Feishu Webhook Server - 四个 Bot 共用
- /webhook/{bot_name} 接收飞书事件
- URL 验证：返回 challenge
- 消息事件：仅 PM Bot 创建任务并路由给 PM；其他 Bot 提示用户找 PM
- EventBus tap：监听 PM 的汇总/通知消息，回传到原飞书群
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

import config
from .bus import EventBus
from .protocol import AgentMessage, MsgType
from .state import TaskState
from .store import Store
from .feishu_client import send_text_message

logger = logging.getLogger(__name__)


def _extract_chat_id(event: dict) -> Optional[str]:
    """从事件里提取 chat_id，兼容不同版本的事件结构"""
    return (
        event.get("open_chat_id")
        or event.get("chat_id")
        or event.get("message", {}).get("chat_id")
    )


def _extract_user_open_id(event: dict) -> Optional[str]:
    return (
        event.get("open_id")
        or event.get("user_open_id")
        or event.get("sender", {}).get("sender_id", {}).get("open_id")
    )


def _extract_message_content(event: dict) -> dict:
    """提取并解析消息 content（JSON 字符串）"""
    raw = (
        event.get("content")
        or event.get("message", {}).get("content")
        or "{}"
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}


def _extract_message_type(event: dict) -> str:
    return (
        event.get("msg_type")
        or event.get("message_type")
        or event.get("message", {}).get("message_type")
        or ""
    )


def _extract_user_text(event: dict) -> str:
    """从事件中提取用户文本"""
    msg_type = _extract_message_type(event)
    content = _extract_message_content(event)
    if msg_type == "text":
        return content.get("text", "").strip()
    return str(content)


def create_app(bus: EventBus, store: Store) -> FastAPI:
    """创建 FastAPI 应用，注入共用的 EventBus 和 Store"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.tap(_on_pm_notify)
        logger.info("[feishu] webhook server started, tap registered")
        yield
        logger.info("[feishu] webhook server stopped")

    app = FastAPI(title="AgentHub Feishu Webhooks", lifespan=lifespan)

    def _on_pm_notify(msg: AgentMessage):
        """EventBus tap 回调：PM 汇总/通知消息回传飞书"""
        if msg.type != MsgType.EVENT or msg.from_agent != "pm":
            return

        task_id = msg.task_id
        if not task_id:
            return

        # 这里 task_id 是根任务 id
        task = store.get_task(task_id)
        if not task:
            return

        spec = task.get("spec", {}) or {}
        if spec.get("source") != "feishu":
            return

        chat_id = spec.get("chat_id")
        bot_name = spec.get("bot")
        if not chat_id or not bot_name:
            logger.warning(f"[feishu] task {task_id} missing chat_id or bot, skip notify")
            return

        bot_cfg = config.FEISHU_BOTS.get(bot_name)
        if not bot_cfg:
            logger.warning(f"[feishu] unknown bot '{bot_name}' for task {task_id}")
            return

        payload = msg.payload
        text = payload.get("text", str(payload))

        try:
            send_text_message(bot_cfg, chat_id, text)
            logger.info(f"[feishu] PM notify sent to chat {chat_id} for task {task_id}")
        except Exception as e:
            logger.exception(f"[feishu] failed to send PM notify: {e}")

    @app.post("/webhook/{bot_name}")
    async def webhook(bot_name: str, request: Request):
        bot_cfg = config.FEISHU_BOTS.get(bot_name)
        if not bot_cfg:
            logger.warning(f"[feishu] unknown bot: {bot_name}")
            raise HTTPException(status_code=404, detail=f"unknown bot {bot_name}")

        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"[feishu] invalid json body: {e}")
            raise HTTPException(status_code=400, detail="invalid json")

        logger.debug(f"[feishu] webhook {bot_name}: {body}")

        # 1. URL 验证挑战
        if body.get("type") == "url_verification":
            return JSONResponse({"challenge": body.get("challenge")})

        # 2. 事件回调
        if body.get("type") == "event_callback":
            event = body.get("event", {}) or {}

            # verification_token 校验（配置了才校验）
            expected_token = bot_cfg.get("verification_token", "")
            if expected_token and body.get("token") != expected_token:
                logger.warning(f"[feishu] token mismatch for {bot_name}")
                raise HTTPException(status_code=403, detail="invalid token")

            # 只处理消息事件
            event_type = event.get("type", "")
            if event_type != "message":
                return JSONResponse({"code": 0, "msg": "ignored"})

            chat_id = _extract_chat_id(event)
            user_text = _extract_user_text(event)
            user_open_id = _extract_user_open_id(event)

            if not chat_id:
                logger.warning(f"[feishu] event missing chat_id: {event}")
                return JSONResponse({"code": 0, "msg": "no chat_id"})

            logger.info(
                f"[feishu][{bot_name}] msg from chat {chat_id}: {user_text[:80]}..."
            )

            # 只有 PM Bot 处理用户下旨；其他 Bot 提示用户找 PM
            if bot_name != "pm":
                _reply_to_other_bot(bot_cfg, bot_name, chat_id)
                return JSONResponse({"code": 0, "msg": "not pm bot"})

            target_agent = bot_cfg["agent_id"]

            # 创建任务
            task = store.create_task(
                title=f"[Feishu/{bot_name}] {user_text[:40]}",
                spec={
                    "source": "feishu",
                    "bot": bot_name,
                    "user_text": user_text,
                    "chat_id": chat_id,
                    "user_open_id": user_open_id,
                },
                assignee=target_agent,
                actor="feishu",
            )

            # 推进到 SUBMITTED，触发 agent 处理
            store.transition(task["task_id"], TaskState.SUBMITTED, actor="feishu")

            # 发送 REQUEST 给 PM
            msg = AgentMessage(
                from_agent="feishu",
                to_agent=target_agent,
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
            await bus.publish(msg)
            return JSONResponse({"code": 0, "msg": "ok"})

        return JSONResponse({"code": 0, "msg": "ignored"})

    def _reply_to_other_bot(bot_cfg: dict, bot_name: str, chat_id: str):
        """非 PM Bot 收到消息时，提示用户找 PM"""
        try:
            send_text_message(
                bot_cfg, chat_id,
                f"【{bot_name}】我只处理内部协作消息，请向 PM 机器人发送你的需求。"
            )
        except Exception as e:
            logger.exception(f"[feishu] failed to reply from {bot_name}: {e}")

    return app

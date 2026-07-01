"""
Feishu Webhook Server 启动入口

流程：
  1. 校验配置
  2. 启动 EventBus、Store
  3. 启动 PM + QA + FE/BE Coder agents
  4. 启动 FastAPI webhook server（和 agents 同 asyncio 事件循环）

运行前：
  cp .env.example .env
  # 填入 LLM key 和飞书 app_id/app_secret
  pip install -r requirements.txt
  python examples/feishu_server.py
"""
import asyncio
import logging
import os
import sys

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from examples.demo import BECoder, FECoder, PMAgent, QAAgent
from hub.bus import EventBus
from hub.feishu_webhook import create_app
from hub.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("feishu_server")


async def main():
    config.validate()
    logger.info("=" * 60)
    logger.info("AgentHub Feishu Server 启动中...")
    logger.info("=" * 60)

    bus = EventBus()
    store = Store()

    # 初始化所有角色
    pm = PMAgent("pm", bus, store)
    qa = QAAgent("qa", bus, store)
    coder_fe = FECoder("coder_fe", bus, store)
    coder_be = BECoder("coder_be", bus, store)

    # 启动
    await pm.start()
    await qa.start()
    await coder_fe.start()
    await coder_be.start()

    logger.info("AgentHub agents 已启动，准备接收飞书消息...")

    # 创建 FastAPI 应用，注入同一个 bus/store
    app = create_app(bus, store)

    # 启动 uvicorn（和 agents 同事件循环）
    uvicorn_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.FEISHU_WEBHOOK_PORT,
        loop="asyncio",
    )
    server = uvicorn.Server(uvicorn_config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())

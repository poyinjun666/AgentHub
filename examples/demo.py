"""
AgentHub 最小可跑 Demo
流程：
    用户下旨 → PM(kimi) 拆解 → 分派给 coder_fe(deepseek) / coder_be(minimax) 并行开发
    → 各自提交验收 → 中枢(GLM) 三段式验收 → 通过/打回/升级

运行前：
    1. cp .env.example .env 并填好 4 个 API key
    2. pip install -r requirements.txt
    3. python examples/demo.py
"""
import asyncio
import json
import logging
import sys
import os

# 把项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from hub.bus import EventBus
from hub.store import Store
from hub.protocol import AgentMessage, MsgType
from hub.state import TaskState
from hub.agent_base import AgentBase
from hub.hub_core import Hub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("demo")


# ============================================================
# 1. 三个成员 Agent 子类
# ============================================================
class PMAgent(AgentBase):
    """产品经理：拆解需求，生成任务规格，再广播给开发"""

    async def _handle(self, msg: AgentMessage) -> tuple[str, str]:
        user_req = msg.payload.get("requirement", "")
        rework = msg.payload.get("rework", False)

        if rework:
            prompt = f"""之前你拆解的需求被中枢打回，请修正。
原需求：{user_req}
问题：{json.dumps(msg.payload.get('issues', []), ensure_ascii=False)}
重新输出 JSON 任务规格。"""
        else:
            prompt = f"""你是产品经理。请把下面用户需求拆解为子任务规格，分配给 coder_fe（前端）和 coder_be（后端）。

用户需求：{user_req}

严格输出 JSON：
{{
  "title": "任务标题",
  "fe_spec": {{{
    "goal": "前端目标",
    "requirements": ["要求1", "要求2"],
    "deliverable": "前端交付物描述"
  }}},
  "be_spec": {{
    "goal": "后端目标",
    "requirements": ["要求1", "要求2"],
    "deliverable": "后端交付物描述"
  }}
}}"""
        result = self.llm_json([
            {"role": "system", "content": "你是资深产品经理，擅长需求拆解。"},
            {"role": "user", "content": prompt},
        ])

        # 把拆解结果广播给两个 coder（异步并行）
        fe_task = self.store.create_task(
            title=result["title"] + " [FE]",
            spec=result["fe_spec"],
            assignee="coder_fe",
            trace_id=msg.trace_id,
            actor=self.agent_id,
        )
        be_task = self.store.create_task(
            title=result["title"] + " [BE]",
            spec=result["be_spec"],
            assignee="coder_be",
            trace_id=msg.trace_id,
            actor=self.agent_id,
        )
        # 触发两个 coder
        await self.bus.publish(AgentMessage(
            from_agent=self.agent_id, to_agent="coder_fe",
            type=MsgType.REQUEST, task_id=fe_task["task_id"], trace_id=msg.trace_id,
            payload={"spec": result["fe_spec"]},
        ))
        await self.bus.publish(AgentMessage(
            from_agent=self.agent_id, to_agent="coder_be",
            type=MsgType.REQUEST, task_id=be_task["task_id"], trace_id=msg.trace_id,
            payload={"spec": result["be_spec"]},
        ))

        artifact = json.dumps(result, ensure_ascii=False, indent=2)
        # PM 自身的产出（拆解结果）也提交验收
        return artifact, "已完成需求拆解并分派给前端/后端"


class FECoder(AgentBase):
    """前端 Coder（DeepSeek）"""

    def _artifact_ext(self):
        return "html"

    async def _handle(self, msg: AgentMessage) -> tuple[str, str]:
        spec = msg.payload.get("spec", {})
        rework = msg.payload.get("rework", False)
        issues = msg.payload.get("issues", [])

        if rework:
            extra = f"\n\n【上次验收意见，请修正】\n{json.dumps(issues, ensure_ascii=False)}"
        else:
            extra = ""

        prompt = f"""你是前端工程师。请按规格交付代码。要求自包含、可直接运行、有基本样式。

【任务规格】
{json.dumps(spec, ensure_ascii=False, indent=2)}{extra}

直接输出 HTML/CSS/JS 代码（如有），不要解释。"""

        code = self.llm_chat([
            {"role": "system", "content": "你是资深前端工程师，代码简洁可运行。"},
            {"role": "user", "content": prompt},
        ])
        return code, f"前端交付：{spec.get('deliverable', '')}，长度 {len(code)} 字符"


class BECoder(AgentBase):
    """后端 Coder（MiniMax）"""

    def _artifact_ext(self):
        return "py"

    async def _handle(self, msg: AgentMessage) -> tuple[str, str]:
        spec = msg.payload.get("spec", {})
        rework = msg.payload.get("rework", False)
        issues = msg.payload.get("issues", [])

        if rework:
            extra = f"\n\n【上次验收意见，请修正】\n{json.dumps(issues, ensure_ascii=False)}"
        else:
            extra = ""

        prompt = f"""你是后端工程师。请按规格交付 Python 代码（FastAPI 风格），含基本错误处理。

【任务规格】
{json.dumps(spec, ensure_ascii=False, indent=2)}{extra}

直接输出代码，不要解释。"""

        code = self.llm_chat([
            {"role": "system", "content": "你是资深后端工程师，写规范可维护的 Python 代码。"},
            {"role": "user", "content": prompt},
        ])
        return code, f"后端交付：{spec.get('deliverable', '')}，长度 {len(code)} 字符"


# ============================================================
# 2. 启动函数
# ============================================================
async def main():
    # 启动前配置校验
    config.validate()
    logger.info("=" * 60)
    logger.info("AgentHub 启动中...")
    logger.info("=" * 60)

    bus = EventBus()
    store = Store()

    # 初始化所有角色
    hub = Hub(bus, store)
    pm = PMAgent("pm", bus, store)
    coder_fe = FECoder("coder_fe", bus, store)
    coder_be = BECoder("coder_be", bus, store)

    # 启动
    await hub.start()
    await pm.start()
    await coder_fe.start()
    await coder_be.start()

    # 用户下旨
    requirement = "做一个登录页面，要求：邮箱+密码登录，前端要有表单校验，后端要返回 JWT token，密码用 bcrypt 存储"
    logger.info(f"\n👑 用户下旨：{requirement}\n")

    # 创建顶层任务并交给 PM
    root_task = store.create_task(
        title="用户下旨：登录系统",
        spec={"requirement": requirement},
        assignee="pm",
        actor="user",
    )
    store.transition(root_task["task_id"], TaskState.SUBMITTED, actor="user")
    store.transition(root_task["task_id"], TaskState.CHECKING, actor="system")

    await bus.publish(AgentMessage(
        from_agent="user", to_agent="pm",
        type=MsgType.REQUEST,
        task_id=root_task["task_id"],
        trace_id=root_task["trace_id"],
        payload={"requirement": requirement},
    ))

    # 等待所有任务结束（最多等 5 分钟）
    for _ in range(300):
        await asyncio.sleep(1)
        tasks = store.list_tasks()
        non_terminal = [t for t in tasks if t["state"] not in
                        (TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED, TaskState.BLOCKED)]
        if not non_terminal:
            break

    # 打印最终状态
    logger.info("\n" + "=" * 60)
    logger.info("🏁 任务全部结束，最终状态：")
    logger.info("=" * 60)
    for t in store.list_tasks():
        verdict = ""
        if t.get("review_result"):
            verdict = f" | verdict={t['review_result'].get('verdict')}"
        logger.info(f"  [{t['state']:10s}] {t['task_id']} | {t['title']}{verdict}")

    # 打印审计概览
    logger.info("\n📜 审计日志（最近 10 条）：")
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    for row in conn.execute(
        "SELECT ts, actor, action, from_state, to_state FROM audit_log "
        "ORDER BY id DESC LIMIT 10"
    ):
        logger.info(f"  {row[0]:.0f} {row[1]:10s} {row[2]:22s} {row[3] or '-':10s} → {row[4] or '-'}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
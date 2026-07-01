"""
Agent 基类集合

- WorkerAgent:   简单的请求/事件处理 Agent，用于 PM 等编排型角色
- AgentBase:     带自动提交验收的 Agent，用于 Coder；QA 通过重写 _on_review_submit 复用
"""
import asyncio
import logging
import os
from typing import Callable, Awaitable

import config
from .bus import EventBus
from .llm import llm, LLMError
from .protocol import AgentMessage, MsgType, make_review_submit
from .store import Store
from .state import TaskState

logger = logging.getLogger(__name__)

# 工件存储目录
ARTIFACTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "artifacts")


def save_artifact(task_id: str, content: str, ext: str = "md") -> str:
    """把 agent 的产出保存到 data/artifacts/<task_id>.<ext>，返回 file:// 引用"""
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    path = os.path.join(ARTIFACTS_DIR, f"{task_id}.{ext}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"file://{path}"


def load_artifact(artifact_ref: str) -> str:
    """读取 artifact_ref 指向的文件内容"""
    if artifact_ref.startswith("file://"):
        path = artifact_ref[len("file://"):]
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return artifact_ref


def strip_thinking_tags(text: str) -> str:
    """
    去除模型输出中的 <think>...</think> 思考过程块。
    某些模型（如 DeepSeek/R1、部分 MiniMax）会在正式输出前附加思考标签，
    影响后续对产出的判断。
    """
    import re
    text = re.sub(r"^\s*<think>.*?</think>\s*", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    return text.strip()


# ---------------------------------------------------------------------------
# WorkerAgent：用于 PM 等编排型角色，只处理 REQUEST/EVENT，不自动提交验收
# ---------------------------------------------------------------------------
class WorkerAgent:
    """简单的请求/事件处理 Agent 基类"""

    def __init__(self, agent_id: str, bus: EventBus, store: Store):
        assert agent_id in config.AGENTS, f"agent_id '{agent_id}' 未在 config.AGENTS 注册"
        self.agent_id = agent_id
        self.bus = bus
        self.store = store
        self._running = False

    async def start(self):
        await self.bus.register(self.agent_id)
        self._running = True
        logger.info(f"[{self.agent_id}] agent started")
        asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False

    async def _loop(self):
        while self._running:
            try:
                msg: AgentMessage = await self.bus.subscribe(self.agent_id)
                asyncio.create_task(self._dispatch(msg))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[{self.agent_id}] loop error: {e}")

    async def _dispatch(self, msg: AgentMessage):
        try:
            if msg.type in (MsgType.REQUEST, MsgType.EVENT, MsgType.ERROR):
                await self._handle(msg)
            else:
                logger.info(f"[{self.agent_id}] ignored msg from {msg.from_agent} type={msg.type}")
        except Exception as e:
            logger.exception(f"[{self.agent_id}] dispatch failed: {e}")

    async def _handle(self, msg: AgentMessage):
        """子类实现"""
        raise NotImplementedError

    def llm_chat(self, messages: list[dict], **kwargs) -> str:
        return llm.chat(self.agent_id, messages, **kwargs)

    def llm_json(self, messages: list[dict], **kwargs) -> dict:
        return llm.chat_json(self.agent_id, messages, **kwargs)


# ---------------------------------------------------------------------------
# AgentBase：带自动提交验收的 Agent，用于 Coder；QA 通过重写 _on_review_submit 复用
# ---------------------------------------------------------------------------
class AgentBase:
    """成员 Agent 基类"""

    # 验收目标，默认 QA；子类可覆盖
    review_target: str = "qa"

    def __init__(self, agent_id: str, bus: EventBus, store: Store):
        assert agent_id in config.AGENTS, f"agent_id '{agent_id}' 未在 config.AGENTS 注册"
        self.agent_id = agent_id
        self.bus = bus
        self.store = store
        self._running = False

    async def start(self):
        await self.bus.register(self.agent_id)
        self._running = True
        logger.info(f"[{self.agent_id}] agent started")
        asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False

    async def _loop(self):
        """主循环：监听 inbox，分派消息"""
        while self._running:
            try:
                msg: AgentMessage = await self.bus.subscribe(self.agent_id)
                asyncio.create_task(self._dispatch(msg))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[{self.agent_id}] loop error: {e}")

    async def _dispatch(self, msg: AgentMessage):
        """消息分派"""
        # QA 验收结果（Coder 收到）
        if msg.type == MsgType.REVIEW_RESULT:
            await self._on_review_result(msg)
            return

        # QA 收到 Coder 的验收提交
        if msg.type == MsgType.REVIEW_SUBMIT:
            await self._on_review_submit(msg)
            return

        # 任务下发 / 业务请求
        if msg.type in (MsgType.REQUEST, MsgType.EVENT):
            task_id = msg.task_id
            try:
                if task_id:
                    self.store.transition(task_id, TaskState.SUBMITTED, actor=self.agent_id)
                artifact_text, self_report = await self._handle(msg)
                artifact_text = strip_thinking_tags(artifact_text)
                ext = self._artifact_ext()
                ref = save_artifact(task_id or msg.msg_id, artifact_text, ext)
                self.store.update_artifact(task_id, ref, self_report, actor=self.agent_id)
                if task_id:
                    self.store.transition(task_id, TaskState.CHECKING,
                                          actor=self.agent_id)
                submit_msg = make_review_submit(
                    from_agent=self.agent_id,
                    task_id=task_id or msg.msg_id,
                    artifact_ref=ref,
                    self_report=self_report,
                    evidence=self._collect_evidence(),
                    trace_id=msg.trace_id,
                )
                submit_msg.to_agent = self.review_target
                await self.bus.publish(submit_msg)
                logger.info(f"[{self.agent_id}] submitted for review: {task_id}")
            except Exception as e:
                logger.exception(f"[{self.agent_id}] handle failed: {e}")
                await self.bus.publish(AgentMessage(
                    from_agent=self.agent_id,
                    to_agent="pm",
                    type=MsgType.ERROR,
                    task_id=task_id,
                    trace_id=msg.trace_id,
                    payload={"error": str(e), "stage": "handle"},
                ))

    # ----------------------------------------------------------
    # 子类实现
    # ----------------------------------------------------------
    async def _handle(self, msg: AgentMessage) -> tuple[str, str]:
        """
        子类实现：处理消息，返回 (artifact_text, self_report)
        """
        raise NotImplementedError

    def _artifact_ext(self) -> str:
        """产出文件扩展名，子类可覆盖（如 .py / .md / .html）"""
        return "md"

    def _collect_evidence(self) -> list:
        """收集证据（测试日志等），默认空。子类可覆盖"""
        return []

    # ----------------------------------------------------------
    # 验收结果处理（Coder 侧）
    # ----------------------------------------------------------
    async def _on_review_result(self, msg: AgentMessage):
        payload = msg.payload
        verdict = payload.get("verdict")
        task_id = msg.task_id
        issues = payload.get("issues", [])

        if verdict == "approved":
            logger.info(f"[{self.agent_id}] task approved: {task_id}")
            self.store.transition(task_id, TaskState.DONE,
                                  actor="qa", detail={"verdict": verdict})
        elif verdict == "rejected":
            retry = self.store.inc_retry(task_id)
            max_retry = config.QA_REVIEW_CONFIG["max_retries"]
            self.store.transition(task_id, TaskState.REWORKING,
                                  actor="qa",
                                  detail={"verdict": verdict, "issues": issues, "retry": retry})
            if retry >= max_retry:
                logger.warning(f"[{self.agent_id}] task rejected {retry} times, "
                               f"escalating to blocked: {task_id}")
                self.store.transition(task_id, TaskState.BLOCKED,
                                      actor="qa")
            else:
                logger.info(f"[{self.agent_id}] task rejected ({retry}/{max_retry}), "
                            f"reworking: {task_id}")
                await self._rework(task_id, issues, msg.trace_id)
        elif verdict == "blocked":
            self.store.transition(task_id, TaskState.BLOCKED,
                                  actor="qa", detail={"issues": issues})
            logger.warning(f"[{self.agent_id}] task blocked (needs human): {task_id}")

    async def _rework(self, task_id: str, issues: list, trace_id: str):
        """返工：重新构造一个处理消息发给自己"""
        task = self.store.get_task(task_id)
        recent_issues = issues[-3:] if issues else []
        original_msg = AgentMessage(
            from_agent=self.agent_id,
            to_agent=self.agent_id,
            type=MsgType.REQUEST,
            task_id=task_id,
            trace_id=trace_id,
            payload={
                "spec": task.get("spec") if task else {},
                "rework": True,
                "issues": recent_issues,
            },
        )
        self.store.transition(task_id, TaskState.SUBMITTED,
                              actor=self.agent_id)
        await self.bus.publish(original_msg)

    # ----------------------------------------------------------
    # 验收提交处理（QA 侧）
    # ----------------------------------------------------------
    async def _on_review_submit(self, msg: AgentMessage):
        """默认无操作；QA 子类重写此方法来执行验收"""
        logger.warning(f"[{self.agent_id}] received REVIEW_SUBMIT but no handler implemented")

    # ----------------------------------------------------------
    # LLM 调用便捷方法
    # ----------------------------------------------------------
    def llm_chat(self, messages: list[dict], **kwargs) -> str:
        return llm.chat(self.agent_id, messages, **kwargs)

    def llm_json(self, messages: list[dict], **kwargs) -> dict:
        return llm.chat_json(self.agent_id, messages, **kwargs)

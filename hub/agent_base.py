"""
成员 Agent 基类 - 自治循环 + 提交验收
职责：
1. 注册到 EventBus，监听自己的 inbox
2. 收到任务/消息后，调用 _handle 自治处理
3. 完成后把产出写入 artifact_ref，发送 review_submit 给中枢
4. 收到 review_result 后判定：approved 收工 / rejected 返工 / blocked 挂起
子类只需实现 _handle(message) → (artifact_text, self_report)
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
    """读取 artifact_ref 指向的文件内容（中枢验收时用）"""
    if artifact_ref.startswith("file://"):
        path = artifact_ref[len("file://"):]
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    # 否则当作内联文本
    return artifact_ref


class AgentBase:
    """成员 Agent 基类"""

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
        # 中枢验收结果
        if msg.type == MsgType.REVIEW_RESULT:
            await self._on_review_result(msg)
            return

        # 任务下发 / 业务请求
        if msg.type in (MsgType.REQUEST, MsgType.EVENT):
            task_id = msg.task_id
            try:
                if task_id:
                    self.store.transition(task_id, TaskState.SUBMITTED, actor=self.agent_id)
                # 调子类处理逻辑
                artifact_text, self_report = await self._handle(msg)
                # 落盘 artifact
                ext = self._artifact_ext()
                ref = save_artifact(task_id or msg.msg_id, artifact_text, ext)
                self.store.update_artifact(task_id, ref, self_report, actor=self.agent_id)
                # 进入验收
                if task_id:
                    self.store.transition(task_id, TaskState.SUBMITTED, TaskState.CHECKING,
                                          actor=self.agent_id)
                submit_msg = make_review_submit(
                    from_agent=self.agent_id,
                    task_id=task_id or msg.msg_id,
                    artifact_ref=ref,
                    self_report=self_report,
                    evidence=self._collect_evidence(),
                    trace_id=msg.trace_id,
                )
                await self.bus.publish(submit_msg)
                logger.info(f"[{self.agent_id}] submitted for review: {task_id}")
            except Exception as e:
                logger.exception(f"[{self.agent_id}] handle failed: {e}")
                # 失败上报
                await self.bus.publish(AgentMessage(
                    from_agent=self.agent_id,
                    to_agent="hub",
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
        - artifact_text: 实际产出（代码/文档/报告），会落盘
        - self_report: 一句话自报告（已做了什么、声称满足什么）
        """
        raise NotImplementedError

    def _artifact_ext(self) -> str:
        """产出文件扩展名，子类可覆盖（如 .py / .md / .html）"""
        return "md"

    def _collect_evidence(self) -> list:
        """收集证据（测试日志等），默认空。子类可覆盖"""
        return []

    # ----------------------------------------------------------
    # 验收结果处理
    # ----------------------------------------------------------
    async def _on_review_result(self, msg: AgentMessage):
        payload = msg.payload
        verdict = payload.get("verdict")
        task_id = msg.task_id
        issues = payload.get("issues", [])

        if verdict == "approved":
            logger.info(f"[{self.agent_id}] ✅ task approved: {task_id}")
            self.store.transition(task_id, TaskState.CHECKING, TaskState.DONE,
                                  actor="hub", detail={"verdict": verdict})
        elif verdict == "rejected":
            retry = self.store.inc_retry(task_id)
            max_retry = config.HUB_CONFIG["max_retries"]
            self.store.transition(task_id, TaskState.CHECKING, TaskState.REWORKING,
                                  actor="hub",
                                  detail={"verdict": verdict, "issues": issues, "retry": retry})
            if retry >= max_retry:
                logger.warning(f"[{self.agent_id}] task rejected {retry} times, "
                               f"escalating to blocked: {task_id}")
                self.store.transition(task_id, TaskState.REWORKING, TaskState.BLOCKED,
                                      actor="hub")
            else:
                logger.info(f"[{self.agent_id}] task rejected ({retry}/{max_retry}), "
                            f"reworking: {task_id}")
                # 重新提交（带 issues 反馈）
                await self._rework(task_id, issues, msg.trace_id)
        elif verdict == "blocked":
            self.store.transition(task_id, TaskState.CHECKING, TaskState.BLOCKED,
                                  actor="hub", detail={"issues": issues})
            logger.warning(f"[{self.agent_id}] task blocked (needs human): {task_id}")

    async def _rework(self, task_id: str, issues: list, trace_id: str):
        """返工：重新构造一个处理消息发给自己。子类可覆盖以更精细控制。"""
        task = self.store.get_task(task_id)
        original_msg = AgentMessage(
            from_agent=self.agent_id,
            to_agent=self.agent_id,
            type=MsgType.REQUEST,
            task_id=task_id,
            trace_id=trace_id,
            payload={
                "spec": task.get("spec") if task else {},
                "rework": True,
                "issues": issues,
            },
        )
        # 转回 SUBMITTED 状态再触发 _dispatch
        self.store.transition(task_id, TaskState.REWORKING, TaskState.SUBMITTED,
                              actor=self.agent_id)
        await self.bus.publish(original_msg)

    # ----------------------------------------------------------
    # LLM 调用便捷方法
    # ----------------------------------------------------------
    def llm_chat(self, messages: list[dict], **kwargs) -> str:
        return llm.chat(self.agent_id, messages, **kwargs)

    def llm_json(self, messages: list[dict], **kwargs) -> dict:
        return llm.chat_json(self.agent_id, messages, **kwargs)
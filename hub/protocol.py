"""
消息协议 - Agent 之间、Agent↔中枢 的统一消息格式
设计原则：
- 大产出走 artifact_ref（文件路径/URL），不塞进 payload，避免堵塞消息总线
- trace_id 贯穿整个任务链路，便于审计和回放
- reply_to 支持请求-响应模式
"""
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# 消息类型枚举
class MsgType:
    REQUEST        = "request"          # 业务请求（agent→agent / 用户→agent）
    RESPONSE       = "response"         # 业务响应
    EVENT          = "event"            # 事件广播
    REVIEW_SUBMIT  = "review_submit"    # 提交验收（agent→hub）
    REVIEW_RESULT  = "review_result"    # 验收结果（hub→agent）
    ERROR          = "error"            # 错误上报
    SYSTEM         = "system"           # 系统消息（启停、心跳）


@dataclass
class AgentMessage:
    """统一的 agent 间消息体"""
    # 必填
    from_agent: str                      # 发送方 agent_id
    to_agent: str                        # 接收方 agent_id / "broadcast" / "hub"
    type: str                            # 见 MsgType
    # 可选
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    trace_id: str = ""                   # 任务链路 id（空则自动等于 msg_id）
    task_id: Optional[str] = None        # 关联的任务 id
    payload: dict = field(default_factory=dict)
    artifact_ref: Optional[str] = None   # 大产出引用（file://、url、文本路径）
    reply_to: Optional[str] = None       # 期望哪个 msg_id 的回复
    ts: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = self.msg_id

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentMessage":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def __repr__(self):
        return (f"<Msg {self.msg_id} {self.from_agent}→{self.to_agent} "
                f"type={self.type} task={self.task_id}>")


def make_review_submit(
    from_agent: str,
    task_id: str,
    artifact_ref: str,
    self_report: str,
    evidence: list = None,
    trace_id: str = "",
) -> AgentMessage:
    """构造提交验收消息"""
    return AgentMessage(
        from_agent=from_agent,
        to_agent="hub",
        type=MsgType.REVIEW_SUBMIT,
        task_id=task_id,
        trace_id=trace_id,
        artifact_ref=artifact_ref,
        payload={
            "self_report": self_report,
            "evidence": evidence or [],
        },
    )


def make_review_result(
    task_id: str,
    verdict: str,           # approved / rejected / blocked
    issues: list,
    confidence: float,
    next_action: str = "",
    trace_id: str = "",
) -> AgentMessage:
    """构造验收结果消息（中枢 → 提交方 agent）"""
    return AgentMessage(
        from_agent="hub",
        to_agent="*",        # 路由时替换为提交方
        type=MsgType.REVIEW_RESULT,
        task_id=task_id,
        trace_id=trace_id,
        payload={
            "verdict":     verdict,
            "issues":      issues,
            "confidence":  confidence,
            "next_action": next_action,
        },
    )
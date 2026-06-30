"""
中枢（Hub） - 验收 + 风控
不参与 agent 之间的业务消息，只接收 review_submit，返回 review_result。

三段式流水线：
    ① 自动化检查（规则，廉价）：格式 / 完整性 / 红线词
    ② LLM 验收（智能）：用 GLM 独立审查产出是否符合规格
    ③ 风险评估（按需）：confidence 低、命中风险词、越权操作 → 升级人工

关键设计：中枢用独立 LLM（GLM），不与成员 agent 共享 provider+model，防自审失效。
"""
import asyncio
import json
import logging
from typing import Optional

import config
from .agent_base import load_artifact
from .bus import EventBus
from .llm import llm, LLMError
from .protocol import AgentMessage, MsgType, make_review_result
from .state import TaskState
from .store import Store

logger = logging.getLogger(__name__)


class Hub:
    """中枢：验收 + 风控"""

    def __init__(self, bus: EventBus, store: Store):
        self.bus = bus
        self.store = store

    async def start(self):
        await self.bus.register("hub")
        logger.info("[hub] 中枢已启动，等待验收请求...")
        asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                msg = await self.bus.subscribe("hub")
                asyncio.create_task(self._handle(msg))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[hub] loop error: {e}")

    async def _handle(self, msg: AgentMessage):
        if msg.type == MsgType.REVIEW_SUBMIT:
            await self._on_review_submit(msg)
        elif msg.type == MsgType.ERROR:
            await self._on_error(msg)
        # 中枢不处理普通 REQUEST/EVENT

    # ----------------------------------------------------------
    # 验收主流程
    # ----------------------------------------------------------
    async def _on_review_submit(self, msg: AgentMessage):
        task_id = msg.task_id
        task = self.store.get_task(task_id)
        if not task:
            logger.warning(f"[hub] 收到未知 task 的验收请求: {task_id}")
            return

        artifact_text = load_artifact(msg.artifact_ref)
        self_report = msg.payload.get("self_report", "")
        evidence = msg.payload.get("evidence", [])
        spec = task.get("spec", {})

        logger.info(f"[hub] 开始验收 {task_id}（提交方: {msg.from_agent}）")

        # ───── ① 自动化检查 ─────
        auto_result = self._auto_check(artifact_text, self_report, spec)
        if not auto_result["passed"]:
            logger.info(f"[hub] {task_id} 自动化检查未通过")
            await self._reply(task_id, msg.from_agent, msg.trace_id,
                              verdict="rejected",
                              issues=auto_result["issues"],
                              confidence=1.0,
                              next_action="按 issues 修复后重新提交")
            return

        # ───── ② LLM 验收 ─────
        try:
            review = await asyncio.wait_for(
                self._llm_review(spec, self_report, artifact_text, evidence),
                timeout=config.HUB_CONFIG["review_timeout_sec"],
            )
        except asyncio.TimeoutError:
            await self._reply(task_id, msg.from_agent, msg.trace_id,
                              verdict="blocked",
                              issues=["中枢验收超时，转人工确认"],
                              confidence=0.0,
                              next_action="人工介入")
            return
        except LLMError as e:
            logger.exception(f"[hub] LLM 验收失败: {e}")
            await self._reply(task_id, msg.from_agent, msg.trace_id,
                              verdict="blocked",
                              issues=[f"中枢 LLM 异常: {e}"],
                              confidence=0.0,
                              next_action="稍后重试")
            return

        verdict = review.get("verdict", "rejected")
        confidence = float(review.get("confidence", 0.5))
        issues = review.get("issues", [])
        missing = review.get("missing", [])

        # ───── ③ 风险评估 ─────
        risk_flags = self._risk_assess(artifact_text, review, spec)
        if risk_flags:
            # 高风险直接升级
            if any(r["severity"] == "high" for r in risk_flags):
                await self._reply(task_id, msg.from_agent, msg.trace_id,
                                  verdict="blocked",
                                  issues=[r["msg"] for r in risk_flags] + issues,
                                  confidence=confidence,
                                  next_action="人工复核：风险超阈值")
                return

        # 信心不足也升级
        if confidence < config.HUB_CONFIG["confidence_threshold"]:
            await self._reply(task_id, msg.from_agent, msg.trace_id,
                              verdict="blocked",
                              issues=["中枢信心不足（%s）" % round(confidence, 2)] + issues,
                              confidence=confidence,
                              next_action="人工复核")
            return

        # 正常返回
        await self._reply(task_id, msg.from_agent, msg.trace_id,
                          verdict=verdict,
                          issues=issues + missing,
                          confidence=confidence,
                          next_action="approved → 完成" if verdict == "approved"
                                      else "按 issues 修复后重新提交")

    # ----------------------------------------------------------
    # 第一段：自动化规则检查
    # ----------------------------------------------------------
    def _auto_check(self, artifact_text: str, self_report: str,
                    spec: dict) -> dict:
        issues = []

        # 1. 非空
        if not artifact_text or not artifact_text.strip():
            issues.append("产出为空")
        # 2. 自报告非空
        if not self_report.strip():
            issues.append("缺少 self_report")
        # 3. 最小长度（防 agent 偷懒交一行）
        min_len = spec.get("min_artifact_len", 50)
        if len(artifact_text) < min_len:
            issues.append(f"产出过短（{len(artifact_text)} < {min_len}）")
        # 4. 红线词
        for kw in config.HUB_CONFIG["blocked_keywords"]:
            if kw and kw in artifact_text:
                issues.append(f"命中红线词: {kw}")
        # 5. 必填字段（如 spec 声明了 required_keys）
        required = config.HUB_CONFIG.get("auto_check_required_keys", [])
        for k in required:
            if k not in artifact_text:
                issues.append(f"缺少必填标记: {k}")

        return {"passed": len(issues) == 0, "issues": issues}

    # ----------------------------------------------------------
    # 第二段：LLM 验收
    # ----------------------------------------------------------
    async def _llm_review(self, spec: dict, self_report: str,
                          artifact_text: str, evidence: list) -> dict:
        """用 hub agent（GLM）独立验收"""
        prompt = f"""你是 AgentHub 的中枢验收官。下面给你【任务规格】【agent 自报告】【产出】，请独立判断产出是否合格。

【任务规格】
{json.dumps(spec, ensure_ascii=False, indent=2)}

【agent 自报告】
{self_report}

【产出】
{artifact_text[:6000]}

【证据】
{json.dumps(evidence, ensure_ascii=False)}

【验收标准】
1. 产出是否完成 spec 中声明的目标
2. 自报告与产出是否一致（防 agent 虚报）
3. 是否有明显遗漏、错误、敷衍
4. 不被自报告的说辞带偏，以产出事实为准

严格输出 JSON：
{{
  "verdict": "approved" | "rejected",
  "confidence": 0.0-1.0,
  "issues": ["问题1", "问题2"],
  "missing": ["缺失项1"]
}}
"""
        result = llm.chat_json("hub", [
            {"role": "system", "content": "你是一名严格、独立的技术验收官。只看事实，不看说辞。"},
            {"role": "user", "content": prompt},
        ])
        # 健壮性兜底
        result.setdefault("verdict", "rejected")
        result.setdefault("confidence", 0.5)
        result.setdefault("issues", [])
        result.setdefault("missing", [])
        return result

    # ----------------------------------------------------------
    # 第三段：风险评估
    # ----------------------------------------------------------
    def _risk_assess(self, artifact_text: str, review: dict,
                     spec: dict) -> list[dict]:
        """识别风险并给出 severity。这里只做规则级，复杂风控可再叠 LLM"""
        risks = []
        text_lower = artifact_text.lower()

        # 危险操作
        danger_keywords = [
            ("rm -rf",           "high",   "疑似破坏性命令 rm -rf"),
            ("drop table",       "high",   "疑似删表 SQL"),
            ("format c:",        "high",   "疑似格式化磁盘"),
            ("sudo ",            "medium", "使用 sudo 提权"),
            ("access_key",       "medium", "疑似泄露 access_key"),
            ("api_key",          "medium", "疑似泄露 api_key"),
            ("password=",        "medium", "疑似明文密码"),
        ]
        for kw, sev, msg in danger_keywords:
            if kw in text_lower:
                risks.append({"severity": sev, "msg": msg})

        return risks

    # ----------------------------------------------------------
    # 错误上报
    # ----------------------------------------------------------
    async def _on_error(self, msg: AgentMessage):
        err = msg.payload
        logger.error(f"[hub] 收到错误上报 from {msg.from_agent}: {err}")
        if msg.task_id:
            try:
                self.store.transition(msg.task_id, TaskState.CHECKING, TaskState.FAILED,
                                      actor=msg.from_agent, detail=err)
            except Exception:
                pass  # 状态可能本就不在 CHECKING

    # ----------------------------------------------------------
    # 发送验收结果
    # ----------------------------------------------------------
    async def _reply(self, task_id: str, to_agent: str, trace_id: str,
                     verdict: str, issues: list, confidence: float,
                     next_action: str):
        self.store.save_review(task_id, {
            "verdict": verdict,
            "issues": issues,
            "confidence": confidence,
            "next_action": next_action,
            "reviewer": "hub",
        }, actor="hub")
        result_msg = make_review_result(
            task_id=task_id,
            verdict=verdict,
            issues=issues,
            confidence=confidence,
            next_action=next_action,
            trace_id=trace_id,
        )
        result_msg.to_agent = to_agent   # 替换占位符 *
        await self.bus.publish(result_msg)
        logger.info(f"[hub] 验收完成 {task_id}: {verdict} (conf={confidence:.2f})")
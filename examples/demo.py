"""
AgentHub 最小可跑 Demo（PM + QA + Coder 架构）
流程：
    用户下旨 → PM(Kimi) 拆解为顺序小模块 → 逐个交给 coder_fe(MiniMax) / coder_be(GLM)
    → Coder 实现后自动提交 QA(DeepSeek) 验收 → QA 把结果同步给 PM
    → PM 推进下一个模块 → 全部完成后汇总报告

运行前：
    1. cp .env.example .env 并填好 4 个 API key 与飞书凭证
    2. pip install -r requirements.txt
    3. python examples/demo.py
"""
import asyncio
import json
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from hub.bus import EventBus
from hub.store import Store
from hub.protocol import AgentMessage, MsgType, make_review_result
from hub.state import TaskState
from hub.agent_base import AgentBase, WorkerAgent, load_artifact

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("demo")


# ============================================================
# 1. PM：需求拆解 + 顺序编排 + 人类通知 + 阻塞后人工干预
# ============================================================
class PMAgent(WorkerAgent):
    """产品经理：拆解需求，顺序派发小模块，根据 QA 结果推进；
    模块被阻塞后保留上下文，等待人类下一步指令。"""

    def __init__(self, agent_id: str, bus: EventBus, store: Store):
        super().__init__(agent_id, bus, store)
        self._root_task_id: str | None = None
        self._trace_id: str = ""
        self._chat_id: str | None = None
        self._modules: list[dict] = []
        self._current_index: int = 0
        self._completed: list[dict] = []
        self._module_map: dict[str, dict] = {}  # task_id -> {index, module}
        self._blocked: bool = False
        self._blocked_reason: str = ""
        self._blocked_task_id: str | None = None

    async def _handle(self, msg: AgentMessage):
        if msg.type == MsgType.REQUEST:
            await self._on_request(msg)
        elif msg.type == MsgType.EVENT and msg.from_agent == "qa":
            await self._on_qa_event(msg)
        elif msg.type == MsgType.ERROR:
            await self._on_error(msg)
        else:
            logger.info(f"[pm] ignored msg from {msg.from_agent} type={msg.type}")

    async def _on_error(self, msg: AgentMessage):
        logger.error(f"[pm] 收到错误上报 from {msg.from_agent}: {msg.payload}")
        if self._root_task_id:
            self.store.transition(self._root_task_id, TaskState.BLOCKED,
                                  actor=self.agent_id, detail={"error": msg.payload})
        await self._finalize(f"Agent {msg.from_agent} 执行出错: {msg.payload.get('error', '')}", blocked=True)

    async def _on_request(self, msg: AgentMessage):
        user_req = msg.payload.get("requirement", "")
        chat_id = msg.payload.get("chat_id")

        # 如果当前聊天有阻塞中的任务，默认当作干预指令处理
        if self._blocked and chat_id and chat_id == self._chat_id:
            intent = self._parse_intervention_intent(user_req)
            if intent == "new":
                # 明确要开新任务：结束旧任务并清空状态
                text = "【AgentHub】已结束当前阻塞任务，开始处理新需求。"
                logger.info(f"[pm] {text}")
                if chat_id:
                    try:
                        from hub.feishu_client import send_text_message
                        bot_cfg = config.FEISHU_BOTS.get("pm")
                        if bot_cfg:
                            send_text_message(bot_cfg, chat_id, text)
                    except Exception as e:
                        logger.exception(f"[pm] 发送提示失败: {e}")
                if self._root_task_id:
                    self.store.transition(self._root_task_id, TaskState.BLOCKED,
                                          actor=self.agent_id, detail={"reason": "用户选择开始新任务"})
                self._clear_state()
            else:
                await self._handle_intervention(user_req, intent)
                return

        # 如果当前聊天有正在运行（未阻塞）的任务，提示用户等待
        if self._root_task_id and not self._blocked and chat_id and chat_id == self._chat_id:
            text = "【AgentHub】当前有任务正在执行中，请等待完成后再发送新需求。如需终止当前任务，请回复'结束'。"
            logger.info(f"[pm] {text}")
            if chat_id:
                try:
                    from hub.feishu_client import send_text_message
                    bot_cfg = config.FEISHU_BOTS.get("pm")
                    if bot_cfg:
                        send_text_message(bot_cfg, chat_id, text)
                except Exception as e:
                    logger.exception(f"[pm] 发送等待提示失败: {e}")
            return

        # 否则作为新需求处理
        await self._start_new_task(msg)

    async def _start_new_task(self, msg: AgentMessage):
        user_req = msg.payload.get("requirement", "")
        self._chat_id = msg.payload.get("chat_id")
        self._trace_id = msg.trace_id or msg.msg_id
        self._completed = []
        self._current_index = 0
        self._module_map = {}
        self._blocked = False
        self._blocked_reason = ""
        self._blocked_task_id = None

        root = self.store.create_task(
            title=f"需求：{user_req[:40]}",
            spec={"requirement": user_req, "source": msg.payload.get("source", "demo")},
            assignee="pm",
            trace_id=self._trace_id,
            actor=self.agent_id,
        )
        self._root_task_id = root["task_id"]
        self.store.transition(self._root_task_id, TaskState.SUBMITTED, actor=self.agent_id)

        modules = self._split_requirement(user_req)
        self._modules = modules

        plan_artifact = json.dumps({
            "requirement": user_req,
            "modules": modules,
        }, ensure_ascii=False, indent=2)
        self.store.update_artifact(
            self._root_task_id,
            self._save_plan(plan_artifact),
            f"已拆解为 {len(modules)} 个顺序模块",
            actor=self.agent_id,
        )

        logger.info(f"[pm] 需求已拆解为 {len(modules)} 个模块，开始顺序执行")
        if self._modules:
            await self._start_module(0)
        else:
            await self._finalize("未拆解出任何模块")

    def _parse_intervention_intent(self, user_text: str) -> str:
        """简单意图识别：修复/继续/跳过/结束/新需求。
        阻塞状态下，默认把用户消息当作对阻塞任务的反馈，除非明确说"新需求"。"""
        text = user_text.lower()
        # 只有明确的新需求关键词才开启新任务
        if any(k in text for k in ["新需求", "重新来", "新开", "新的需求", "换个需求", "新任务"]):
            return "new"
        # 跳过
        if any(k in text for k in ["跳过", "skip", "下一个", "不管", "忽略"]):
            return "skip"
        # 结束
        if any(k in text for k in ["结束", "停止", "取消", "不做了", "cancel"]):
            return "cancel"
        # 其他所有消息都默认是"修复/继续"阻塞任务
        return "fix"

    async def _handle_intervention(self, user_text: str, intent: str):
        logger.info(f"[pm] 收到人类干预，意图={intent}: {user_text}")

        if intent == "cancel":
            await self._finalize("用户取消任务", blocked=True)
            return

        if intent == "skip":
            self._completed.append({
                "module": self._modules[self._current_index],
                "task_id": self._blocked_task_id,
                "skipped": True,
            })
            self._current_index += 1
            self._blocked = False
            self._blocked_reason = ""
            self._blocked_task_id = None
            await self._start_module(self._current_index)
            return

        # intent == fix：尝试修复阻塞模块
        await self._retry_blocked_module(user_text)

    async def _retry_blocked_module(self, human_feedback: str):
        """结合人类反馈，重新做被阻塞的模块"""
        if not self._blocked_task_id:
            logger.error("[pm] 没有阻塞任务却收到修复指令")
            return

        info = self._module_map.get(self._blocked_task_id)
        if not info:
            logger.error(f"[pm] 找不到阻塞任务信息: {self._blocked_task_id}")
            return

        index = info["index"]
        module = info["module"]
        assignee = module["assignee"]

        # 根任务从 BLOCKED 回到 SUBMITTED，表示继续推进
        self.store.transition(self._root_task_id, TaskState.SUBMITTED,
                              actor=self.agent_id, detail={"action": "human_retry", "feedback": human_feedback})

        # 被阻塞的模块任务也回到 SUBMITTED
        self.store.transition(self._blocked_task_id, TaskState.SUBMITTED,
                              actor=self.agent_id, detail={"action": "human_retry", "feedback": human_feedback})

        # 生成结合人类反馈的优化规格
        refined_spec = self._refine_spec(module.get("spec", {}), self._blocked_reason, human_feedback)
        module["spec"] = refined_spec  # 更新本地模块规格

        logger.info(f"[pm] 根据人类反馈重新启动模块 {index + 1}: {module['title']}")

        # 重新发送 REQUEST 给 Coder（带反馈和之前 QA 的 issues）
        await self.bus.publish(AgentMessage(
            from_agent=self.agent_id,
            to_agent=assignee,
            type=MsgType.REQUEST,
            task_id=self._blocked_task_id,
            trace_id=self._trace_id,
            payload={
                "spec": refined_spec,
                "rework": True,
                "issues": [self._blocked_reason, human_feedback],
                "human_feedback": human_feedback,
            },
        ))

        # 重置阻塞标记，等待新的 QA 结果
        self._blocked = False
        self._blocked_reason = ""
        self._blocked_task_id = None

    def _refine_spec(self, spec: dict, blocked_reason: str, human_feedback: str) -> dict:
        """调用 LLM 结合人类反馈生成更明确的模块规格"""
        prompt = f"""你是资深产品经理。当前模块被 QA 阻塞，请根据阻塞原因和人类反馈，输出一份更明确、可执行的模块规格。

原规格：
{json.dumps(spec, ensure_ascii=False, indent=2)}

QA 阻塞原因：
{blocked_reason}

人类反馈：
{human_feedback}

请输出 JSON：
{{
  "goal": "模块目标",
  "requirements": ["要求1", "要求2"],
  "deliverable": "交付物描述"
}}

注意：
- 规格必须具体、可验收，避免模糊描述。
- 如果人类说"不需要后端"，确保不要要求后端接口。
- 如果 QA 抱怨"缺少目录结构"，但人类只需要单文件，明确要求"交付单文件 HTML，不强制项目目录"。"""

        try:
            result = self.llm_json([
                {"role": "system", "content": "你是资深产品经理，擅长把阻塞问题转化为清晰的修复规格。只输出 JSON。"},
                {"role": "user", "content": prompt},
            ])
            return {
                "goal": result.get("goal", spec.get("goal", "")),
                "requirements": result.get("requirements", spec.get("requirements", [])),
                "deliverable": result.get("deliverable", spec.get("deliverable", "")),
            }
        except Exception as e:
            logger.warning(f"[pm] refine_spec LLM failed: {e}, use original spec with feedback")
            return {
                "goal": spec.get("goal", ""),
                "requirements": spec.get("requirements", []) + [f"人类反馈：{human_feedback}"],
                "deliverable": spec.get("deliverable", ""),
            }

    def _split_requirement(self, user_req: str) -> list[dict]:
        """调用 Kimi 把需求拆成顺序小模块"""
        prompt = f"""你是资深产品经理。请把下面用户需求拆解为 2-5 个可顺序交付的小模块。
每个模块只交给一个角色（coder_fe 前端 或 coder_be 后端），粒度要小，单个模块必须能一次交付完成。

重要约束：
- 前端模块默认交付单文件 HTML（内联 CSS/JS），不要要求项目目录或多文件结构，除非用户明确需要。
- 后端模块默认交付单文件 Python/FastAPI，不要要求完整项目结构，除非用户明确需要。
- 每个模块的 spec 必须具体、可验收，避免"初始化项目目录"这种和实际交付形式冲突的要求。

用户需求：{user_req}

严格输出 JSON：
{{
  "title": "任务标题",
  "modules": [
    {{
      "title": "模块标题",
      "assignee": "coder_fe 或 coder_be",
      "spec": {{
        "goal": "模块目标",
        "requirements": ["要求1", "要求2"],
        "deliverable": "交付物描述"
      }}
    }}
  ]
}}"""
        result = self.llm_json([
            {"role": "system", "content": "你是资深产品经理，擅长把复杂需求拆成可顺序交付的小模块。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ])
        modules = result.get("modules", [])
        if not modules:
            modules = [{
                "title": result.get("title", "整体实现"),
                "assignee": "coder_fe",
                "spec": {"goal": user_req, "requirements": [user_req], "deliverable": "完整实现"},
            }]
        return modules

    def _save_plan(self, content: str) -> str:
        os.makedirs("data/artifacts", exist_ok=True)
        path = os.path.join("data", "artifacts", f"{self._root_task_id}_plan.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"file://{path}"

    async def _start_module(self, index: int):
        if index >= len(self._modules):
            await self._finalize("所有模块已验收通过")
            return

        module = self._modules[index]
        assignee = module["assignee"]
        spec = module.get("spec", {})

        task = self.store.create_task(
            title=f"{module['title']} [{assignee.upper()}]",
            spec={
                "module_index": index,
                **spec,
                "source": "pm_orchestration",
            },
            assignee=assignee,
            trace_id=self._trace_id,
            actor=self.agent_id,
        )
        task_id = task["task_id"]
        self._module_map[task_id] = {"index": index, "module": module}
        self.store.transition(task_id, TaskState.SUBMITTED, actor=self.agent_id)

        logger.info(f"[pm] 启动模块 {index + 1}/{len(self._modules)}: {module['title']} -> {assignee}")

        await self.bus.publish(AgentMessage(
            from_agent=self.agent_id,
            to_agent=assignee,
            type=MsgType.REQUEST,
            task_id=task_id,
            trace_id=self._trace_id,
            payload={"spec": spec},
        ))

    async def _on_qa_event(self, msg: AgentMessage):
        task_id = msg.task_id
        payload = msg.payload
        verdict = payload.get("verdict")
        issues = payload.get("issues", [])

        info = self._module_map.get(task_id)
        if not info:
            logger.warning(f"[pm] 收到未知 task_id 的 QA 事件: {task_id}")
            return

        index = info["index"]
        module = info["module"]
        logger.info(f"[pm] 模块 {index + 1} QA 结果: {verdict}")

        if verdict == "approved":
            self._completed.append({"module": module, "task_id": task_id})
            self._current_index = index + 1
            await self._start_module(self._current_index)
        elif verdict == "rejected":
            logger.info(f"[pm] 模块 {index + 1} 被打回，Coder 自动返工: {issues}")
        elif verdict == "blocked":
            self._blocked = True
            self._blocked_reason = "\n".join(issues) if issues else "未知原因"
            self._blocked_task_id = task_id
            await self._notify_blocked(index + 1, self._blocked_reason)

    async def _notify_blocked(self, module_index: int, reason: str):
        text = (
            f"【AgentHub 阻塞通知】\n"
            f"模块 {module_index}/{len(self._modules)} 被 QA 阻塞。\n"
            f"原因：{reason}\n\n"
            f"请回复指令：\n"
            f"• '修复这个问题' / '继续' → 结合你的反馈让 Coder 重新做\n"
            f"• '跳过' → 跳过该模块，继续做下一个\n"
            f"• '结束' / '取消' → 终止本次任务"
        )
        logger.info(f"[pm] {text}")

        if self._root_task_id:
            self.store.update_artifact(
                self._root_task_id,
                self._save_plan(json.dumps({
                    "requirement": self._modules[0].get("spec", {}).get("goal", "") if self._modules else "",
                    "total_modules": len(self._modules),
                    "completed_modules": len(self._completed),
                    "blocked_module_index": self._current_index,
                    "blocked_reason": reason,
                }, ensure_ascii=False, indent=2)),
                f"模块 {module_index} 被阻塞，等待人类干预",
                actor=self.agent_id,
            )
            self.store.transition(self._root_task_id, TaskState.BLOCKED,
                                  actor=self.agent_id, detail={"reason": reason})

        await self.bus.publish(AgentMessage(
            from_agent=self.agent_id,
            to_agent=self.agent_id,
            type=MsgType.EVENT,
            task_id=self._root_task_id,
            trace_id=self._trace_id,
            payload={"text": text, "chat_id": self._chat_id},
        ))

        if self._chat_id:
            try:
                from hub.feishu_client import send_text_message
                bot_cfg = config.FEISHU_BOTS.get("pm")
                if bot_cfg:
                    send_text_message(bot_cfg, self._chat_id, text)
            except Exception as e:
                logger.exception(f"[pm] 通知人类失败: {e}")

    def _clear_state(self):
        """清空 PM 内部状态"""
        self._root_task_id = None
        self._modules = []
        self._current_index = 0
        self._completed = []
        self._module_map = {}
        self._blocked = False
        self._blocked_reason = ""
        self._blocked_task_id = None

    async def _finalize(self, reason: str, blocked: bool = False):
        summary = {
            "requirement": self._modules[0].get("spec", {}).get("goal", "") if self._modules else "",
            "total_modules": len(self._modules),
            "completed_modules": len(self._completed),
            "reason": reason,
            "completed": self._completed,
        }
        report = json.dumps(summary, ensure_ascii=False, indent=2)
        if self._root_task_id:
            self.store.update_artifact(
                self._root_task_id,
                self._save_plan(report),
                reason,
                actor=self.agent_id,
            )
            if blocked:
                self.store.transition(self._root_task_id, TaskState.BLOCKED, actor=self.agent_id, detail=summary)
            else:
                self.store.transition(self._root_task_id, TaskState.DONE, actor=self.agent_id, detail=summary)

        text = f"【AgentHub 编排结果】\n{reason}\n完成模块：{len(self._completed)}/{len(self._modules)}"
        logger.info(f"[pm] {text}")

        await self.bus.publish(AgentMessage(
            from_agent=self.agent_id,
            to_agent=self.agent_id,
            type=MsgType.EVENT,
            task_id=self._root_task_id,
            trace_id=self._trace_id,
            payload={"text": text, "chat_id": self._chat_id},
        ))

        if self._chat_id:
            try:
                from hub.feishu_client import send_text_message
                bot_cfg = config.FEISHU_BOTS.get("pm")
                if bot_cfg:
                    send_text_message(bot_cfg, self._chat_id, text)
            except Exception as e:
                logger.exception(f"[pm] 通知人类失败: {e}")

        # 非阻塞终态才清空上下文
        if not self._blocked:
            self._clear_state()


# ============================================================
# 2. QA：功能/代码验收
# ============================================================
class QAAgent(AgentBase):
    """QA：验收 Coder 产出，返回 verdict + issues，并通知 PM"""

    async def _on_review_submit(self, msg: AgentMessage):
        task_id = msg.task_id
        artifact_ref = msg.artifact_ref
        self_report = msg.payload.get("self_report", "")
        spec = {}
        task_title = ""
        assignee = ""
        task = self.store.get_task(task_id) if task_id else None
        if task:
            spec = task.get("spec", {})
            task_title = task.get("title", "")
            assignee = task.get("assignee", "")

        self.store.transition(task_id, TaskState.QA_REVIEWING, actor=self.agent_id)

        try:
            artifact_text = load_artifact(artifact_ref) if artifact_ref else ""
            review = self._review(artifact_text, spec, self_report, task_title, assignee)
            verdict = review.get("verdict", "blocked")
            issues = review.get("issues", [])
            confidence = review.get("confidence", 0.0)

            self.store.save_review(task_id, review, actor=self.agent_id)

            # 根据 verdict 推进状态
            if verdict == "approved":
                self.store.transition(task_id, TaskState.QA_APPROVED, actor=self.agent_id, detail=review)
            elif verdict == "rejected":
                self.store.transition(task_id, TaskState.QA_REJECTED, actor=self.agent_id, detail=review)
            elif verdict == "blocked":
                self.store.transition(task_id, TaskState.QA_BLOCKED, actor=self.agent_id, detail=review)

            # 发送 REVIEW_RESULT 给原 Coder
            result = make_review_result(
                task_id=task_id,
                verdict=verdict,
                issues=issues,
                confidence=confidence,
                trace_id=msg.trace_id,
            )
            result.to_agent = msg.from_agent
            await self.bus.publish(result)

            # 发送 EVENT 给 PM
            await self.bus.publish(AgentMessage(
                from_agent=self.agent_id,
                to_agent="pm",
                type=MsgType.EVENT,
                task_id=task_id,
                trace_id=msg.trace_id,
                payload={"verdict": verdict, "issues": issues, "confidence": confidence},
            ))

            logger.info(f"[qa] 验收完成 {task_id}: {verdict}")
        except Exception as e:
            logger.exception(f"[qa] 验收失败: {e}")
            # 失败按 blocked 处理
            self.store.transition(task_id, TaskState.QA_BLOCKED, actor=self.agent_id,
                                  detail={"error": str(e)})
            err_result = make_review_result(
                task_id=task_id,
                verdict="blocked",
                issues=[f"QA 验收异常: {e}"],
                confidence=0.0,
                trace_id=msg.trace_id,
            )
            err_result.to_agent = msg.from_agent
            await self.bus.publish(err_result)

    def _review(self, artifact_text: str, spec: dict, self_report: str, task_title: str = "", assignee: str = "") -> dict:
        """调用 DeepSeek 审查代码/产出，重点关注功能正确性和可运行性"""
        role_hint = ""
        if "coder_fe" in assignee:
            role_hint = (
                "这是前端模块。单文件 HTML（含内联 CSS/JS）是合理交付，"
                "不要要求必须有项目目录、多文件结构或外部资源引用。"
                "重点关注：页面能否运行、功能是否实现、交互是否正常。"
            )
        elif "coder_be" in assignee:
            role_hint = (
                "这是后端模块。单文件 FastAPI/Python 是合理交付，"
                "不要要求必须有完整项目结构、数据库迁移文件或 Dockerfile。"
                "重点关注：API 逻辑是否正确、能否运行、关键功能是否实现。"
            )

        prompt = f"""你是一名务实的 QA 工程师。请根据任务规格验收 Coder 交付的产出，输出 JSON。

任务标题：{task_title or "未命名"}
任务规格：
{json.dumps(spec, ensure_ascii=False, indent=2)}

Coder 自评：{self_report}

交付内容（前 12000 字符）：
```
{artifact_text[:12000]}
```

请输出 JSON：
{{
  "verdict": "approved 或 rejected 或 blocked",
  "confidence": 0.0-1.0,
  "issues": ["问题1", "问题2"]
}}

判断标准（请严格遵守）：
- approved：功能满足规格核心要求，代码基本完整可运行，没有阻塞性问题。
- rejected：主要功能满足但有小缺陷（如边界处理、小 bug、样式偏差、命名不规范），Coder 返工即可。
- blocked：只有以下严重情况才使用：
  * 代码无法运行或缺少关键功能
  * 严重安全漏洞（如明文存储密码、SQL 注入）
  * 交付物与规格完全不符（例如规格要求文档报告，交付的是代码）
  * 无法判断交付物是什么

特别说明：
{role_hint}
- 不要因缺少注释、文档、测试用例而阻塞或拒绝。
- 不要因代码风格、缩进、命名等纯风格问题而阻塞。
- 如果规格描述有歧义，优先按"功能已实现"判定为 approved 或 rejected，不要 blocked。
- 每个 issue 必须具体、可修复，不要写空泛的评价。"""

        return self.llm_json([
            {"role": "system", "content": "你是务实、公正的 QA 工程师，严格按功能标准验收，不轻易阻塞。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ])


# ============================================================
# 3. 前端 Coder（MiniMax）
# ============================================================
class FECoder(AgentBase):
    """前端 Coder（MiniMax 2.7）"""

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

        prompt = f"""你是一名资深前端工程师。请根据以下任务规格，交付一个完整、可独立运行的单文件 HTML 页面。

要求：
1. 必须包含完整的 HTML 骨架（<!DOCTYPE html>、<html>、<head>、<body>）。
2. 必须包含内联 CSS（<style>）和内联 JavaScript（<script>），不要依赖外部文件。
3. 必须实现任务规格中所有功能点，包括：表单校验、接口调用、状态管理、错误提示、加载状态、Token 存储等。
4. 如果规格要求单元测试，请在 <script> 中预留测试入口或用简单断言展示关键逻辑。
5. 代码必须完整，不能被截断；如果内容较多，请优先保证核心功能完整可用。
6. 只输出代码，不要输出任何解释、注释说明、Markdown 代码块标记或 <think> 等思考标签。

【任务规格】
{json.dumps(spec, ensure_ascii=False, indent=2)}{extra}

直接输出完整 HTML 代码："""

        code = self.llm_chat([
            {
                "role": "system",
                "content": (
                    "你是资深前端工程师，专门交付可直接运行的单文件 HTML。"
                    "只输出代码，禁止输出解释、Markdown标记、<think>标签或任何非代码内容。"
                    "确保HTML结构完整、JS逻辑完整、功能可运行。"
                ),
            },
            {"role": "user", "content": prompt},
        ])
        return code, f"前端交付：{spec.get('deliverable', '')}，长度 {len(code)} 字符"


# ============================================================
# 4. 后端 Coder（GLM-5.1）
# ============================================================
class BECoder(AgentBase):
    """后端 Coder（GLM-5.1）"""

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

        prompt = f"""你是一名资深后端工程师。请根据以下任务规格，交付一个完整、可运行的 FastAPI 项目代码。

要求：
1. 使用 Python + FastAPI 风格，所有代码放在同一个 .py 文件中即可运行（包括模型、schema、路由、服务逻辑）。
2. 必须实现任务规格中所有功能点：登录/登出/刷新接口、密码哈希校验、JWT 签发与验证、频率限制/防暴力破解、审计日志等。
3. 必须包含基本错误处理和输入校验。
4. 如果规格要求单元测试，请在文件末尾用简单断言或 pytest 风格展示关键测试用例。
5. 代码必须完整，不能被截断；如果内容较多，请优先保证核心 API 接口可运行。
6. 只输出 Python 代码，不要输出任何解释、注释说明、Markdown 代码块标记或 <think> 等思考标签。

【任务规格】
{json.dumps(spec, ensure_ascii=False, indent=2)}{extra}

直接输出完整 Python 代码："""

        code = self.llm_chat([
            {
                "role": "system",
                "content": (
                    "你是资深后端工程师，专门交付可直接运行的 Python/FastAPI 代码。"
                    "只输出代码，禁止输出解释、Markdown标记、<think>标签或任何非代码内容。"
                    "确保路由、模型、认证逻辑完整，代码可在 python xxx.py 或 uvicorn 方式下运行。"
                ),
            },
            {"role": "user", "content": prompt},
        ])
        return code, f"后端交付：{spec.get('deliverable', '')}，长度 {len(code)} 字符"


# ============================================================
# 5. 启动函数
# ============================================================
async def main():
    config.validate()
    logger.info("=" * 60)
    logger.info("AgentHub 启动中...")
    logger.info("=" * 60)

    bus = EventBus()
    store = Store()

    pm = PMAgent("pm", bus, store)
    qa = QAAgent("qa", bus, store)
    coder_fe = FECoder("coder_fe", bus, store)
    coder_be = BECoder("coder_be", bus, store)

    await pm.start()
    await qa.start()
    await coder_fe.start()
    await coder_be.start()

    requirement = "做一个登录页面，要求：邮箱+密码登录，前端要有表单校验，后端要返回 JWT token，密码用 bcrypt 存储"
    logger.info(f"\n👑 用户下旨：{requirement}\n")

    root_task = store.create_task(
        title="用户下旨：登录系统",
        spec={"requirement": requirement},
        assignee="pm",
        actor="user",
    )
    store.transition(root_task["task_id"], TaskState.SUBMITTED, actor="user")

    await bus.publish(AgentMessage(
        from_agent="user", to_agent="pm",
        type=MsgType.REQUEST,
        task_id=root_task["task_id"],
        trace_id=root_task["trace_id"],
        payload={"requirement": requirement},
    ))

    # 等待所有任务结束（最多等 10 分钟）
    for _ in range(600):
        await asyncio.sleep(1)
        tasks = store.list_tasks()
        non_terminal = [t for t in tasks if t["state"] not in TaskState.TERMINAL_STATES]
        if not non_terminal:
            break

    logger.info("\n" + "=" * 60)
    logger.info("🏁 任务全部结束，最终状态：")
    logger.info("=" * 60)
    for t in store.list_tasks():
        verdict = ""
        rr = t.get("review_result")
        if isinstance(rr, dict) and rr.get("verdict"):
            verdict = f" | verdict={rr['verdict']}"
        logger.info(f"  [{t['state']:12s}] {t['task_id']} | {t['title']}{verdict}")

    logger.info("\n📜 审计日志（最近 15 条）：")
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    for row in conn.execute(
        "SELECT ts, actor, action, from_state, to_state FROM audit_log "
        "ORDER BY id DESC LIMIT 15"
    ):
        logger.info(f"  {row[0]:.0f} {row[1]:10s} {row[2]:22s} {row[3] or '-':12s} → {row[4] or '-'}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())

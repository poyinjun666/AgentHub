# AgentHub

> 多 Agent 群聊系统 · 中枢验收式架构
> 参考自三省六部制（edict）的"制度性审核"思想：成员 agent 自治协作，中枢只在产出节点做验收 + 风控。

## 核心理念

- **Agent 之间自治**：成员 agent 直接通过 EventBus 通信，不必每条消息都经过中枢
- **中枢只验收**：在 agent 完成产出、提交验收的关键节点介入，做"自动化检查 + LLM 验收 + 风险评估"三段式审查
- **防自审失效**：中枢用 GLM，成员 agent 用 Kimi/DeepSeek/MiniMax，验收者与被验收者不同源
- **完全可审计**：所有任务状态变更、消息流转落盘 SQLite，可回放、可追溯

## 角色与模型

| Agent ID | 角色 | Provider | 模型 |
|---|---|---|---|
| `hub` | 中枢（验收 + 风控） | GLM（智谱） | glm-4.6 |
| `pm` | 产品经理（需求拆解） | Kimi（月之暗面） | moonshot-v1-8k |
| `coder_fe` | 前端开发 | DeepSeek | deepseek-chat |
| `coder_be` | 后端开发 | MiniMax | abab6.5s-chat |

## 任务状态机

```
created → submitted → checking ─┬→ done       (通过)
                               ├→ reworking  (打回，最多 3 次)
                               └→ blocked    (升级人工)
reworking → submitted (循环)
blocked   → submitted | cancelled
```

非法状态跳转直接拒绝并审计。

## 目录结构

```
AgentHub/
├── config.py              # Provider 池 + 角色绑定 + 启动校验
├── requirements.txt
├── .env.example           # 环境变量模板
├── hub/
│   ├── protocol.py        # AgentMessage 消息协议
│   ├── bus.py             # EventBus（asyncio.Queue）
│   ├── state.py           # 任务状态机
│   ├── store.py           # SQLite TaskStore + 审计日志
│   ├── llm.py             # 通用多 Provider LLM Client
│   ├── agent_base.py      # 成员 Agent 基类
│   └── hub_core.py        # 中枢三段式验收流水线
├── examples/
│   └── demo.py            # 完整可跑 demo（用户下旨 → PM → 双 coder → 验收）
└── data/                  # 运行时数据（SQLite + artifacts）
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API key
cp .env.example .env
# 编辑 .env，填入 4 个 provider 的真实 key

# 3. 校验配置
python config.py

# 4. 跑 demo
python examples/demo.py
```

## 消息协议

```python
AgentMessage(
    from_agent="coder_fe",
    to_agent="hub",               # 或 agent_id / "broadcast"
    type="review_submit",
    task_id="abc123",
    trace_id="abc123",            # 贯穿整个任务链路
    artifact_ref="file://...",    # 大产出走引用
    payload={"self_report": "...", "evidence": [...]},
)
```

## 中枢验收三段式

```
产出进入 → ① 自动化检查（规则）
              ↓ passed
          ② LLM 验收（GLM 独立审查）
              ↓
          ③ 风险评估（danger keywords / confidence）
              ↓
          verdict: approved / rejected / blocked
```

- **第一段**：80% 低级问题（空、过短、红线词）挡掉，不耗 token
- **第二段**：GLM 拿"任务规格 + 产出"独立判断，不给 agent 思考过程（防被说服）
- **第三段**：danger keyword 命中、confidence 不足 → 升级人工

## 后续可扩展

- [ ] EventBus 升级到 Redis Streams（跨进程、可重放）
- [ ] 中枢加 LLM 级风控（不限于关键词）
- [ ] 看板 API（FastAPI 暴露任务/审计/artifact 查询）
- [ ] 钉钉/飞书适配器（接入你现有 `bot.py`）
- [ ] DAG 编排器（多 agent 依赖关系）
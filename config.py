"""
AgentHub 配置中心
- PROVIDERS:  provider 连接信息（key/url/model），与角色无关
- AGENTS:     角色 → provider 绑定 + 调用参数
- HUB_CONFIG: 中枢验收流水线参数
- validate(): 启动校验，防止配错跑偏
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 1. Provider 池（连接信息）
# ============================================================
PROVIDERS = {
    "glm": {
        "api_key":  os.getenv("GLM_API_KEY"),
        "base_url": os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        "model":    os.getenv("GLM_MODEL", "glm-4.6"),
    },
    "kimi": {
        "api_key":  os.getenv("KIMI_API_KEY"),
        "base_url": os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
        "model":    os.getenv("KIMI_MODEL", "moonshot-v1-8k"),
    },
    "deepseek": {
        "api_key":  os.getenv("DEEPSEEK_API_KEY"),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model":    os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    },
    "minimax": {
        "api_key":  os.getenv("MINIMAX_API_KEY"),
        "base_url": os.getenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1"),
        "model":    os.getenv("MINIMAX_MODEL", "abab6.5s-chat"),
    },
}

# ============================================================
# 2. Agent 角色 → Provider 绑定
# ============================================================
AGENTS = {
    # 中枢：GLM，低温稳定，不参与业务消息
    "hub": {
        "provider":    "glm",
        "temperature": 0.1,
        "max_tokens":  2000,
        "role":        "验收官 + 风控",
    },
    # 产品经理：Kimi（长上下文，适合需求拆解）
    "pm": {
        "provider":    "kimi",
        "temperature": 0.4,
        "max_tokens":  4000,
        "role":        "需求分析 + 任务拆解",
    },
    # 前端 Coder：DeepSeek
    "coder_fe": {
        "provider":    "deepseek",
        "temperature": 0.2,
        "max_tokens":  4000,
        "role":        "前端开发",
    },
    # 后端 Coder：MiniMax
    "coder_be": {
        "provider":    "minimax",
        "temperature": 0.2,
        "max_tokens":  4000,
        "role":        "后端开发",
    },
}

# ============================================================
# 3. 中枢验收流水线参数
# ============================================================
HUB_CONFIG = {
    "max_retries":               3,    # 打回最多重试 3 次
    "review_timeout_sec":        60,   # 中枢单次验收超时
    "confidence_threshold":      0.7,  # 验收信心 < 此值 → 升级人工
    "blocked_keywords":          [],   # 硬性红线词，按需扩展
    "auto_check_required_keys":  [],   # 产出必填字段
}

# SQLite 数据文件
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "agenthub.db")


# ============================================================
# 4. 启动校验
# ============================================================
def validate():
    """启动时校验配置，缺 key 直接拒绝启动。"""
    errors = []
    warnings = []

    # 每个 agent 的 provider 必须可用
    for agent_id, cfg in AGENTS.items():
        provider = cfg.get("provider")
        p = PROVIDERS.get(provider)
        if not p:
            errors.append(f"agent '{agent_id}' 绑定了未知 provider '{provider}'")
            continue
        if not p.get("api_key") or p["api_key"].startswith("your_"):
            errors.append(f"agent '{agent_id}' 的 provider '{provider}' 缺 api_key（请检查 .env）")

    # 关键校验：中枢与其他 agent 不能完全相同 provider+model（防自审失效）
    hub_provider = AGENTS["hub"]["provider"]
    hub_model = PROVIDERS[hub_provider]["model"]
    for aid, cfg in AGENTS.items():
        if aid == "hub":
            continue
        if cfg["provider"] == hub_provider and PROVIDERS[cfg["provider"]]["model"] == hub_model:
            warnings.append(
                f"agent '{aid}' 与中枢使用相同 provider+model ({hub_provider}/{hub_model})，"
                f"验收独立性可能受损"
            )

    for w in warnings:
        print(f"[WARN] {w}")
    if errors:
        for e in errors:
            print(f"[ERROR] {e}")
        raise RuntimeError("配置校验失败，请修正 .env 后重试")

    return True


if __name__ == "__main__":
    validate()
    print("✓ 配置校验通过")
    for aid, cfg in AGENTS.items():
        p = PROVIDERS[cfg["provider"]]
        print(f"  {aid:10s} → {cfg['provider']:10s} / {p['model']}  ({cfg['role']})")

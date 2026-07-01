"""
AgentHub 配置中心
- PROVIDERS:  provider 连接信息（key/url/model），与角色无关
- AGENTS:     角色 → provider 绑定 + 调用参数
- QA_REVIEW_CONFIG: QA 验收流水线参数
- FEISHU_BOTS: 飞书机器人配置
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
        "model":    os.getenv("KIMI_MODEL", "kimi-for-coding"),
    },
    "deepseek": {
        "api_key":  os.getenv("DEEPSEEK_API_KEY"),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model":    os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
    },
    "minimax": {
        "api_key":  os.getenv("MINIMAX_API_KEY"),
        "base_url": os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"),
        "model":    os.getenv("MINIMAX_MODEL", "MiniMax-M2.1"),
    },
}

# ============================================================
# 2. Agent 角色 → Provider 绑定
# ============================================================
AGENTS = {
    # 产品经理：Kimi，负责需求拆解与模块编排
    "pm": {
        "provider":    "kimi",
        "temperature": 1.0,
        "max_tokens":  8000,
        "timeout":     120,
        "role":        "需求拆解 + 模块编排 + 人类通知",
    },
    # 前端 Coder：MiniMax 2.7
    "coder_fe": {
        "provider":    "minimax",
        "temperature": 0.2,
        "max_tokens":  12000,
        "timeout":     120,
        "role":        "前端小模块实现",
    },
    # 后端 Coder：GLM-5.1
    "coder_be": {
        "provider":    "glm",
        "temperature": 0.2,
        "max_tokens":  24000,
        "timeout":     300,
        "role":        "后端小模块实现",
    },
    # QA：DeepSeek v4-pro，负责功能/代码验收
    "qa": {
        "provider":    "deepseek",
        "temperature": 0.2,
        "max_tokens":  12000,
        "timeout":     120,
        "role":        "模块功能/代码验收",
    },
}

# ============================================================
# 3. QA 验收流水线参数
# ============================================================
QA_REVIEW_CONFIG = {
    "max_retries":               3,    # 打回最多重试 3 次
    "review_timeout_sec":        300,  # QA 单次验收超时
    "coder_timeout_sec":         300,  # Coder 单次实现超时
    "confidence_threshold":      0.7,  # 验收信心 < 此值 → 升级人工
    "blocked_keywords":          [],   # 硬性红线词，按需扩展
    "auto_check_required_keys":  [],   # 产出必填字段
}

# ============================================================
# 4. Feishu Bot 配置
# ============================================================
FEISHU_BOTS = {
    "pm": {
        "app_id":     os.getenv("FEISHU_PM_APPID"),
        "app_secret": os.getenv("FEISHU_PM_SECRET"),
        "agent_id":   "pm",
        "verification_token": os.getenv("FEISHU_PM_VERIFICATION_TOKEN", ""),
        "encrypt_key": os.getenv("FEISHU_PM_ENCRYPT_KEY", ""),
    },
    "qa": {
        "app_id":     os.getenv("FEISHU_QA_APPID"),
        "app_secret": os.getenv("FEISHU_QA_SECRET"),
        "agent_id":   "qa",
        "verification_token": os.getenv("FEISHU_QA_VERIFICATION_TOKEN", ""),
        "encrypt_key": os.getenv("FEISHU_QA_ENCRYPT_KEY", ""),
    },
    "coder_fe": {
        "app_id":     os.getenv("FEISHU_FE_APPID"),
        "app_secret": os.getenv("FEISHU_FE_SECRET"),
        "agent_id":   "coder_fe",
        "verification_token": os.getenv("FEISHU_FE_VERIFICATION_TOKEN", ""),
        "encrypt_key": os.getenv("FEISHU_FE_ENCRYPT_KEY", ""),
    },
    "coder_be": {
        "app_id":     os.getenv("FEISHU_BE_APPID"),
        "app_secret": os.getenv("FEISHU_BE_SECRET"),
        "agent_id":   "coder_be",
        "verification_token": os.getenv("FEISHU_BE_VERIFICATION_TOKEN", ""),
        "encrypt_key": os.getenv("FEISHU_BE_ENCRYPT_KEY", ""),
    },
}

FEISHU_WEBHOOK_PORT = int(os.getenv("FEISHU_WEBHOOK_PORT", "8000"))

# SQLite 数据文件
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "agenthub.db")


# ============================================================
# 5. 启动校验
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

    # 关键校验：QA 与 Coder 不能完全相同 provider+model（防自审失效）
    qa_provider = AGENTS["qa"]["provider"]
    qa_model = PROVIDERS[qa_provider]["model"]
    for aid, cfg in AGENTS.items():
        if aid == "qa":
            continue
        if cfg["provider"] == qa_provider and PROVIDERS[cfg["provider"]]["model"] == qa_model:
            warnings.append(
                f"agent '{aid}' 与 QA 使用相同 provider+model ({qa_provider}/{qa_model})，"
                f"验收独立性可能受损"
            )

    # 飞书配置可选校验（缺 key 只警告，不阻断本地开发）
    for bot_name, bot_cfg in FEISHU_BOTS.items():
        if not bot_cfg.get("app_id") or not bot_cfg.get("app_secret"):
            warnings.append(f"飞书 bot '{bot_name}' 缺少 app_id 或 app_secret")

    for w in warnings:
        print(f"[WARN] {w}")

    if errors:
        for e in errors:
            print(f"[ERROR] {e}")
        raise RuntimeError("配置校验失败，请修正 .env 后重试")

    return True


if __name__ == "__main__":
    validate()
    print("[OK] 配置校验通过")
    for aid, cfg in AGENTS.items():
        p = PROVIDERS[cfg["provider"]]
        print(f"  {aid:10s} -> {cfg['provider']:10s} / {p['model']}  ({cfg['role']})")

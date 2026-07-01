"""
Feishu API 客户端
- 缓存 tenant_access_token，过期前 5 分钟自动刷新
- 发送文本消息到群聊/用户
"""
import json
import logging
import time
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


class FeishuTokenCache:
    """每个 app_id 一个 token 缓存"""

    def __init__(self):
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, app_id: str) -> Optional[str]:
        entry = self._tokens.get(app_id)
        if not entry:
            return None
        token, expire_at = entry
        # 提前 5 分钟刷新
        if time.time() > expire_at - 300:
            return None
        return token

    def set(self, app_id: str, token: str, expire_in: int):
        self._tokens[app_id] = (token, time.time() + expire_in)


_token_cache = FeishuTokenCache()


def _fetch_token(app_id: str, app_secret: str) -> str:
    """从飞书获取 tenant_access_token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
    resp = requests.post(
        url,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu token error: {data}")
    token = data["tenant_access_token"]
    expire = data.get("expire", 7200)
    _token_cache.set(app_id, token, expire)
    logger.info(f"[feishu] tenant_access_token refreshed for {app_id}")
    return token


def get_token(bot_config: dict) -> str:
    """获取有效 token，过期自动刷新"""
    app_id = bot_config["app_id"]
    cached = _token_cache.get(app_id)
    if cached:
        return cached
    if not app_id or not bot_config.get("app_secret"):
        raise RuntimeError(f"Feishu bot '{bot_config.get('agent_id')}' missing app_id or app_secret")
    return _fetch_token(app_id, bot_config["app_secret"])


def send_text_message(bot_config: dict, receive_id: str, text: str,
                      receive_id_type: str = "chat_id") -> dict:
    """发送文本消息到飞书"""
    token = get_token(bot_config)
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"receive_id_type": receive_id_type}
    body = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    resp = requests.post(url, headers=headers, params=params, json=body, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        logger.error(f"[feishu] send message failed: {data}")
        raise RuntimeError(f"Send message failed: {data}")
    logger.info(f"[feishu] message sent to {receive_id}")
    return data

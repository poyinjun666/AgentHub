"""
通用 LLM Client - 适配 4 个 provider（GLM / Kimi / DeepSeek / MiniMax）
全部兼容 OpenAI 接口风格，按 provider 切 base_url + key + model 即可。
设计：
- 每次调用传 agent_id，自动查 config 拿对应 provider 参数
- 支持 chat（messages）和 chat_json（要求返回 JSON）
- 失败自动重试 2 次，指数退避
"""
import json
import logging
import time
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMClient:
    """按 agent_id 路由到对应 provider 的 LLM 调用"""

    def __init__(self):
        # 缓存：agent_id → 解析后的连接信息
        self._cache: dict[str, dict] = {}

    def _resolve(self, agent_id: str) -> dict:
        if agent_id in self._cache:
            return self._cache[agent_id]
        if agent_id not in config.AGENTS:
            raise LLMError(f"unknown agent_id: {agent_id}")
        agent_cfg = config.AGENTS[agent_id]
        provider = config.PROVIDERS[agent_cfg["provider"]]
        merged = {
            "api_key":     provider["api_key"],
            "base_url":    provider["base_url"].rstrip("/"),
            "model":       provider["model"],
            "temperature": agent_cfg.get("temperature", 0.3),
            "max_tokens":  agent_cfg.get("max_tokens", 2000),
            "timeout":     agent_cfg.get("timeout", 60),
        }
        self._cache[agent_id] = merged
        return merged

    def chat(self, agent_id: str, messages: list[dict],
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None,
             response_format_json: bool = False,
             retries: int = 2,
             timeout: Optional[int] = None) -> str:
        """
        调用 LLM 返回文本。
        - response_format_json=True 时，自动解析 JSON 并返回 dict/str（解析失败返回原文）
        """
        cfg = self._resolve(agent_id)
        url = f"{cfg['base_url']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }
        body = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": temperature if temperature is not None else cfg["temperature"],
            "max_tokens": max_tokens or cfg["max_tokens"],
        }
        if response_format_json:
            # 多数 provider 支持 OpenAI 风格的 response_format
            body["response_format"] = {"type": "json_object"}

        timeout = timeout if timeout is not None else cfg.get("timeout", 60)

        last_err = None
        for attempt in range(retries + 1):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=timeout)
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                if "choices" not in data:
                    raise LLMError(f"unexpected response: {data}")
                content = data["choices"][0]["message"]["content"]
                if response_format_json:
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError:
                        # 模型没按 JSON 返回，尝试提取
                        logger.warning(f"[{agent_id}] response_format_json parse failed, raw: {content[:200]}")
                        return content
                return content
            except (requests.RequestException, LLMError) as e:
                last_err = e
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"[{agent_id}] LLM call failed (attempt {attempt+1}), retry in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    break
        raise LLMError(f"[{agent_id}] LLM call failed after {retries+1} attempts: {last_err}")

    def chat_json(self, agent_id: str, messages: list[dict], **kwargs) -> dict:
        """便捷方法：强制返回 dict"""
        result = self.chat(agent_id, messages, response_format_json=True, **kwargs)
        if isinstance(result, dict):
            return result
        # fallback：尝试从文本里抓 JSON
        import re
        m = re.search(r"\{[\s\S]*\}", result)
        if m:
            return json.loads(m.group(0))
        raise LLMError(f"[{agent_id}] cannot extract JSON from response: {result[:200]}")


# 单例
llm = LLMClient()
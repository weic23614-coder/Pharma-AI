from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


class BailianAIBrain:
    """Bailian LLM wrapper (OpenAI-compatible endpoint)."""

    def __init__(self) -> None:
        self.enabled = _env_bool("ENABLE_AI_BRAIN", False)
        self.api_key = os.getenv("BAILIAN_API_KEY", "")
        self.model = os.getenv("BAILIAN_MODEL", "qwen-plus")
        self.base_url = os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.timeout = max(float(os.getenv("BAILIAN_TIMEOUT_SEC", "8")), 20.0)
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout) if self.api_key else None

    def is_enabled(self) -> bool:
        return self.enabled and bool(self.client)

    def recommend(
        self,
        main_item: dict[str, Any],
        policy: dict[str, Any],
        strategy: dict[str, Any],
        candidates: list[dict[str, Any]],
        variant: str,
    ) -> dict[str, Any] | None:
        if not self.is_enabled():
            return None
        if not candidates:
            return None

        # Keep candidate count small to avoid timeout.
        candidates = candidates[:8]
        sku_list = [str(x.get("sku_id", "")) for x in candidates if x.get("sku_id")]
        system_prompt = (
            "你是医药电商组货助手。"
            "从候选池选1个最合适SKU，必须返回候选池中的selected_sku_id。"
            "输出严格JSON。"
        )
        user_payload = {
            "task": "smart_bundle",
            "variant": variant,
            "main_item": main_item,
            "policy": policy,
            "strategy": strategy,
            "candidate_sku_list": sku_list,
            "candidates": candidates,
            "output_schema": {
                "selected_sku_id": "string",
                "medical_logic": "string",
                "sales_copy": "string",
                "medical_reason": "string",
                "confidence": "0~1 float",
            },
        }

        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        )
        text = resp.choices[0].message.content or "{}"
        data = json.loads(text)
        if "selected_sku_id" not in data:
            return None
        # Enforce strict SKU validity to avoid hallucinated IDs.
        selected = str(data.get("selected_sku_id", "")).strip()
        if selected not in sku_list and sku_list:
            data["selected_sku_id"] = sku_list[0]
            data["medical_reason"] = (str(data.get("medical_reason", "")) + "（SKU已按候选池约束修正）").strip()
        usage_obj = getattr(resp, "usage", None)
        usage = {
            "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
        }
        return {"result": data, "usage": usage, "model": self.model}

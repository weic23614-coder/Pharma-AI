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
        self.timeout = max(float(os.getenv("BAILIAN_TIMEOUT_SEC", "1.2")), 0.5)
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout) if self.api_key else None

    def is_enabled(self) -> bool:
        return self.enabled and bool(self.client)

    def configure(
        self,
        *,
        enabled: bool | None = None,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if api_key is not None:
            self.api_key = api_key.strip()
        if model is not None and model.strip():
            self.model = model.strip()
        if base_url is not None and base_url.strip():
            self.base_url = base_url.strip()
        if timeout is not None:
            self.timeout = max(float(timeout), 0.5)
        self.client = (
            OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
            if self.api_key
            else None
        )

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
            "你是医药电商「用药组合」页的文案与组货顾问，面向普通消费者，语气自然、好读、像 App 商品卡片上的说明。"
            "从候选池选 1 个最合适 SKU，selected_sku_id 必须是 candidate_sku_list 里存在的 id。"
            "sales_copy 要求（非常重要）："
            "1）模仿「主品功效 + 搭配品功效」结构，用一两句中文说完即可；"
            "2）先写主品（可用简称）能缓解/辅助什么，再写搭配品能缓解/辅助什么；"
            "不要在末尾加「组合针对××」「省心搭配更适合家庭常备」等策略收口套话；"
            "3）不要用「【药师建议】」「本单加购」「管理连续性」等后台腔；不要列表、不要 Markdown；"
            "4）40～90 字为宜，口语化、移动端扫一眼能懂；避免绝对化疗效承诺，用「缓解」「辅助」「关注」等表述。"
            "5）严禁荒谬搭配：儿童牙膏/口腔护理/家清类主品，不可搭配避孕套、润滑液、情趣用品等；"
            "品名含「益生菌」的牙膏按口腔护理理解，不要写成治腹泻。"
            "6）package_name：面向用户的套餐短标题，2～10 个字，参考「流行性感冒」「感冒咳嗽」「感冒伴鼻炎」，不要标点。"
            "输出严格 JSON，字段齐全。"
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
                "package_name": "string（套餐短标题，如流行性感冒）",
                "medical_logic": "string",
                "sales_copy": "string（用药组合页风格，主品+搭配品）",
                "medical_reason": "string",
                "confidence": "0~1 float",
            },
            "style_example": (
                "感冒灵缓解感冒头痛与鼻塞，和胃整肠丸缓解腹痛腹泻；组合针对感冒伴腹泻。"
                "感冒灵缓解感冒相关症状，口罩助力日常防护；组合侧重缓解感冒与防护。"
            ),
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

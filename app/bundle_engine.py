from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime


@dataclass
class EngineInput:
    main_sku_id: str
    main_product_name: str
    main_category: str
    main_price: float
    main_cost: float
    user_id: str | None
    variant: str


@dataclass
class EnginePolicy:
    logic_type: str
    prompt_hint: str
    margin_rate: float


@dataclass
class EngineStrategy:
    anchor_ratio: float
    min_margin_rate: float
    forbidden_terms: list[str]


@dataclass
class EngineCandidate:
    sku_id: str
    product_name: str
    cost: float
    original_price: float
    category: str | None = None


class BundleEngine:
    """Smart bundle engine: guard -> score -> price -> copy."""

    safety_map = {
        "抗生素": {"益生菌", "肠胃", "营养保健"},
        "高血压药": {"血压计", "辅酶Q10", "医疗器械", "营养保健"},
        "降糖药": {"血糖仪", "神经", "营养保健", "医疗器械"},
        "降脂药": {"辅酶Q10", "营养保健"},
    }

    def _is_medically_safe(self, main_category: str, candidate: EngineCandidate) -> bool:
        expected = self.safety_map.get(main_category)
        if not expected:
            return True
        text = f"{candidate.product_name}{candidate.category or ''}"
        return any(token in text for token in expected)

    def _price(self, candidate: EngineCandidate, policy: EnginePolicy, strategy: EngineStrategy, variant: str) -> tuple[float, float]:
        margin_rate = max(policy.margin_rate, strategy.min_margin_rate)
        anchor_ratio = strategy.anchor_ratio if variant == "A" else max(0.3, strategy.anchor_ratio - 0.03)
        floor_price = candidate.cost * (1 + margin_rate)
        anchor_price = candidate.original_price * anchor_ratio
        addon_price = round(max(floor_price, anchor_price), 2)
        projected_profit = round(addon_price - candidate.cost, 2)
        return addon_price, projected_profit

    def _score(
        self,
        main_price: float,
        candidate: EngineCandidate,
        addon_price: float,
        projected_profit: float,
        safe: bool,
    ) -> tuple[float, dict]:
        if main_price <= 0:
            affordability = 0.3
        else:
            affordability = max(0.0, 1 - (addon_price / (main_price * 4)))
        margin_score = min(1.0, projected_profit / max(1, candidate.original_price))
        medical_score = 1.0 if safe else 0.0
        total = 0.5 * medical_score + 0.3 * margin_score + 0.2 * affordability
        trace = {
            "source": "rule_engine",
            "medical_score": round(medical_score, 3),
            "margin_score": round(margin_score, 3),
            "affordability_score": round(affordability, 3),
            "total_score": round(total, 3),
        }
        return total, trace

    def _copy(self, hint: str, product_name: str, variant: str, forbidden_terms: list[str]) -> str:
        copy_a = f"【药师建议】{hint} 建议本单加购{product_name}，帮助形成更完整的用药管理。"
        copy_b = f"【用药提醒】结合当前主药方案，搭配{product_name}可提升管理连续性，建议同步配置。"
        text = copy_a if variant == "A" else copy_b
        for bad in forbidden_terms:
            text = text.replace(bad, "")
        return text

    def recommend(
        self,
        engine_input: EngineInput,
        policy: EnginePolicy,
        strategy: EngineStrategy,
        candidates: list[EngineCandidate],
    ) -> dict:
        if not candidates:
            raise ValueError("候选池为空")

        best_item = None
        for c in candidates:
            safe = self._is_medically_safe(engine_input.main_category, c)
            if not safe:
                continue
            addon_price, projected_profit = self._price(c, policy, strategy, engine_input.variant)
            score, trace = self._score(engine_input.main_price, c, addon_price, projected_profit, safe)
            if not best_item or score > best_item["score"]:
                best_item = {
                    "candidate": c,
                    "addon_price": addon_price,
                    "projected_profit": projected_profit,
                    "score": score,
                    "trace": trace,
                }

        if not best_item:
            raise ValueError("无医学安全候选")

        selected = best_item["candidate"]
        request_id = f"req_{int(datetime.now().timestamp() * 1000)}_{random.randint(100, 999)}"
        sales_copy = self._copy(policy.prompt_hint, selected.product_name, engine_input.variant, strategy.forbidden_terms)
        return {
            "recommendation": {
                "request_id": request_id,
                "variant": engine_input.variant,
                "selected_sku_id": selected.sku_id,
                "product_name": selected.product_name,
                "medical_logic": policy.logic_type,
                "sales_copy": sales_copy,
                "pricing_strategy": {
                    "addon_price": best_item["addon_price"],
                    "original_price": selected.original_price,
                    "display_tag": f"加{best_item['addon_price']:.0f}元换购价",
                },
                "projected_profit": best_item["projected_profit"],
                "decision_trace": best_item["trace"],
            }
        }

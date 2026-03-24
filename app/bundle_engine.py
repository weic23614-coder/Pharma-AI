from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime

# 历史版本卖点末尾套话，读取/展示时剥掉（无需整批重生成）
_LEGACY_BUNDLE_TAIL_RES = (
    re.compile(r"[;；]\s*组合针对[^。]+?[，,]\s*省心搭配更适合家庭常备。\s*$"),
    re.compile(r"[，,]\s*组合针对[^。]+?[，,]\s*省心搭配更适合家庭常备。\s*$"),
    # 无前导分号、直接接在正文后
    re.compile(r"\s*组合针对[^。]+?[，,]\s*省心搭配更适合家庭常备。\s*$"),
)


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

    @staticmethod
    def strip_legacy_bundle_sales_tail(text: str | None) -> str:
        """去掉旧模板「；组合针对××，省心搭配更适合家庭常备。」等后缀，兼容库里历史数据。"""
        if text is None:
            return ""
        s = str(text).strip()
        if not s:
            return s
        for _ in range(3):
            prev = s
            for pat in _LEGACY_BUNDLE_TAIL_RES:
                s = pat.sub("", s).strip()
            if s == prev:
                break
        return s

    safety_map = {
        "抗生素": {"益生菌", "肠胃", "营养保健"},
        "高血压药": {"血压计", "辅酶Q10", "医疗器械", "营养保健"},
        "降糖药": {"血糖仪", "神经", "营养保健", "医疗器械"},
        "降脂药": {"辅酶Q10", "营养保健"},
    }
    # 规则化的“主品科室/类目 -> 可搭配关键词”，用于关联度评分。
    relation_map = {
        "心脑血管": {"血压", "心率", "辅酶", "鱼油", "低钠", "监测", "血压计", "血脂"},
        "内分泌": {"血糖", "糖尿", "神经", "足部", "血糖仪", "控糖", "代谢"},
        "消化系统疾病": {"益生菌", "胃", "肠", "消化", "护胃", "腹泻"},
        "呼吸道疾病": {"雾化", "咽喉", "鼻", "口罩", "润喉"},
        "皮肤病": {"修复", "皮肤", "保湿", "抗菌", "护理"},
        "风湿骨科": {"钙", "维生素d", "关节", "护膝", "热敷"},
        "男科疾病": {"前列腺", "男科", "锌", "肾", "护理"},
        "妇科疾病": {"益生菌", "私护", "妇科", "调理"},
        "滋补调养": {"维生素", "矿物质", "蛋白", "营养"},
    }
    # 对明显无关的“凑数品”做惩罚。
    unrelated_penalty_tokens = {"棉签", "牙痛", "润肤", "香皂", "洗发", "美白"}

    # 副品含以下特征 → 视为成人/计生/润滑等，不得与儿童、口腔、家清等主品搭配
    _INTIMATE_OR_ADULT_ADDON_MARKERS: tuple[str, ...] = (
        "润滑液",
        "润滑剂",
        "润滑油",
        "避孕套",
        "安全套",
        "情趣",
        "延时",
        "冈本",
        "杜蕾斯",
        "杰士邦",
        "第六感",
        "飞机杯",
        "跳蛋",
        "震动",
        "验孕",
        "排卵试",
        "早孕",
    )
    # 主品为日化/口腔/家清/儿童向时，禁止与上类副品组货
    _FAMILY_ORAL_DAILY_MAIN_MARKERS: tuple[str, ...] = (
        "牙膏",
        "牙刷",
        "漱口水",
        "牙线",
        "口腔喷雾",
        "含漱",
        "洗发水",
        "洗发露",
        "沐浴露",
        "香皂",
        "洗衣粉",
        "洗衣液",
        "洗手液",
        "洗洁精",
        "抽纸",
        "卷纸",
        "湿巾",
        "纸尿裤",
        "拉拉裤",
    )

    def _addon_tags_intimate_or_adult(self, addon_name: str, addon_category: str | None) -> bool:
        blob = f"{addon_name or ''}{addon_category or ''}"
        if any(t in blob for t in self._INTIMATE_OR_ADULT_ADDON_MARKERS):
            return True
        if addon_category and any(x in addon_category for x in ("计生", "成人", "情趣", "润滑")):
            return True
        return False

    def _main_tags_family_oral_child_daily(self, main_name: str, main_category: str | None) -> bool:
        blob = f"{main_name or ''}{main_category or ''}"
        if any(t in blob for t in self._FAMILY_ORAL_DAILY_MAIN_MARKERS):
            return True
        if "儿童" in blob:
            return True
        return False

    def is_addon_inappropriate_for_main(
        self,
        main_name: str,
        main_category: str | None,
        addon_name: str,
        addon_category: str | None,
    ) -> bool:
        """
        硬规则：儿童牙膏、家清、口腔护理等主品，不得搭配润滑/避孕套/情趣品牌等副品。
        避免「益生菌牙膏」被当成肠道益生菌、再与润滑液等荒谬组货。
        """
        if not self._addon_tags_intimate_or_adult(addon_name, addon_category):
            return False
        return self._main_tags_family_oral_child_daily(main_name, main_category)

    def _is_medically_safe(self, main_category: str, candidate: EngineCandidate) -> bool:
        expected = self.safety_map.get(main_category)
        if not expected:
            return True
        text = f"{candidate.product_name}{candidate.category or ''}"
        return any(token in text for token in expected)

    def _association_score(self, main_category: str, main_name: str, candidate: EngineCandidate) -> tuple[float, list[str]]:
        text = f"{candidate.product_name}{candidate.category or ''}".lower()
        main_text = f"{main_category}{main_name}".lower()
        hits: list[str] = []
        score = 0.0

        # 1) 类目级匹配
        rel_tokens = self.relation_map.get(main_category, set())
        for t in rel_tokens:
            if t.lower() in text:
                score += 0.12
                hits.append(f"类目关联:{t}")

        # 2) 主副品关键词协同
        pair_rules = [
            ({"高血压", "降压"}, {"血压", "血压计", "心率", "辅酶"}),
            ({"糖尿", "降糖"}, {"血糖", "血糖仪", "足部", "神经"}),
            ({"抗生素", "感染"}, {"益生菌", "肠", "胃"}),
            ({"降脂", "胆固醇"}, {"鱼油", "辅酶", "心血管"}),
            ({"胃", "肠"}, {"益生菌", "护胃", "消化"}),
        ]
        for left, right in pair_rules:
            if any(x in main_text for x in left):
                for y in right:
                    if y.lower() in text:
                        score += 0.16
                        hits.append(f"病种协同:{y}")

        # 3) 惩罚明显无关词，避免“凑数品”
        for bad in self.unrelated_penalty_tokens:
            if bad in text:
                score -= 0.2
                hits.append(f"弱关联惩罚:{bad}")

        # 4) 禁忌搭配：口腔/儿童/家清主品 + 成人润滑计生副品 → 直接打穿分数
        if self.is_addon_inappropriate_for_main(main_name, main_category, candidate.product_name, candidate.category):
            score = 0.0
            hits.append("禁忌搭配:已拦截")

        return max(0.0, min(1.0, score)), hits

    def _price(self, candidate: EngineCandidate, policy: EnginePolicy, strategy: EngineStrategy, variant: str) -> tuple[float, float]:
        """组货阶段不按毛利/锚定算价，换购价一律用商品原价（库内 original_price）。"""
        addon_price = round(float(candidate.original_price or 0), 2)
        projected_profit = round(addon_price - float(candidate.cost or 0), 2)
        return addon_price, projected_profit

    def _score(
        self,
        main_category: str,
        main_name: str,
        main_price: float,
        candidate: EngineCandidate,
        addon_price: float,
        projected_profit: float,
        safe: bool,
    ) -> tuple[float, dict]:
        assoc_score, assoc_tags = self._association_score(main_category, main_name, candidate)
        if main_price <= 0:
            affordability = 0.3
        else:
            # 仅用原价相对主品单价的轻量参考，不参与“定价优化”。
            affordability = max(0.0, 1 - (addon_price / (main_price * 4)))
        medical_score = 1.0 if safe else 0.0
        # 只管组货关联，不按毛利选品。
        total = 0.6 * assoc_score + 0.3 * medical_score + 0.1 * affordability
        trace = {
            "source": "rule_engine",
            "price_mode": "original_price",
            "association_score": round(assoc_score, 3),
            "association_tags": assoc_tags[:6],
            "medical_score": round(medical_score, 3),
            "affordability_score": round(affordability, 3),
            "total_score": round(total, 3),
        }
        return total, trace

    def _short_display_name(self, name: str, max_len: int = 18) -> str:
        """C 端列表用短名：去规格括号、控制长度。"""
        s = (name or "").strip()
        if not s:
            return "本品"
        for sep in ("（", "(", "[", "【", "｜"):
            if sep in s:
                s = s.split(sep, 1)[0].strip()
        s = " ".join(s.split())
        if len(s) > max_len:
            s = s[: max_len - 1] + "…"
        return s or "本品"

    def _symptom_snippet_for_product(self, product_name: str, category: str | None) -> str:
        """
        从品名/类目推断一句「功效向」说明，对齐用药组合页：
        「主品缓解…，搭配品…；组合针对某场景」的中间素材。
        """
        blob = (product_name or "") + (category or "")
        # 口腔护理必须优先于「益生菌」：牙膏常标注益生菌，不能套肠道话术
        if any(k in blob for k in ("牙膏", "牙刷", "漱口水", "牙线", "口腔喷雾", "含漱", "洁牙")):
            return "助力日常口腔清洁与牙齿护理"
        rules: list[tuple[tuple[str, ...], str]] = [
            (("口罩", "医用防护", "外科口罩", "N95", "防护"), "助力日常防护、减少飞沫接触"),
            (("感冒", "氨酚", "黄那敏", "鼻塞", "流涕", "退热", "发热", "头痛"), "缓解感冒头痛、鼻塞等不适"),
            (("咳", "痰", "咽喉", "咽炎", "扁桃体", "润肺"), "缓解咳嗽、咳痰及咽喉不适"),
            # 肠道益生菌补剂（非牙膏）
            (("益生菌",), "辅助调节肠道菌群与消化舒适"),
            (("腹泻", "腹痛", "肠道", "和胃", "整肠", "肠炎", "蒙脱"), "缓解腹痛、腹泻并呵护肠道舒适"),
            (("胃", "消化", "健胃", "消食", "反酸"), "辅助温和调理消化不适"),
            (("维生素", "维C", "钙铁锌", "泡腾", "多维"), "补充营养与矿物质"),
            (("血糖", "糖尿", "血糖仪", "试纸", "胰岛素"), "辅助血糖监测与控糖管理"),
            (("血压", "降压", "地平", "沙坦", "心率", "心脑", "血栓"), "关注血压与心脑血管养护"),
            (("皮肤", "软膏", "乳膏", "外用", "皮炎"), "照护局部皮肤不适"),
            (("钙", "维D", "骨骼", "关节", "软骨"), "关注骨骼与关节舒适"),
            (("雾化", "哮喘", "支气管", "气雾"), "辅助呼吸道症状管理"),
            (("眼", "滴眼", "干涩"), "缓解眼部干涩与疲劳"),
            (("妇科", "私护", "菌群"), "辅助私密护理与菌群平衡"),
            (("器械", "血压计", "体温计", "听诊"), "方便居家监测体征"),
            (("保健", "滋补", "营养", "蛋白粉"), "营养补充与日常调养"),
            (("鼻", "通鼻", "过敏"), "舒缓鼻部不适与敏感"),
            (("护肝", "肝", "脂肪肝"), "辅助肝脏代谢与日常养护"),
        ]
        for keys, phrase in rules:
            if any(k in blob for k in keys):
                return phrase
        return "贴合当前症状与护理需要"

    def build_package_name(
        self,
        main_name: str,
        main_category: str | None,
        addon_name: str,
        addon_category: str | None,
        logic_type: str,
    ) -> str:
        """
        C 端套餐标题（短）：参考用药组合页，如「流行性感冒」「感冒伴鼻炎」。
        """
        m = (main_name or "") + (main_category or "")
        a = (addon_name or "") + (addon_category or "")
        blob = m + a

        if any(k in blob for k in ("流感", "奥斯他韦", "连花清瘟", "连花")) and any(
            k in blob for k in ("感冒", "氨酚", "清热", "颗粒")
        ):
            return "流行性感冒"
        if any(k in m for k in ("感冒", "氨酚", "黄那敏", "清热")) and any(
            k in a for k in ("咳", "痰", "枇杷", "止咳", "润肺", "氨溴", "川贝")
        ):
            return "感冒咳嗽"
        if any(k in m for k in ("感冒", "流涕", "鼻塞")) and any(k in a for k in ("鼻", "鼻炎", "通鼻", "喷雾")):
            return "感冒伴鼻炎"
        if any(k in m for k in ("感冒",)) and any(k in a for k in ("腹泻", "肠道", "益生菌", "蒙脱", "肠炎")):
            return "感冒伴腹泻"
        if any(k in m for k in ("感冒",)) and any(k in a for k in ("口罩", "防护", "外科口罩")):
            return "缓解感冒防护"
        if any(k in blob for k in ("血糖", "糖尿", "胰岛素")):
            return "血糖管理"
        if any(k in blob for k in ("血压", "降压", "沙坦", "地平")):
            return "血压管理"
        if any(k in m for k in ("钙", "碳酸钙", "牡蛎", "维D", "维生素D")) and any(
            k in a for k in ("维C", "维生素C", "VC")
        ):
            return "补钙维C"
        if any(k in blob for k in ("钙", "维D", "骨骼", "关节")):
            return "骨骼关节"
        if any(k in blob for k in ("维生素", "维C", "多维", "泡腾")):
            return "营养补充"
        if any(k in blob for k in ("牙膏", "牙刷", "漱口水", "口腔")):
            return "口腔护理"
        if any(k in blob for k in ("胃", "消化", "益生菌")) and "牙膏" not in m:
            return "肠胃舒适"
        if any(k in blob for k in ("咳", "咽喉", "雾化")):
            return "呼吸道护理"
        lt = (logic_type or "").strip()
        if lt and lt not in ("慢病管理", "未分类") and len(lt) <= 12:
            return lt[:12]
        return "联合用药"

    def build_consumer_sales_copy(
        self,
        main_name: str,
        main_category: str | None,
        addon_name: str,
        addon_category: str | None,
        logic_type: str,
        variant: str,
        forbidden_terms: list[str],
    ) -> str:
        """
        面向消费者的「用药组合」式一句话：仅保留主品功效 + 搭配品功效（不再追加策略场景收口句）。
        """
        m = self._short_display_name(main_name)
        a = self._short_display_name(addon_name)
        sm = self._symptom_snippet_for_product(main_name, main_category)
        sa = self._symptom_snippet_for_product(addon_name, addon_category)

        if (variant or "A").upper() == "B":
            text = f"{m}{sm}；再配{a}{sa}。"
        else:
            text = f"{m}{sm}，{a}{sa}。"

        for bad in forbidden_terms or []:
            if bad:
                text = text.replace(str(bad), "")
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

        min_assoc = 0.12

        def pick_best(require_min_assoc: bool) -> dict | None:
            best: dict | None = None
            for c in candidates:
                if self.is_addon_inappropriate_for_main(
                    engine_input.main_product_name,
                    engine_input.main_category,
                    c.product_name,
                    c.category,
                ):
                    continue
                safe = self._is_medically_safe(engine_input.main_category, c)
                if not safe:
                    continue
                addon_price, projected_profit = self._price(c, policy, strategy, engine_input.variant)
                score, trace = self._score(
                    engine_input.main_category,
                    engine_input.main_product_name,
                    engine_input.main_price,
                    c,
                    addon_price,
                    projected_profit,
                    safe,
                )
                if require_min_assoc and trace.get("association_score", 0) < min_assoc:
                    continue
                if not best or score > best["score"]:
                    best = {
                        "candidate": c,
                        "addon_price": addon_price,
                        "projected_profit": projected_profit,
                        "score": score,
                        "trace": trace,
                    }
            return best

        # 先按关联门槛选，避免弱凑单；若无命中（常见于「未分类」或类目不在 relation_map），再放宽门槛只保留医学安全。
        best_item = pick_best(require_min_assoc=True)
        if not best_item:
            best_item = pick_best(require_min_assoc=False)
            if best_item:
                tr = best_item["trace"]
                tr = dict(tr)
                tr["fallback_relaxed_assoc"] = True
                tr["note"] = "主品类目关联信号弱，已放宽关联门槛，仍以医学安全为先。"
                best_item["trace"] = tr

        if not best_item:
            raise ValueError("无高关联候选（已过滤弱关联/不安全商品）")

        selected = best_item["candidate"]
        request_id = f"req_{int(datetime.now().timestamp() * 1000)}_{random.randint(100, 999)}"
        sales_copy = self.build_consumer_sales_copy(
            engine_input.main_product_name,
            engine_input.main_category,
            selected.product_name,
            selected.category,
            policy.logic_type,
            engine_input.variant,
            strategy.forbidden_terms,
        )
        package_name = self.build_package_name(
            engine_input.main_product_name,
            engine_input.main_category,
            selected.product_name,
            selected.category,
            policy.logic_type,
        )
        return {
            "recommendation": {
                "request_id": request_id,
                "variant": engine_input.variant,
                "selected_sku_id": selected.sku_id,
                "product_name": selected.product_name,
                "medical_logic": policy.logic_type,
                "package_name": package_name,
                "sales_copy": sales_copy,
                "pricing_strategy": {
                    "addon_price": best_item["addon_price"],
                    "original_price": selected.original_price,
                    "display_tag": f"按商品原价 ¥{best_item['addon_price']:.2f}",
                },
                "projected_profit": best_item["projected_profit"],
                "decision_trace": best_item["trace"],
            }
        }

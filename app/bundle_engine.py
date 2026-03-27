from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


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

    def __init__(self) -> None:
        self._copy_cfg = self._load_copy_rule_config()

    safety_map = {
        "抗生素": {"益生菌", "肠胃", "营养保健"},
        # 注意：不要把“医疗器械”当作泛 token，否则医用纱布等会被大量放行。
        # 这里保留更具指向性的 token，减少“不合理搭售”。
        "高血压药": {"血压计", "辅酶Q10", "营养保健"},
        "降糖药": {"血糖仪", "神经", "营养保健"},
        "降脂药": {"辅酶Q10", "营养保健"},
    }

    # Excel / 后台类目名与策略 safety_map 对齐（本期优先组货合理性，下期再细抠价格成本）
    _category_canon = (
        (("心脑血管", "高血压", "降压", "冠心病", "中风", "脑卒中", "眩晕"), "高血压药"),
        (("降糖", "糖尿病", "内分泌", "胰岛素", "格列", "二甲双胍"), "降糖药"),
        (("降脂", "他汀", "血脂"), "降脂药"),
        (("抗生素", "抗菌", "抗感染", "头孢", "青霉素", "阿莫西林", "沙星"), "抗生素"),
    )

    # 无明确外科/创面主诉时，避免纱布棉签等被低价分“刷”成万能搭售
    _wound_consumables = ("纱布", "棉签", "绷带", "脱脂棉", "创可贴", "碘伏", "医用胶带")
    _wound_main_signals = ("外伤", "创口", "创面", "术后", "烧伤", "烫伤", "褥疮", "换药", "清创", "手术", "缝合", "消毒")

    def _canonical_safety_category(self, main_category: str) -> str | None:
        c = (main_category or "").strip()
        if c in self.safety_map:
            return c
        for keys, canon in self._category_canon:
            if any(k in c for k in keys):
                return canon
        return None

    def _wound_consumable_factor(self, main_product_name: str, main_category: str, candidate: EngineCandidate) -> tuple[float, str]:
        main_ctx = f"{main_product_name}{main_category or ''}"
        cand_txt = f"{candidate.product_name}{candidate.category or ''}"
        if not any(x in cand_txt for x in self._wound_consumables):
            return 1.0, "n/a"
        if any(x in main_ctx for x in self._wound_main_signals):
            return 1.0, "wound_context_ok"
        return 0.22, "wound_consumable_without_context"

    def _is_medically_safe(self, main_category: str, candidate: EngineCandidate) -> bool:
        canon = self._canonical_safety_category(main_category)
        if canon:
            expected = self.safety_map[canon]
        else:
            # 未知主类目：用已知 token 并集兜底，避免默认放行
            expected = set()
            for tokens in self.safety_map.values():
                expected |= set(tokens)
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
        consumable_factor: float,
        consumable_note: str,
    ) -> tuple[float, dict]:
        if main_price <= 0:
            affordability = 0.3
        else:
            affordability = max(0.0, 1 - (addon_price / (main_price * 4)))
        margin_score = min(1.0, projected_profit / max(1, candidate.original_price))
        medical_score = (1.0 if safe else 0.0) * consumable_factor
        # 本期：组货合理性优先；毛利/可负担性仅作极轻量 tie-break（定价与成本下钻放下一期）
        total = 0.88 * medical_score + 0.07 * margin_score + 0.05 * affordability
        trace = {
            "source": "rule_engine",
            "pairing_mode": "pairing_first_v1",
            "medical_score": round(medical_score, 3),
            "margin_score": round(margin_score, 3),
            "affordability_score": round(affordability, 3),
            "consumable_factor": round(consumable_factor, 3),
            "consumable_note": consumable_note,
            "total_score": round(total, 3),
        }
        return total, trace

    def _short_product_title(self, full_name: str, max_len: int = 18) -> str:
        """展示用短名：尽量掐在规格数字/括号之前。"""
        s = (full_name or "").strip()
        if not s:
            return ""
        cut = len(s)
        for i, ch in enumerate(s):
            if ch.isdigit() or ch in "（(":
                cut = i
                break
        s = s[:cut].strip() or (full_name or "").strip()
        return (s[:max_len] + "…") if len(s) > max_len else s

    # C 端话术：按「主品轴 × 副品轴」选自然收尾，避免策略里泛化的「慢病管理」套在所有搭配上
    _generic_policy_tags = frozenset({"慢病管理", "综合健康管理", "慢病", "健康管理"})
    _consumer_banned_terms = (
        "对症需求",
        "说明用于",
        "用于",
        "按说明",
        "遵说明书",
        "详见说明书",
    )

    def _load_copy_rule_config(self) -> dict[str, Any]:
        cfg_path = Path(__file__).resolve().parent / "config" / "copy_rule_config.json"
        if not cfg_path.exists():
            return {}
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _axis_from_config(self, text: str) -> str | None:
        rules = self._copy_cfg.get("main_axis_rules") or []
        for r in rules:
            axis = str(r.get("axis", "")).strip()
            kws = r.get("keywords") or []
            if axis and any(str(k) in text for k in kws):
                return axis
        return None

    def _style_from_config(self, main_axis: str, addon_axis: str) -> tuple[str, str] | None:
        rules = self._copy_cfg.get("style_rules") or []
        for r in rules:
            mains = set(r.get("main_axes") or [])
            addons = set(r.get("addon_axes") or [])
            if main_axis in mains and addon_axis in addons:
                key = str(r.get("style_key") or "").strip()
                name = str(r.get("style_name") or "").strip()
                if key and name:
                    return key, name
        return None

    def _scene_from_config(self, main_axis: str, addon_axis: str, seed: str) -> str | None:
        scenes = self._copy_cfg.get("scene_titles") or {}
        pair_key = f"{main_axis}|{addon_axis}"
        if pair_key in scenes and isinstance(scenes[pair_key], list) and scenes[pair_key]:
            return self._pick_stable_choice(seed + "|scene_cfg", [str(x) for x in scenes[pair_key]])
        axis_key = f"{main_axis}|*"
        if axis_key in scenes and isinstance(scenes[axis_key], list) and scenes[axis_key]:
            return self._pick_stable_choice(seed + "|scene_cfg_axis", [str(x) for x in scenes[axis_key]])
        return None

    def _main_axis(self, product_name: str, category: str | None) -> str:
        t = f"{product_name}{category or ''}"
        cfg_axis = self._axis_from_config(t)
        if cfg_axis:
            return cfg_axis
        # 大盘科室覆盖（基于真实出库大表）：先按科室语义归到主轴，再补商品关键词。
        if any(x in t for x in ("呼吸道疾病", "呼吸", "感冒", "流感", "鼻炎", "咽炎", "支气管", "咳嗽", "化痰")):
            return "respiratory"
        if any(x in t for x in ("消化系统疾病", "消化", "胃", "反酸", "腹泻", "便秘", "肠", "奥美", "拉唑")):
            return "digestive"
        if any(x in t for x in ("神经系统", "神经", "眩晕", "失眠", "焦虑", "偏头痛", "头痛")):
            return "neuro"
        if any(x in t for x in ("风湿骨科", "骨科", "关节", "腰腿", "颈肩", "风湿", "扭伤")):
            return "ortho"
        if any(x in t for x in ("妇科疾病", "妇科", "月经", "宫颈", "盆腔", "白带", "乳腺")):
            return "womens"
        if any(x in t for x in ("肝病科", "肝病", "乙肝", "保肝", "肝炎")):
            return "liver"
        if any(x in t for x in ("肿瘤科", "肿瘤", "放化疗", "升白", "止吐")):
            return "oncology"
        if any(x in t for x in ("五官科疾病", "五官科", "耳鼻喉", "咽喉", "中耳", "鼻窦")):
            return "ent"
        if any(x in t for x in ("维生素、钙剂", "维生素", "钙剂")):
            return "vitamin"
        if any(x in t for x in ("滋补调养", "滋补", "调养", "人参", "黄芪", "灵芝")):
            return "tonic"
        if any(x in t for x in ("皮肤病", "皮肤", "湿疹", "真菌", "皮炎", "痤疮")):
            return "skin"
        if any(x in t for x in ("男科疾病", "男科", "西地那非", "达泊西汀", "他达拉", "勃起")):
            return "mens"
        if any(x in t for x in ("内分泌", "糖尿病", "降糖", "胰岛素", "甲状腺")):
            return "metabolic"
        if any(x in t for x in ("心脑血管", "高血压", "降压", "冠心病", "他汀", "血脂")):
            return "cardio"
        if any(x in t for x in ("医疗器械", "美妆个护", "成人用品", "隐形眼镜")):
            return "general"
        if any(x in t for x in ("滴眼", "眼用", "眼膏", "眼液", "眼科", "眼内")) or (
            "眼" in t and any(x in t for x in ("凝胶", "软膏", "悬液"))
        ):
            return "eye"
        if any(x in t for x in ("偏头痛", "瑞美吉泮", "瑞美", "曲普坦", "利扎曲普坦")):
            return "migraine"
        if any(x in t for x in ("西地那非", "达泊西汀", "他达拉", "伐地那非", "金戈")):
            return "mens"
        if any(x in t for x in ("更昔洛韦", "阿昔洛韦", "利巴韦林")) and "眼" not in t:
            return "antiviral"
        if "感冒" in t:
            return "cold"
        if any(x in t for x in ("腹泻", "泄泻", "肠炎", "肠胃", "整肠")):
            return "gut"
        if any(x in t for x in ("咳", "痰", "桉柠", "愈创")):
            return "cough"
        if any(x in t for x in ("痔疮", "肛肠", "栓")):
            return "procto"
        if any(x in t for x in ("皮肤", "乳膏", "软膏", "搽剂", "外用", "瘢痕", "氢醌", "湿疹", "癣")):
            return "skin"
        if any(x in t for x in ("血压", "降压", "沙坦", "地平", "洛尔")):
            return "cardio"
        # 避免「凝胶糖果」等含「糖」误伤：只匹配典型降糖药关键词
        if any(x in t for x in ("糖尿病", "降糖", "胰岛素", "双胍", "列净", "格列", "二甲双胍")):
            return "metabolic"
        if any(x in t for x in ("胃", "反酸", "泮托", "奥美", "拉唑")):
            return "gi_acid"
        if any(x in t for x in ("过敏", "氯雷他定", "西替利嗪")):
            return "allergy"
        if any(x in t for x in ("鼻", "喷剂", "阻隔")) and "皮肤" not in t:
            return "nasal"
        if "避孕" in t or "杜蕾斯" in t or "润滑" in t:
            return "personal"
        return "general"

    def _addon_axis(self, product_name: str, category: str | None) -> str:
        t = f"{product_name}{category or ''}"
        c = category or ""
        if "口罩" in t or ("外科" in t and "口罩" in t):
            return "mask"
        if "血压计" in t:
            return "bp"
        if any(x in t for x in ("血糖仪", "试纸")):
            return "glucose"
        if any(x in t for x in ("益生菌", "乳酸菌")):
            return "probiotic"
        if any(x in t for x in ("鱼油", "叶黄素", "维生素", "辅酶", "Q10")) or "保健" in c:
            return "nutrition"
        if any(x in t for x in ("软胶囊", "凝胶糖果", "咀嚼片", "片糖果")) and (
            "鱼" in t or "藻" in t or "黄" in t or "维" in t or "保健" in c
        ):
            return "nutrition"
        if any(x in t for x in ("软胶囊", "凝胶糖果")) and "保健" in c:
            return "nutrition"
        if any(x in t for x in ("纱布", "绷带", "棉签", "碘伏")):
            return "wound_care"
        if any(x in t for x in ("润喉", "咽喉", "含片")):
            return "throat"
        return "general_addon"

    def _consumer_main_blurb(self, axis: str, product_name: str, category: str | None) -> str:
        """口语短句：避免“说明书体”。"""
        m = {
            "eye": "先稳住眼部不适，做外用修护",
            "migraine": "先把头痛发作压下来",
            "mens": "聚焦男士即时状态管理",
            "antiviral": "先处理当前感染相关不适",
            "cold": "管感冒常见不适",
            "gut": "管肚子不舒服、腹泻那一类",
            "cough": "帮着缓和咳嗽、有痰那种粘腻感",
            "procto": "针对肛肠不适做局部护理",
            "skin": "侧重皮肤局部护理对症",
            "cardio": "聚焦血压相关长期管理",
            "metabolic": "聚焦血糖相关日常管理",
            "gi_acid": "管胃酸、胃不舒服这类",
            "allergy": "缓解过敏、发痒、鼻涕喷嚏等",
            "nasal": "缓解鼻子堵、刺激等不适",
            "personal": "个人护理类需求",
            "general": "先把当下不适处理到位",
        }.get(axis, "先把当下不适处理到位")
        tname = product_name or ""
        if axis == "skin" and any(x in tname for x in ("乳膏", "软膏", "凝胶")):
            return "做局部涂抹护理"
        return m

    def _consumer_addon_blurb(
        self, axis: str, seed: str = "", product_name: str = "", category: str | None = None
    ) -> str:
        if axis == "nutrition":
            t = f"{product_name}{category or ''}"
            if any(x in t for x in ("胶原", "软骨", "氨糖", "钙片", "硫酸软骨素")):
                return self._pick_stable_choice(seed + "|bone", ["补充关节/骨骼相关营养", "骨骼软骨营养做长期打底"])
            if any(x in t for x in ("番茄", "红素", "叶黄素", "葡萄籽")):
                return self._pick_stable_choice(seed + "|ox", ["补充抗氧化营养支持", "做日常抗氧化与眼部营养补给"])
            if any(x in t for x in ("大豆异黄酮", "蔓越莓", "月见草", "叶酸")):
                return self._pick_stable_choice(seed + "|fem", ["补充女性日常营养", "做女性方向轻量营养支持"])
            if any(x in t for x in ("鱼油", "辅酶", "DHA", "维生素", "维C", "益生菌")):
                return self._pick_stable_choice(seed + "|daily", ["做常见日常营养打底", "补一层日常营养续航"])
            return self._pick_stable_choice(seed + "|n", ["加一件日常营养补充", "做轻量营养续航"])
        return {
            "mask": "出门防护更踏实一点",
            "bp": "在家就能看血压走势",
            "glucose": "测血糖更方便对照饮食和用药",
            "probiotic": "给肠道舒适度多一小层支持",
            "wound_care": "换药护理更方便",
            "throat": "嗓子干痒时含一片更舒服",
            "general_addon": "和本单一起带走少跑一趟",
        }.get(axis, "和本单一起带走少跑一趟")

    def _pick_stable_choice(self, seed: str, options: list[str]) -> str:
        if not options:
            return ""
        h = sum(ord(c) for c in seed) if seed else random.randint(0, 10_000)
        return options[h % len(options)]

    def _style_bucket(self, main_axis: str, addon_axis: str) -> tuple[str, str]:
        cfg = self._style_from_config(main_axis, addon_axis)
        if cfg:
            return cfg
        if main_axis in {"migraine", "antiviral", "cold", "gut", "cough", "respiratory", "digestive", "liver"} and addon_axis == "nutrition":
            return "full_cycle", "标本兼顾型"
        if main_axis in {"eye", "skin", "ent"} and addon_axis in {"nutrition", "probiotic"}:
            return "inside_out", "内外兼修型"
        if main_axis in {"mens", "cardio", "metabolic", "neuro", "ortho", "womens", "oncology"} and addon_axis in {"nutrition", "bp", "glucose"}:
            return "professional_guard", "专业守护型"
        return "value_bundle", "超值凑单型"

    def _scene_title(self, main_axis: str, addon_axis: str, seed: str) -> str:
        cfg_scene = self._scene_from_config(main_axis, addon_axis, seed)
        if cfg_scene:
            return cfg_scene
        mapping: dict[tuple[str, str], list[str]] = {
            ("migraine", "nutrition"): ["头痛急救包", "清醒守护组"],
            ("eye", "nutrition"): ["明眸内外组合", "护眼双通路"],
            ("mens", "nutrition"): ["男士活力套装", "状态续航组"],
            ("skin", "nutrition"): ["肌肤修护拍档", "内外养护组"],
            ("respiratory", "nutrition"): ["呼吸舒缓全周期", "换季呼吸守护组"],
            ("digestive", "nutrition"): ["肠胃轻养组合", "胃肠调理拍档"],
            ("neuro", "nutrition"): ["神经舒缓组合", "脑神经续航组"],
            ("ortho", "nutrition"): ["关节行动力组合", "骨关节养护组"],
            ("womens", "nutrition"): ["女性周期守护组", "女性内调组合"],
            ("liver", "nutrition"): ["肝脏轻养组合", "肝养调理组"],
            ("ent", "nutrition"): ["咽鼻舒护组合", "耳鼻喉舒缓组"],
            ("vitamin", "nutrition"): ["每日维养组合", "轻补给组合"],
            ("tonic", "nutrition"): ["滋补调养套组", "元气养护组"],
            ("oncology", "nutrition"): ["术后营养支持组", "恢复期续航组"],
            ("general", "nutrition"): ["日常轻补组合", "一单省心搭"],
        }
        options = mapping.get((main_axis, addon_axis))
        if options:
            return self._pick_stable_choice(seed + "|scene", options)
        if addon_axis in {"bp", "glucose"}:
            return self._pick_stable_choice(seed + "|scene", ["监测管理搭档", "居家管理组合"])
        if addon_axis == "nutrition":
            return self._pick_stable_choice(seed + "|scene", ["全周期调理组", "日常续航组合"])
        return self._pick_stable_choice(seed + "|scene", ["家庭护理凑单礼", "省心搭配组"])

    def _sanitize_consumer_text(self, text: str) -> str:
        out = text
        for bad in self._consumer_banned_terms:
            out = out.replace(bad, "")
        while "  " in out:
            out = out.replace("  ", " ")
        out = out.replace("；。", "。").replace("，，", "，").replace("。。", "。").strip()
        return out

    def _consumer_joint(
        self,
        main_axis: str,
        addon_axis: str,
        policy: EnginePolicy,
        variant: str,
        seed: str,
    ) -> str:
        """按搭配选「人话」收尾；不把泛化的 policy.logic_type 硬塞进每一单。"""
        lt = (policy.logic_type or "").strip()
        use_logic_label = bool(lt) and lt not in self._generic_policy_tags

        pair_lines: dict[tuple[str, str], list[str]] = {
            ("skin", "nutrition"): [
                "外涂护理 + 小营养，一件结算更省事。",
                "皮肤对症管局部，营养小件顺带走，少拆一单。",
                "别分开凑了：护理和营养一次带走。",
            ],
            ("general", "nutrition"): [
                "对症用药为主，营养小件顺单带走，少拆一单。",
                "药该买就买，营养当轻补顺手带上。",
                "主品管症状，营养别抢戏，一起结算更省心。",
                "分开凑营养太折腾，一单带走更省事。",
                "营养不是替代药，顺单带上日常更方便。",
                "该对症的对症，营养小搭配别单独下单。",
                "用药按说明，营养小件一次买齐少跑腿。",
                "别为点小营养再开一单，结算顺手带上。",
                "主药解决当下问题，营养作日常小补充。",
                "两件一起付，比来回找SKU更省力。",
                "营养顺单不是必选项，但带上确实省事。",
                "对症优先，营养顺路，一单搞定。",
                "小营养别当治疗主力，顺带走就行。",
                "该买的药不落，营养小件顺便凑齐。",
                "分开买容易忘，一单带走更稳。",
                "营养轻量补一点，和主品一起带走。",
                "别把小营养拆成第二单，顺手更划算。",
                "主品对症，营养辅助，一次结算完事。",
                "日常营养小件，和本单一起带走少折腾。",
            ],
            ("eye", "nutrition"): [
                "眼部对症护理为主，营养顺单只是日常轻补。",
                "滴眼/眼用先按说明，营养小件别当替代治疗。",
                "眼睛不舒服先对症，营养搭配顺手带走更省事。",
            ],
            ("migraine", "nutrition"): [
                "偏头痛用药遵医嘱，营养顺单只是辅助日常。",
                "对症止痛为主，营养小件别指望“治根”。",
                "发作期先按医嘱用药，营养搭配一次买齐。",
            ],
            ("mens", "nutrition"): [
                "对症用药按说明，营养顺单别当功效替代。",
                "主需求先解决，营养小件顺手带上即可。",
                "用药为主，营养轻补顺单更方便。",
            ],
            ("antiviral", "nutrition"): [
                "抗病毒用药遵医嘱，营养顺单只是日常补充。",
                "对症抗病毒优先，营养小件别抢戏。",
                "按疗程用药，营养搭配一次带走少折腾。",
            ],
            ("cold", "mask"): [
                "吃药缓解症状，口罩护一路，更安心。",
            ],
            ("cold", "nutrition"): [
                "感冒休养顺带补点营养，一次买齐少折腾。",
            ],
            ("cardio", "bp"): [
                "长期用药配上血压计，在家心里有数。",
            ],
            ("cardio", "nutrition"): [
                "遵医嘱用药，营养作日常轻量补充即可。",
            ],
            ("metabolic", "glucose"): [
                "用药配合血糖监测，日常管理更直观。",
            ],
            ("metabolic", "nutrition"): [
                "控糖为主，营养小件别当“替代药”，顺单更方便。",
            ],
            ("gut", "probiotic"): [
                "肠胃不舒服时，对症用药和益生菌可以形成常见搭配思路。",
            ],
            ("general", "mask"): [
                "对症用药之外，顺手带点防护，少跑一单。",
            ],
        }

        key = (main_axis, addon_axis)
        if key in pair_lines:
            opts = pair_lines[key]
            return self._pick_stable_choice(seed or variant, opts)

        # 轴级兜底（不对称时也尽量别念「慢病管理」）
        if addon_axis == "nutrition":
            if main_axis in ("cardio", "metabolic") and use_logic_label:
                return self._pick_stable_choice(
                    seed,
                    [f"围绕「{lt}」的日常管理，营养顺单只是辅助。", "慢病管理遵医嘱，营养别当药，一起下单省时间。"],
                )
            return self._pick_stable_choice(
                seed,
                [
                    "两件事一次结算，比分开找省心。",
                    "一单带走，少跑一趟。",
                    "结算时顺手带上，省事。",
                ],
            )

        if addon_axis in ("bp", "glucose") and main_axis in ("cardio", "metabolic", "general"):
            return self._pick_stable_choice(
                seed,
                ["监测设备顺单带，日常对照更方便。", "用药+监测一起买，少忘一件。"],
            )

        if use_logic_label:
            return self._pick_stable_choice(
                seed,
                [f"和「{lt}」相关的需求一次性配齐更省心。", f"围绕「{lt}」，搭配带走少折腾。"],
            )

        return self._pick_stable_choice(
            seed,
            [
                "两件一起下单，结算更省事。" if variant == "A" else "一次买齐，少跑一单。",
                "搭配带走更方便。",
            ],
        )

    def combo_sales_copy(
        self,
        main_product_name: str,
        main_category: str | None,
        addon: EngineCandidate,
        policy: EnginePolicy,
        variant: str,
        forbidden_terms: list[str],
        main_sku_id: str = "",
    ) -> str:
        """
        C 端可读卖点：短句 + 按搭配推断的「人话」收尾，避免全文案模板与错误「慢病管理」标签。
        """
        ma = self._short_product_title(main_product_name)
        ad = self._short_product_title(addon.product_name)
        m_axis = self._main_axis(main_product_name, main_category)
        a_axis = self._addon_axis(addon.product_name, addon.category)
        b1 = self._consumer_main_blurb(m_axis, main_product_name, main_category)
        seed = f"{main_sku_id}|{addon.sku_id}|{variant}"
        b2 = self._consumer_addon_blurb(a_axis, seed + "|addon", addon.product_name, addon.category)
        joint = self._consumer_joint(m_axis, a_axis, policy, variant, seed + "|joint")
        style_key, style_name = self._style_bucket(m_axis, a_axis)
        scene = self._scene_title(m_axis, a_axis, seed)
        style_templates = {
            "full_cycle": f"【{scene}｜{style_name}】{ma}{b1}；{ad}{b2}。先稳当下，再做后续调理，{joint}",
            "inside_out": f"【{scene}｜{style_name}】外面先处理表层不适：{ma}{b1}；里面补源头支持：{ad}{b2}。{joint}",
            "professional_guard": f"【{scene}｜{style_name}】{ma}{b1}解决当前场景，{ad}{b2}补长期续航。{joint}",
            "value_bundle": f"【{scene}｜{style_name}】{ma}{b1}；{ad}{b2}。一单带走更省心，{joint}",
        }
        text = style_templates.get(style_key, style_templates["value_bundle"])
        text = self._sanitize_consumer_text(text)
        for bad in forbidden_terms:
            text = text.replace(str(bad), "")
        return text

    def recommend(
        self,
        engine_input: EngineInput,
        policy: EnginePolicy,
        strategy: EngineStrategy,
        candidates: list[EngineCandidate],
        selection_counts: dict[str, int] | None = None,
        diversity_alpha: float = 0.003,
    ) -> dict:
        if not candidates:
            raise ValueError("候选池为空")

        best_item = None
        main_axis = self._main_axis(engine_input.main_product_name, engine_input.main_category)
        for c in candidates:
            safe = self._is_medically_safe(engine_input.main_category, c)
            if not safe:
                continue
            cf, cnote = self._wound_consumable_factor(engine_input.main_product_name, engine_input.main_category, c)
            addon_price, projected_profit = self._price(c, policy, strategy, engine_input.variant)
            score, trace = self._score(
                engine_input.main_price, c, addon_price, projected_profit, safe, cf, cnote
            )
            # Batch-level diversity:
            # if a candidate is already picked many times in this batch,
            # penalize its effective score to avoid "everything becomes the same SKU".
            count = selection_counts.get(c.sku_id, 0) if selection_counts else 0
            effective_score = score - diversity_alpha * count
            trace["effective_score"] = round(effective_score, 4)
            addon_axis = self._addon_axis(c.product_name, c.category)
            style_key, style_name = self._style_bucket(main_axis, addon_axis)
            trace["copy_style"] = style_key
            trace["copy_style_name"] = style_name
            trace["main_axis"] = main_axis
            trace["addon_axis"] = addon_axis
            trace["scene_title"] = self._scene_title(main_axis, addon_axis, f"{engine_input.main_sku_id}|{c.sku_id}|{engine_input.variant}")
            if not best_item or effective_score > best_item["effective_score"]:
                best_item = {
                    "candidate": c,
                    "addon_price": addon_price,
                    "projected_profit": projected_profit,
                    "score": score,
                    "effective_score": effective_score,
                    "trace": trace,
                }

        if not best_item:
            raise ValueError("无医学安全候选")

        selected = best_item["candidate"]
        request_id = f"req_{int(datetime.now().timestamp() * 1000)}_{random.randint(100, 999)}"
        sales_copy = self.combo_sales_copy(
            engine_input.main_product_name,
            engine_input.main_category,
            selected,
            policy,
            engine_input.variant,
            strategy.forbidden_terms,
            main_sku_id=engine_input.main_sku_id,
        )
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

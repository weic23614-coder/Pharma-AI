#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd


BASE_AXIS_BY_DEPT = {
    "心脑血管": "cardio",
    "内分泌": "metabolic",
    "呼吸道疾病": "respiratory",
    "消化系统疾病": "digestive",
    "神经系统": "neuro",
    "风湿骨科": "ortho",
    "妇科疾病": "womens",
    "男科疾病": "mens",
    "肝病科": "liver",
    "五官科疾病": "ent",
    "皮肤病": "skin",
    "维生素、钙剂": "vitamin",
    "滋补调养": "tonic",
    "肿瘤科": "oncology",
}

NOISE_TOKENS = {
    "薄膜衣片",
    "浓缩丸",
    "糖衣片",
    "大蜜丸",
    "OTC",
    "BYS",
    "找药服务预约",
}
SPEC_RE = re.compile(r"^\d+(mg|ml|g|片|粒|袋|支|丸|cm|板|瓶)$", re.I)
TOKEN_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9\+]+")


def clean_token(tok: str) -> str:
    t = tok.strip()
    if len(t) < 2:
        return ""
    if SPEC_RE.match(t):
        return ""
    if t.isdigit():
        return ""
    if t in NOISE_TOKENS:
        return ""
    return t


def build_rules(df: pd.DataFrame, top_k: int) -> dict:
    by_axis = defaultdict(Counter)
    for _, row in df.iterrows():
        dept = str(row.get("科室") or "").strip()
        name = str(row.get("产品名称") or "").strip()
        axis = BASE_AXIS_BY_DEPT.get(dept)
        if not axis or not name:
            continue
        toks = {clean_token(t) for t in TOKEN_RE.findall(name)}
        toks.discard("")
        for t in toks:
            by_axis[axis][t] += 1

    main_axis_rules = []
    for dept, axis in BASE_AXIS_BY_DEPT.items():
        top_tokens = [t for t, _ in by_axis[axis].most_common(top_k)]
        merged = [dept] + top_tokens
        # preserve order and uniqueness
        seen = set()
        deduped = []
        for k in merged:
            if k not in seen:
                deduped.append(k)
                seen.add(k)
        main_axis_rules.append({"axis": axis, "keywords": deduped})

    return {"main_axis_rules": main_axis_rules}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build copy rule config from outbound XLSX.")
    parser.add_argument("--xlsx", required=True, help="Path to 药网订单数据*.xlsx")
    parser.add_argument("--output", required=True, help="Path to output json")
    parser.add_argument("--top-k", type=int, default=60, help="Top keywords per axis")
    args = parser.parse_args()

    xlsx = Path(args.xlsx)
    out = Path(args.output)
    df = pd.read_excel(xlsx)
    rules = build_rules(df, args.top_k)

    if out.exists():
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    else:
        old = {}
    old["main_axis_rules"] = rules["main_axis_rules"]
    out.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"updated: {out}")
    print(f"axis rules: {len(rules['main_axis_rules'])}")


if __name__ == "__main__":
    main()

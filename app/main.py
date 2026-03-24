from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import random
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.ai_brain import BailianAIBrain
from app.bundle_engine import BundleEngine, EngineCandidate, EngineInput, EnginePolicy, EngineStrategy

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("APP_DB_PATH", str(BASE_DIR / "app.db")))

app = FastAPI(title="1药网 AI 组货中间件 MVP", version="0.1.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
ai_brain = BailianAIBrain()


class MainItem(BaseModel):
    sku_id: str
    product_name: str
    category: str
    price: float
    cost: float


class CandidateItem(BaseModel):
    sku_id: str
    product_name: str
    cost: float
    original_price: float
    category: str | None = None


class RecommendRequest(BaseModel):
    user_intent: str = Field(default="checkout")
    main_item: MainItem
    candidate_pool: list[CandidateItem]
    user_id: str | None = None


def _resolve_item_code(cur: sqlite3.Cursor, sku_id: str) -> str:
    """商品编码：优先库存表 item_code，其次 product_code，否则 sku_id。"""
    if not sku_id:
        return ""
    row = cur.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(item_code), ''), NULLIF(TRIM(product_code), ''), sku_id)
        FROM products WHERE sku_id=?
        """,
        (sku_id,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    return str(sku_id)


def _enrich_bundle_row(cur: sqlite3.Cursor, d: dict[str, Any]) -> None:
    if not d.get("main_item_code"):
        d["main_item_code"] = _resolve_item_code(cur, str(d.get("main_sku_id") or ""))
    if not d.get("selected_item_code"):
        d["selected_item_code"] = _resolve_item_code(cur, str(d.get("selected_sku_id") or ""))
    if d.get("sales_copy"):
        d["sales_copy"] = BundleEngine.strip_legacy_bundle_sales_tail(str(d["sales_copy"]))
    pn = (d.get("package_name") or "").strip()
    if not pn:
        try:
            pay = json.loads(d.get("decision_payload") or "{}")
            pn = (pay.get("package_name") or "").strip()
        except Exception:
            pn = ""
        if not pn:
            pn = BundleEngine().build_package_name(
                str(d.get("main_product_name") or ""),
                d.get("main_category"),
                str(d.get("selected_product_name") or ""),
                None,
                str(d.get("medical_logic") or ""),
            )
        d["package_name"] = pn


def db_conn() -> sqlite3.Connection:
    # SQLite 在并发写场景下容易短暂锁表，这里增加等待时间并启用 busy_timeout。
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _execute_with_retry(
    cur: sqlite3.Cursor,
    sql: str,
    params: tuple[Any, ...] | list[Any] = (),
    retries: int = 8,
    sleep_sec: float = 0.25,
) -> None:
    for i in range(retries):
        try:
            cur.execute(sql, tuple(params))
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or i == retries - 1:
                raise
            time.sleep(sleep_sec * (i + 1))


def _executemany_with_retry(
    cur: sqlite3.Cursor,
    sql: str,
    rows: list[tuple[Any, ...]],
    retries: int = 6,
    sleep_sec: float = 0.35,
) -> None:
    for i in range(retries):
        try:
            cur.executemany(sql, rows)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or i == retries - 1:
                raise
            time.sleep(sleep_sec * (i + 1))


def init_db() -> None:
    conn = db_conn()
    cur = conn.cursor()
    # WAL 模式可以显著减少读写互斥带来的锁冲突。
    # 某些情况下（例如上次异常退出）这里会短暂锁库，做重试并降级，避免启动直接失败。
    pragma_ok = False
    for i in range(8):
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            pragma_ok = True
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            time.sleep(0.2 * (i + 1))
    if not pragma_ok:
        try:
            cur.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            # 保底：即使无法设置 pragma，也继续初始化表结构。
            pass
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            logic_type TEXT NOT NULL,
            prompt_hint TEXT NOT NULL,
            margin_rate REAL NOT NULL DEFAULT 0.35,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cached_recommendations (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            main_sku_id TEXT NOT NULL,
            main_category TEXT NOT NULL,
            selected_sku_id TEXT,
            addon_price REAL,
            projected_profit REAL,
            source TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            result_status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            sku_id TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            product_code TEXT,
            manufacturer TEXT,
            department TEXT,
            item_code TEXT,
            level1_category TEXT,
            category TEXT NOT NULL,
            role TEXT NOT NULL,
            cost REAL NOT NULL,
            original_price REAL NOT NULL,
            gross_margin_rate REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL,
            version TEXT NOT NULL,
            content_json TEXT NOT NULL,
            status TEXT NOT NULL,
            published_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exp_name TEXT NOT NULL,
            category TEXT NOT NULL,
            traffic_a REAL NOT NULL DEFAULT 0.5,
            traffic_b REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            main_sku_id TEXT NOT NULL,
            selected_sku_id TEXT,
            variant TEXT,
            revenue REAL,
            margin REAL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_batches (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS uploaded_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            sku_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            category TEXT,
            price REAL NOT NULL,
            cost REAL NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            gmv REAL NOT NULL DEFAULT 0,
            role_hint TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            main_sku_id TEXT NOT NULL,
            main_product_name TEXT NOT NULL,
            main_category TEXT NOT NULL,
            selected_sku_id TEXT NOT NULL,
            selected_product_name TEXT NOT NULL,
            medical_logic TEXT NOT NULL,
            addon_price REAL NOT NULL,
            projected_profit REAL NOT NULL,
            sales_copy TEXT NOT NULL,
            package_name TEXT NOT NULL DEFAULT '',
            main_item_code TEXT NOT NULL DEFAULT '',
            selected_item_code TEXT NOT NULL DEFAULT '',
            decision_payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_rules (
            main_sku_id TEXT PRIMARY KEY,
            main_product_name TEXT NOT NULL,
            selected_sku_id TEXT NOT NULL,
            selected_product_name TEXT NOT NULL,
            addon_price REAL NOT NULL,
            medical_logic TEXT NOT NULL,
            sales_copy TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            api_key TEXT,
            base_url TEXT,
            timeout_sec REAL NOT NULL DEFAULT 1.2,
            enabled INTEGER NOT NULL DEFAULT 1,
            monthly_budget_usd REAL NOT NULL DEFAULT 50,
            input_cost_per_1k REAL NOT NULL DEFAULT 0.001,
            output_cost_per_1k REAL NOT NULL DEFAULT 0.002,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    cols = [r["name"] for r in cur.execute("PRAGMA table_info(uploaded_products)").fetchall()]
    if "gmv" not in cols:
        cur.execute("ALTER TABLE uploaded_products ADD COLUMN gmv REAL NOT NULL DEFAULT 0")
        conn.commit()
    product_cols = [r["name"] for r in cur.execute("PRAGMA table_info(products)").fetchall()]
    for name, ddl in [
        ("product_code", "ALTER TABLE products ADD COLUMN product_code TEXT"),
        ("manufacturer", "ALTER TABLE products ADD COLUMN manufacturer TEXT"),
        ("department", "ALTER TABLE products ADD COLUMN department TEXT"),
        ("item_code", "ALTER TABLE products ADD COLUMN item_code TEXT"),
        ("level1_category", "ALTER TABLE products ADD COLUMN level1_category TEXT"),
    ]:
        if name not in product_cols:
            cur.execute(ddl)
            conn.commit()
    br_cols = [r["name"] for r in cur.execute("PRAGMA table_info(bundle_recommendations)").fetchall()]
    for name, ddl in [
        ("package_name", "ALTER TABLE bundle_recommendations ADD COLUMN package_name TEXT NOT NULL DEFAULT ''"),
        ("main_item_code", "ALTER TABLE bundle_recommendations ADD COLUMN main_item_code TEXT NOT NULL DEFAULT ''"),
        ("selected_item_code", "ALTER TABLE bundle_recommendations ADD COLUMN selected_item_code TEXT NOT NULL DEFAULT ''"),
    ]:
        if name not in br_cols:
            cur.execute(ddl)
            conn.commit()
    llm_cols = [r["name"] for r in cur.execute("PRAGMA table_info(llm_settings)").fetchall()]
    for name, ddl in [
        ("api_key", "ALTER TABLE llm_settings ADD COLUMN api_key TEXT"),
        ("base_url", "ALTER TABLE llm_settings ADD COLUMN base_url TEXT"),
        ("timeout_sec", "ALTER TABLE llm_settings ADD COLUMN timeout_sec REAL NOT NULL DEFAULT 1.2"),
    ]:
        if name not in llm_cols:
            cur.execute(ddl)
            conn.commit()
    cur.execute("SELECT COUNT(*) AS cnt FROM policies")
    count = cur.fetchone()["cnt"]
    if count == 0:
        now = datetime.now(timezone.utc).isoformat()
        seeds = [
            ("抗生素", "副作用对冲", "抗生素易破坏肠道菌群，优先推荐益生菌。", 0.35, 1, now),
            ("降脂药", "疗效协同", "他汀类可能消耗辅酶Q10，推荐辅酶Q10补充。", 0.38, 1, now),
            ("降糖药", "病因延展", "关注神经和血管并发风险，优先营养神经类。", 0.4, 1, now),
            ("高血压药", "慢病管理", "搭配血压计监测，强化管理闭环。", 0.42, 1, now),
        ]
        cur.executemany(
            """
            INSERT INTO policies (category, logic_type, prompt_hint, margin_rate, active, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            seeds,
        )
        conn.commit()
    cur.execute("SELECT COUNT(*) AS cnt FROM products")
    pcount = cur.fetchone()["cnt"]
    if pcount == 0:
        now = datetime.now(timezone.utc).isoformat()
        products = [
            ("A200", "盐酸二甲双胍片", "降糖药", "main", 23, 26, 0.115, 1, now),
            ("A300", "缬沙坦胶囊", "高血压药", "main", 27, 32, 0.156, 1, now),
            ("A123", "阿莫西林胶囊", "抗生素", "main", 22, 25, 0.12, 1, now),
            ("B801", "α-硫辛酸胶囊", "营养保健", "addon", 29, 99, 0.707, 1, now),
            ("B802", "家用血糖仪", "医疗器械", "addon", 65, 188, 0.654, 1, now),
            ("B901", "上臂式电子血压计", "医疗器械", "addon", 88, 259, 0.66, 1, now),
            ("B902", "辅酶Q10软胶囊", "营养保健", "addon", 36, 139, 0.741, 1, now),
            ("B001", "益生菌冻干粉", "营养保健", "addon", 30, 128, 0.766, 1, now),
        ]
        cur.executemany(
            """
            INSERT INTO products (
                sku_id, product_name, category, role, cost, original_price, gross_margin_rate, active, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            products,
        )
        conn.commit()
    cur.execute("SELECT COUNT(*) AS cnt FROM strategy_versions")
    scount = cur.fetchone()["cnt"]
    if scount == 0:
        now = datetime.now(timezone.utc).isoformat()
        strategy = {
            "title": "默认医嘱式推荐策略",
            "pricing_rules": {"anchor_ratio": 0.42, "min_margin_rate": 0.35},
            "copy_style": ["医学逻辑优先", "医嘱关怀语气", "不使用低价促销词"],
            "forbidden_terms": ["跳楼价", "秒杀", "白菜价", "清仓"],
        }
        cur.execute(
            """
            INSERT INTO strategy_versions (strategy_name, version, content_json, status, published_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("smart-bundle-core", "v1.0.0", json.dumps(strategy, ensure_ascii=False), "published", now, now),
        )
        conn.commit()
    cur.execute("SELECT COUNT(*) AS cnt FROM experiments")
    ecount = cur.fetchone()["cnt"]
    if ecount == 0:
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            """
            INSERT INTO experiments (exp_name, category, traffic_a, traffic_b, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("checkout-copy-pricing", "all", 0.5, 0.5, "running", now),
        )
        conn.commit()
    cur.execute("SELECT COUNT(*) AS cnt FROM llm_settings")
    lcount = cur.fetchone()["cnt"]
    if lcount == 0:
        cur.execute(
            """
            INSERT INTO llm_settings (
              provider, model, enabled, monthly_budget_usd, input_cost_per_1k, output_cost_per_1k, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("bailian", "qwen-plus", 1, 50, 0.0012, 0.0024, now_iso()),
        )
        conn.commit()
    conn.close()


@app.on_event("startup")
def startup_event() -> None:
    init_db()
    _refresh_ai_brain_from_setting()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def assign_variant(user_id: str | None) -> str:
    if not user_id:
        return "A" if random.random() < 0.5 else "B"
    return "A" if hash(user_id) % 2 == 0 else "B"


def get_latest_strategy() -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT * FROM strategy_versions
        WHERE status='published'
        ORDER BY published_at DESC, updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    if not row:
        return {"pricing_rules": {"anchor_ratio": 0.42, "min_margin_rate": 0.35}, "forbidden_terms": []}
    return json.loads(row["content_json"])


def select_candidates_by_pool_or_db(payload: RecommendRequest) -> list[CandidateItem]:
    if payload.candidate_pool:
        return payload.candidate_pool
    conn = db_conn()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT sku_id, product_name, cost, original_price, category FROM products WHERE role='addon' AND active=1"
    ).fetchall()
    conn.close()
    return [CandidateItem(**dict(r)) for r in rows]


def infer_role(category: str | None, product_name: str) -> str:
    text = f"{category or ''}{product_name}"
    addon_tokens = ("保健", "器械", "血压计", "血糖仪", "辅酶", "益生菌", "维生素")
    return "addon" if any(t in text for t in addon_tokens) else "main"


def normalize_header(text: str) -> str:
    return str(text).strip().lower().replace(" ", "")


def find_col_idx(headers: list[str], candidates: set[str]) -> int | None:
    cand = {normalize_header(x) for x in candidates}
    for idx, h in enumerate(headers):
        if normalize_header(h) in cand:
            return idx
    return None


def to_float(v: Any, default: float = 0) -> float:
    if v in (None, ""):
        return default
    try:
        return float(v)
    except Exception:
        return default


def _get_llm_setting() -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM llm_settings ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else {}


def _refresh_ai_brain_from_setting() -> dict[str, Any]:
    setting = _get_llm_setting()
    ai_brain.configure(
        enabled=bool(setting.get("enabled", 1)),
        api_key=str(setting.get("api_key") or os.getenv("BAILIAN_API_KEY", "")),
        model=str(setting.get("model") or ai_brain.model),
        base_url=str(setting.get("base_url") or os.getenv("BAILIAN_BASE_URL", ai_brain.base_url)),
        timeout=float(setting.get("timeout_sec") or os.getenv("BAILIAN_TIMEOUT_SEC", ai_brain.timeout)),
    )
    return setting


def _log_ai_usage(
    scene: str,
    model: str,
    usage: dict[str, int],
    source: str,
) -> None:
    setting = _get_llm_setting()
    in_cost = float(setting.get("input_cost_per_1k", 0.0012) or 0.0012)
    out_cost = float(setting.get("output_cost_per_1k", 0.0024) or 0.0024)
    p = int(usage.get("prompt_tokens", 0) or 0)
    c = int(usage.get("completion_tokens", 0) or 0)
    t = int(usage.get("total_tokens", p + c) or (p + c))
    est = round((p / 1000) * in_cost + (c / 1000) * out_cost, 6)
    try:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_usage_logs (
              scene, provider, model, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (scene, "bailian", model, p, c, t, est, source, now_iso()),
        )
        conn.commit()
        conn.close()
    except Exception:
        # Usage logging should never block recommendation generation.
        return


def _log_ai_attempt(source: str, model: str) -> None:
    try:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_usage_logs (
              scene, provider, model, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd, source, created_at
            ) VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?)
            """,
            ("bundle", "bailian", model, source, now_iso()),
        )
        conn.commit()
        conn.close()
    except Exception:
        return


def _is_ai_allowed() -> bool:
    _refresh_ai_brain_from_setting()
    return ai_brain.is_enabled()


def _build_recommendation_result(
    main_item: MainItem,
    user_id: str | None,
    candidates: list[CandidateItem],
    policy_like: dict[str, Any],
    strategy: dict[str, Any],
    variant: str,
    prefer_ai: bool = True,
    force_ai_only: bool = False,
) -> dict[str, Any]:
    engine = BundleEngine()
    engine_input = EngineInput(
        main_sku_id=main_item.sku_id,
        main_product_name=main_item.product_name,
        main_category=main_item.category,
        main_price=main_item.price,
        main_cost=main_item.cost,
        user_id=user_id,
        variant=variant,
    )
    engine_policy = EnginePolicy(
        logic_type=policy_like["logic_type"],
        prompt_hint=policy_like["prompt_hint"],
        margin_rate=policy_like["margin_rate"],
    )
    engine_strategy = EngineStrategy(
        anchor_ratio=strategy.get("pricing_rules", {}).get("anchor_ratio", 0.42),
        min_margin_rate=strategy.get("pricing_rules", {}).get("min_margin_rate", 0.35),
        forbidden_terms=strategy.get("forbidden_terms", []),
    )
    engine_candidates = [
        EngineCandidate(
            sku_id=c.sku_id,
            product_name=c.product_name,
            cost=c.cost,
            original_price=c.original_price,
            category=c.category,
        )
        for c in candidates
        if not engine.is_addon_inappropriate_for_main(
            main_item.product_name,
            main_item.category,
            c.product_name,
            c.category,
        )
    ]
    if prefer_ai and _is_ai_allowed():
        _log_ai_attempt("ai_attempt", ai_brain.model)
        ai_result = None
        try:
            ai_result = ai_brain.recommend(
            {
                "sku_id": main_item.sku_id,
                "product_name": main_item.product_name,
                "category": main_item.category,
                "price": main_item.price,
                "cost": main_item.cost,
            },
            {
                "logic_type": policy_like["logic_type"],
                "prompt_hint": policy_like["prompt_hint"],
                "margin_rate": policy_like["margin_rate"],
            },
            strategy,
            [
                {
                    "sku_id": c.sku_id,
                    "product_name": c.product_name,
                    "category": c.category,
                    "cost": c.cost,
                    "original_price": c.original_price,
                }
                for c in engine_candidates
            ],
            variant,
            )
        except Exception as e:
            _log_ai_attempt("ai_error", ai_brain.model)
            if force_ai_only:
                raise RuntimeError(f"AI调用异常: {str(e)}")
        if ai_result:
            llm_data = ai_result.get("result", {})
            usage = ai_result.get("usage", {})
            model_used = str(ai_result.get("model", ai_brain.model))
            _log_ai_usage("bundle", model_used, usage, "bailian_llm")
            candidate_map = {c.sku_id: c for c in engine_candidates}
            selected = candidate_map.get(str(llm_data.get("selected_sku_id", "")).strip())
            if selected:
                # 组货只管选品；价格一律用副品原价，后续业务侧再谈促销/换购价。
                addon_price = round(float(selected.original_price or 0), 2)
                projected_profit = round(addon_price - float(selected.cost or 0), 2)
                sales_copy = str(llm_data.get("sales_copy") or "").strip()
                if not sales_copy:
                    sales_copy = BundleEngine().build_consumer_sales_copy(
                        main_item.product_name,
                        main_item.category,
                        selected.product_name,
                        selected.category,
                        policy_like["logic_type"],
                        variant,
                        list(strategy.get("forbidden_terms") or []),
                    )
                forbidden_terms = strategy.get("forbidden_terms", [])
                for bad in forbidden_terms:
                    sales_copy = sales_copy.replace(str(bad), "")
                sales_copy = BundleEngine.strip_legacy_bundle_sales_tail(sales_copy)
                pkg = str(llm_data.get("package_name") or "").strip()
                if not pkg:
                    pkg = BundleEngine().build_package_name(
                        main_item.product_name,
                        main_item.category,
                        selected.product_name,
                        selected.category,
                        policy_like["logic_type"],
                    )
                return {
                    "recommendation": {
                        "request_id": f"req_{int(datetime.now().timestamp() * 1000)}_{random.randint(100, 999)}",
                        "variant": variant,
                        "selected_sku_id": selected.sku_id,
                        "product_name": selected.product_name,
                        "medical_logic": str(llm_data.get("medical_logic") or policy_like["logic_type"]),
                        "package_name": pkg,
                        "sales_copy": sales_copy,
                        "pricing_strategy": {
                            "addon_price": addon_price,
                            "original_price": selected.original_price,
                            "display_tag": f"按商品原价 ¥{addon_price:.2f}",
                        },
                        "projected_profit": projected_profit,
                        "decision_trace": {
                            "source": "bailian_llm",
                            "price_mode": "original_price",
                            "confidence": float(llm_data.get("confidence", 0.5) or 0.5),
                            "medical_reason": str(llm_data.get("medical_reason", "")),
                            "model": model_used,
                        },
                    }
                }
        if force_ai_only:
            raise RuntimeError("AI未返回可用结果（可能超时或返回SKU不在候选池）。")
    elif force_ai_only:
        raise RuntimeError("AI当前未启用。")
    return engine.recommend(
        engine_input=engine_input,
        policy=engine_policy,
        strategy=engine_strategy,
        candidates=engine_candidates,
    )


async def run_ai_logic(payload: RecommendRequest, variant: str) -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM policies WHERE category=? AND active=1 ORDER BY updated_at DESC LIMIT 1",
        (payload.main_item.category,),
    )
    policy = cur.fetchone()
    conn.close()
    if not policy:
        raise HTTPException(status_code=404, detail="当前类目没有启用策略")
    candidates = select_candidates_by_pool_or_db(payload)
    if not candidates:
        raise HTTPException(status_code=400, detail="候选池为空")
    strategy = get_latest_strategy()
    try:
        return await asyncio.to_thread(
            _build_recommendation_result,
            payload.main_item,
            payload.user_id,
            candidates,
            {"logic_type": policy["logic_type"], "prompt_hint": policy["prompt_hint"], "margin_rate": policy["margin_rate"]},
            strategy,
            variant,
            True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def write_log(
    req: RecommendRequest,
    result: dict[str, Any] | None,
    source: str,
    latency_ms: int,
    result_status: str,
) -> None:
    conn = db_conn()
    cur = conn.cursor()
    rec = result.get("recommendation", {}) if result else {}
    cur.execute(
        """
        INSERT INTO recommendation_logs (
            main_sku_id, main_category, selected_sku_id, addon_price,
            projected_profit, source, latency_ms, result_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            req.main_item.sku_id,
            req.main_item.category,
            rec.get("selected_sku_id"),
            rec.get("pricing_strategy", {}).get("addon_price"),
            rec.get("projected_profit"),
            source,
            latency_ms,
            result_status,
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/admin/ai-status")
def ai_status() -> dict[str, Any]:
    setting = _refresh_ai_brain_from_setting()
    return {
        "enabled": ai_brain.is_enabled(),
        "model": setting.get("model", ai_brain.model),
        "base_url": ai_brain.base_url,
        "timeout_sec": ai_brain.timeout,
        "api_key_configured": bool(ai_brain.api_key),
    }


@app.get("/api/admin/ai-ping")
def ai_ping() -> dict[str, Any]:
    if not _is_ai_allowed():
        return {"ok": False, "reason": "ai_disabled"}
    try:
        # Minimal model reachability probe.
        res = ai_brain.recommend(
            {"sku_id": "PING001", "product_name": "测试商品A", "category": "测试", "price": 10, "cost": 8},
            {"logic_type": "测试逻辑", "prompt_hint": "测试提示", "margin_rate": 0.35},
            {"pricing_rules": {"anchor_ratio": 0.42, "min_margin_rate": 0.35}, "forbidden_terms": []},
            [{"sku_id": "PING002", "product_name": "测试商品B", "category": "测试", "cost": 5, "original_price": 20}],
            "A",
        )
        if not res:
            return {"ok": False, "reason": "empty_response"}
        return {"ok": True, "model": ai_brain.model}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "admin.html", {})


@app.get("/api/admin/policies")
def list_policies() -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    rows = cur.execute("SELECT * FROM policies ORDER BY id DESC").fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


class PolicyIn(BaseModel):
    category: str
    logic_type: str
    prompt_hint: str
    margin_rate: float
    active: bool = True


@app.post("/api/admin/policies")
def create_policy(data: PolicyIn) -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO policies (category, logic_type, prompt_hint, margin_rate, active, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            data.category,
            data.logic_type,
            data.prompt_hint,
            data.margin_rate,
            1 if data.active else 0,
            now_iso(),
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"id": new_id, "message": "created"}


@app.put("/api/admin/policies/{policy_id}")
def update_policy(policy_id: int, data: PolicyIn) -> dict[str, str]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE policies
        SET category=?, logic_type=?, prompt_hint=?, margin_rate=?, active=?, updated_at=?
        WHERE id=?
        """,
        (
            data.category,
            data.logic_type,
            data.prompt_hint,
            data.margin_rate,
            1 if data.active else 0,
            now_iso(),
            policy_id,
        ),
    )
    conn.commit()
    conn.close()
    return {"message": "updated"}


@app.delete("/api/admin/policies/{policy_id}")
def delete_policy(policy_id: int) -> dict[str, str]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM policies WHERE id=?", (policy_id,))
    conn.commit()
    conn.close()
    return {"message": "deleted"}


@app.get("/api/admin/metrics")
def metrics() -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) AS cnt FROM recommendation_logs").fetchone()["cnt"]
    success = cur.execute(
        "SELECT COUNT(*) AS cnt FROM recommendation_logs WHERE result_status='ok'"
    ).fetchone()["cnt"]
    cache_hits = cur.execute(
        "SELECT COUNT(*) AS cnt FROM recommendation_logs WHERE source='cache'"
    ).fetchone()["cnt"]
    avg_profit = cur.execute(
        "SELECT COALESCE(AVG(projected_profit), 0) AS val FROM recommendation_logs"
    ).fetchone()["val"]
    ctr = cur.execute(
        """
        SELECT
        CAST(SUM(CASE WHEN event_type='click' THEN 1 ELSE 0 END) AS REAL) /
        NULLIF(SUM(CASE WHEN event_type='exposure' THEN 1 ELSE 0 END), 0) AS v
        FROM recommendation_events
        """
    ).fetchone()["v"]
    cvr = cur.execute(
        """
        SELECT
        CAST(SUM(CASE WHEN event_type='order_addon' THEN 1 ELSE 0 END) AS REAL) /
        NULLIF(SUM(CASE WHEN event_type='click' THEN 1 ELSE 0 END), 0) AS v
        FROM recommendation_events
        """
    ).fetchone()["v"]
    revenue = cur.execute(
        "SELECT COALESCE(SUM(revenue), 0) AS v FROM recommendation_events WHERE event_type='order_addon'"
    ).fetchone()["v"]
    margin = cur.execute(
        "SELECT COALESCE(SUM(margin), 0) AS v FROM recommendation_events WHERE event_type='order_addon'"
    ).fetchone()["v"]
    ai_total = cur.execute(
        "SELECT COUNT(*) AS cnt FROM recommendation_logs WHERE source LIKE 'ai_%'"
    ).fetchone()["cnt"]
    ai_success = cur.execute(
        "SELECT COUNT(*) AS cnt FROM recommendation_logs WHERE source='ai_realtime' AND result_status='ok'"
    ).fetchone()["cnt"]
    ai_fallback = cur.execute(
        "SELECT COUNT(*) AS cnt FROM recommendation_logs WHERE source='fallback' AND result_status='timeout_or_error'"
    ).fetchone()["cnt"]
    token = cur.execute("SELECT COALESCE(SUM(total_tokens),0) AS v FROM ai_usage_logs").fetchone()["v"]
    cost = cur.execute("SELECT COALESCE(SUM(estimated_cost_usd),0) AS v FROM ai_usage_logs").fetchone()["v"]
    conn.close()
    hit_rate = round(cache_hits / total, 3) if total else 0
    success_rate = round(success / total, 3) if total else 0
    return {
        "total_requests": total,
        "success_rate": success_rate,
        "cache_hit_rate": hit_rate,
        "avg_projected_profit": round(avg_profit, 2),
        "ctr": round(ctr or 0, 3),
        "cvr": round(cvr or 0, 3),
        "addon_revenue": round(revenue or 0, 2),
        "addon_margin": round(margin or 0, 2),
        "ai_total_calls": ai_total,
        "ai_success_rate": round((ai_success / ai_total), 3) if ai_total else 0,
        "ai_fallback_rate": round((ai_fallback / max(total, 1)), 3),
        "ai_total_tokens": int(token or 0),
        "ai_estimated_cost_usd": round(float(cost or 0), 6),
    }


@app.post("/api/recommend")
async def recommend(req: RecommendRequest) -> dict[str, Any]:
    started = datetime.now()
    cache_key = f"{req.main_item.sku_id}:{req.main_item.category}"
    conn = db_conn()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT payload, expires_at FROM cached_recommendations WHERE cache_key=?",
        (cache_key,),
    ).fetchone()
    conn.close()

    if row and datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc):
        result = json.loads(row["payload"])
        latency = int((datetime.now() - started).total_seconds() * 1000)
        write_log(req, result, "cache", latency, "ok")
        return result

    variant = assign_variant(req.user_id)
    try:
        result = await asyncio.wait_for(run_ai_logic(req, variant), timeout=1.5)
        latency = int((datetime.now() - started).total_seconds() * 1000)
        trace_source = (
            result.get("recommendation", {})
            .get("decision_trace", {})
            .get("source", "rule_engine")
        )
        log_source = "ai_realtime" if trace_source == "bailian_llm" else "rule_realtime"
        write_log(req, result, log_source, latency, "ok")
    except Exception:
        latency = int((datetime.now() - started).total_seconds() * 1000)
        write_log(req, None, "fallback", latency, "timeout_or_error")
        return {"recommendation": None, "fallback": "skip_module"}

    if req.main_item.category in {"抗生素", "降糖药", "高血压药", "降脂药"}:
        conn = db_conn()
        cur = conn.cursor()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        cur.execute(
            """
            INSERT INTO cached_recommendations (cache_key, payload, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at
            """,
            (cache_key, json.dumps(result, ensure_ascii=False), expires),
        )
        conn.commit()
        conn.close()

    return result


class EventIn(BaseModel):
    request_id: str
    event_type: str
    main_sku_id: str
    selected_sku_id: str | None = None
    variant: str | None = None
    revenue: float | None = None
    margin: float | None = None


@app.post("/api/events")
def ingest_event(event: EventIn) -> dict[str, str]:
    if event.event_type not in {"exposure", "click", "order_addon"}:
        raise HTTPException(status_code=400, detail="invalid event_type")
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO recommendation_events (
            request_id, event_type, main_sku_id, selected_sku_id, variant, revenue, margin, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.request_id,
            event.event_type,
            event.main_sku_id,
            event.selected_sku_id,
            event.variant,
            event.revenue,
            event.margin,
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()
    return {"message": "ok"}


@app.get("/api/admin/products")
def list_products(role: str | None = Query(default=None)) -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    if role:
        rows = cur.execute("SELECT * FROM products WHERE role=? ORDER BY updated_at DESC", (role,)).fetchall()
    else:
        rows = cur.execute("SELECT * FROM products ORDER BY updated_at DESC").fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


class ProductIn(BaseModel):
    sku_id: str
    product_name: str
    category: str
    role: str
    cost: float
    original_price: float
    gross_margin_rate: float
    active: bool = True


@app.post("/api/admin/products")
def upsert_product(data: ProductIn) -> dict[str, str]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO products (
            sku_id, product_name, category, role, cost, original_price, gross_margin_rate, active, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sku_id) DO UPDATE SET
            product_name=excluded.product_name,
            category=excluded.category,
            role=excluded.role,
            cost=excluded.cost,
            original_price=excluded.original_price,
            gross_margin_rate=excluded.gross_margin_rate,
            active=excluded.active,
            updated_at=excluded.updated_at
        """,
        (
            data.sku_id,
            data.product_name,
            data.category,
            data.role,
            data.cost,
            data.original_price,
            data.gross_margin_rate,
            1 if data.active else 0,
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()
    return {"message": "saved"}


@app.get("/api/admin/strategies")
def list_strategies() -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    rows = cur.execute("SELECT * FROM strategy_versions ORDER BY updated_at DESC").fetchall()
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        d["content_json"] = json.loads(d["content_json"])
        items.append(d)
    return {"items": items}


class StrategyIn(BaseModel):
    strategy_name: str
    version: str
    content_json: dict[str, Any]
    status: str = "draft"


@app.post("/api/admin/strategies")
def create_strategy(data: StrategyIn) -> dict[str, Any]:
    if data.status not in {"draft", "published"}:
        raise HTTPException(status_code=400, detail="invalid status")
    conn = db_conn()
    cur = conn.cursor()
    now = now_iso()
    pub = now if data.status == "published" else None
    cur.execute(
        """
        INSERT INTO strategy_versions (strategy_name, version, content_json, status, published_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (data.strategy_name, data.version, json.dumps(data.content_json, ensure_ascii=False), data.status, pub, now),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return {"id": rid, "message": "created"}


@app.post("/api/admin/strategies/{strategy_id}/publish")
def publish_strategy(strategy_id: int) -> dict[str, str]:
    conn = db_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT strategy_name FROM strategy_versions WHERE id=?", (strategy_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="strategy not found")
    strategy_name = row["strategy_name"]
    now = now_iso()
    cur.execute(
        "UPDATE strategy_versions SET status='draft' WHERE strategy_name=? AND status='published'",
        (strategy_name,),
    )
    cur.execute(
        "UPDATE strategy_versions SET status='published', published_at=?, updated_at=? WHERE id=?",
        (now, now, strategy_id),
    )
    conn.commit()
    conn.close()
    return {"message": "published"}


@app.get("/api/admin/experiments")
def list_experiments() -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    rows = cur.execute("SELECT * FROM experiments ORDER BY updated_at DESC").fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


class ExperimentIn(BaseModel):
    exp_name: str
    category: str = "all"
    traffic_a: float = 0.5
    traffic_b: float = 0.5
    status: str = "running"


@app.post("/api/admin/experiments")
def upsert_experiment(data: ExperimentIn) -> dict[str, str]:
    if round(data.traffic_a + data.traffic_b, 5) != 1:
        raise HTTPException(status_code=400, detail="traffic_a + traffic_b must equal 1")
    if data.status not in {"running", "paused"}:
        raise HTTPException(status_code=400, detail="invalid status")
    conn = db_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM experiments WHERE exp_name=?", (data.exp_name,)).fetchone()
    if row:
        cur.execute(
            """
            UPDATE experiments
            SET category=?, traffic_a=?, traffic_b=?, status=?, updated_at=?
            WHERE exp_name=?
            """,
            (data.category, data.traffic_a, data.traffic_b, data.status, now_iso(), data.exp_name),
        )
    else:
        cur.execute(
            """
            INSERT INTO experiments (exp_name, category, traffic_a, traffic_b, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (data.exp_name, data.category, data.traffic_a, data.traffic_b, data.status, now_iso()),
        )
    conn.commit()
    conn.close()
    return {"message": "saved"}


@app.get("/api/admin/ab-report")
def ab_report() -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT
          variant,
          SUM(CASE WHEN event_type='exposure' THEN 1 ELSE 0 END) AS exposure,
          SUM(CASE WHEN event_type='click' THEN 1 ELSE 0 END) AS click,
          SUM(CASE WHEN event_type='order_addon' THEN 1 ELSE 0 END) AS orders,
          COALESCE(SUM(CASE WHEN event_type='order_addon' THEN margin ELSE 0 END), 0) AS margin
        FROM recommendation_events
        WHERE variant IS NOT NULL
        GROUP BY variant
        """
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        exp = d["exposure"] or 0
        clk = d["click"] or 0
        d["ctr"] = round((clk / exp), 3) if exp else 0
        d["cvr"] = round((d["orders"] / clk), 3) if clk else 0
        items.append(d)
    return {"items": items}


@app.post("/api/ops/upload-catalog")
async def ops_upload_catalog(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        raise HTTPException(status_code=400, detail="仅支持 Excel 文件(.xlsx)")
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        raise HTTPException(status_code=500, detail="缺少 openpyxl 依赖") from exc

    content = await file.read()
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    row_iter = ws.iter_rows(values_only=True)
    header_row = next(row_iter, None)
    if not header_row:
        raise HTTPException(status_code=400, detail="文件为空")

    headers = [str(x).strip() if x is not None else "" for x in header_row]
    sku_idx = find_col_idx(headers, {"sku", "sku_id", "商品编码", "产品编码", "商品sku", "商品id", "货号"})
    name_idx = find_col_idx(headers, {"商品名称", "产品名称", "药品名称", "名称", "商品名", "通用名"})
    cat_idx = find_col_idx(headers, {"类目", "商品类目", "一级类目", "二级类目", "品类", "科室"})
    price_idx = find_col_idx(headers, {"成交价", "单价", "实付单价", "吊牌价", "销售价", "销售单价", "gmv"})
    cost_idx = find_col_idx(headers, {"成本", "采购价", "供货价", "成本价"})
    qty_idx = find_col_idx(headers, {"销量", "数量", "销售数量", "出库数量", "件数"})
    gmv_idx = find_col_idx(headers, {"gmv", "销售额", "订单金额"})

    missing = []
    if sku_idx is None:
        missing.append("SKU")
    if name_idx is None:
        missing.append("商品名称")
    if missing:
        raise HTTPException(status_code=400, detail=f"缺少关键字段: {', '.join(missing)}")

    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    now = now_iso()
    parsed = []
    for row in row_iter:
        sku = str(row[sku_idx]).strip() if row[sku_idx] is not None else ""
        name = str(row[name_idx]).strip() if row[name_idx] is not None else ""
        if not sku or not name:
            continue
        category = str(row[cat_idx]).strip() if cat_idx is not None and row[cat_idx] else "未分类"
        qty = int(to_float(row[qty_idx], 1)) if qty_idx is not None else 1
        if qty <= 0:
            qty = 1
        gmv = to_float(row[gmv_idx], 0) if gmv_idx is not None else 0
        price = to_float(row[price_idx], 0) if price_idx is not None else 0
        if price <= 0 and gmv > 0:
            price = round(gmv / qty, 2)
        if price <= 0:
            # 组货阶段不以价格为门槛；缺失时给占位值。
            price = 99.0
        cost = to_float(row[cost_idx], 0) if cost_idx is not None else 0
        if cost <= 0:
            cost = round(price * 0.78, 2)
        if gmv <= 0:
            gmv = round(price * qty, 2)
        parsed.append((batch_id, sku, name, category, price, cost, max(qty, 1), gmv, infer_role(category, name), now))

    if not parsed:
        raise HTTPException(status_code=400, detail="没有可导入的数据行")

    conn = db_conn()
    cur = conn.cursor()
    try:
        _execute_with_retry(
            cur,
            "INSERT INTO upload_batches (id, filename, total_rows, created_at) VALUES (?, ?, ?, ?)",
            (batch_id, file.filename, len(parsed), now),
        )
        _executemany_with_retry(
            cur,
            """
            INSERT INTO uploaded_products (
              batch_id, sku_id, product_name, category, price, cost, qty, gmv, role_hint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            parsed,
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "locked" in str(exc).lower():
            raise HTTPException(status_code=503, detail="数据库忙，请3秒后重试一次上传") from exc
        raise
    conn.close()
    return {"batch_id": batch_id, "total_rows": len(parsed)}


@app.post("/api/ops/library/upload")
async def ops_upload_library(
    file: UploadFile = File(...),
    default_role: str = Query(default="addon"),
) -> dict[str, Any]:
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        raise HTTPException(status_code=400, detail="仅支持 Excel 文件(.xlsx)")
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        raise HTTPException(status_code=500, detail="缺少 openpyxl 依赖") from exc
    content = await file.read()
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=400, detail="文件为空")
    headers = [str(x).strip() if x is not None else "" for x in rows[0]]
    sku_idx = find_col_idx(headers, {"sku", "sku_id", "商品编码", "商品sku", "商品id", "货号"})
    name_idx = find_col_idx(headers, {"商品名称", "产品名称", "药品名称", "名称", "商品名", "通用名"})
    product_code_idx = find_col_idx(headers, {"产品编码", "药网编码", "产品id"})
    manufacturer_idx = find_col_idx(headers, {"生产厂商", "厂商", "厂家"})
    department_idx = find_col_idx(headers, {"科室"})
    item_code_idx = find_col_idx(headers, {"商品编码"})
    level1_cat_idx = find_col_idx(headers, {"一级类目", "类目", "商品类目", "品类"})
    cat_idx = level1_cat_idx if level1_cat_idx is not None else department_idx
    price_idx = find_col_idx(headers, {"成交价", "单价", "实付单价", "吊牌价", "销售价", "销售单价", "gmv"})
    cost_idx = find_col_idx(headers, {"成本", "采购价", "供货价", "成本价", "revenue"})
    role_idx = find_col_idx(headers, {"角色", "role", "商品角色", "类型"})
    if sku_idx is None:
        sku_idx = item_code_idx
    if sku_idx is None or name_idx is None:
        raise HTTPException(status_code=400, detail="缺少关键字段: 商品编码(或SKU)/产品名称")
    now = now_iso()
    upserts = []
    for row in rows[1:]:
        sku = str(row[sku_idx]).strip() if row[sku_idx] is not None else ""
        name = str(row[name_idx]).strip() if row[name_idx] is not None else ""
        if not sku or not name:
            continue
        category = str(row[cat_idx]).strip() if cat_idx is not None and row[cat_idx] else "未分类"
        product_code = str(row[product_code_idx]).strip() if product_code_idx is not None and row[product_code_idx] else ""
        manufacturer = str(row[manufacturer_idx]).strip() if manufacturer_idx is not None and row[manufacturer_idx] else ""
        department = str(row[department_idx]).strip() if department_idx is not None and row[department_idx] else ""
        item_code = str(row[item_code_idx]).strip() if item_code_idx is not None and row[item_code_idx] else sku
        level1_category = str(row[level1_cat_idx]).strip() if level1_cat_idx is not None and row[level1_cat_idx] else category
        price = to_float(row[price_idx], 0) if price_idx is not None else 0
        # 库存清单常常没有价格字段，使用默认定价占位，后续可在后台维护真实价格。
        if price <= 0:
            price = 99.0
        cost = to_float(row[cost_idx], 0) if cost_idx is not None else 0
        if cost <= 0:
            cost = round(price * 0.78, 2)
        role_val = str(row[role_idx]).strip().lower() if role_idx is not None and row[role_idx] else ""
        role = role_val if role_val in {"main", "addon"} else default_role
        if role not in {"main", "addon"}:
            role = infer_role(category, name)
        margin_rate = max(0.01, min(0.95, (price - cost) / max(price, 1)))
        upserts.append(
            (
                sku,
                name,
                product_code,
                manufacturer,
                department,
                item_code,
                level1_category,
                category,
                role,
                cost,
                price,
                round(margin_rate, 3),
                1,
                now,
            )
        )
    if not upserts:
        raise HTTPException(status_code=400, detail="没有可导入商品")
    conn = db_conn()
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO products (
          sku_id, product_name, product_code, manufacturer, department, item_code, level1_category,
          category, role, cost, original_price, gross_margin_rate, active, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sku_id) DO UPDATE SET
          product_name=excluded.product_name,
          product_code=excluded.product_code,
          manufacturer=excluded.manufacturer,
          department=excluded.department,
          item_code=excluded.item_code,
          level1_category=excluded.level1_category,
          category=excluded.category,
          role=excluded.role,
          cost=excluded.cost,
          original_price=excluded.original_price,
          gross_margin_rate=excluded.gross_margin_rate,
          active=excluded.active,
          updated_at=excluded.updated_at
        """,
        upserts,
    )
    conn.commit()
    main_cnt = cur.execute("SELECT COUNT(*) AS c FROM products WHERE role='main' AND active=1").fetchone()["c"]
    addon_cnt = cur.execute("SELECT COUNT(*) AS c FROM products WHERE role='addon' AND active=1").fetchone()["c"]
    conn.close()
    return {"imported": len(upserts), "main_count": main_cnt, "addon_count": addon_cnt}


@app.get("/api/ops/library/stats")
def ops_library_stats() -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) AS c FROM products WHERE active=1").fetchone()["c"]
    main_cnt = cur.execute("SELECT COUNT(*) AS c FROM products WHERE role='main' AND active=1").fetchone()["c"]
    addon_cnt = cur.execute("SELECT COUNT(*) AS c FROM products WHERE role='addon' AND active=1").fetchone()["c"]
    conn.close()
    return {"total": total, "main_count": main_cnt, "addon_count": addon_cnt}


@app.get("/api/ops/inventory/list")
def ops_inventory_list(
    q: str | None = Query(default=None),
    role: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    where = ["active=1"]
    params: list[Any] = []
    if q:
        where.append("(sku_id LIKE ? OR product_name LIKE ? OR category LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if role in {"main", "addon"}:
        where.append("role=?")
        params.append(role)
    where_sql = " AND ".join(where)
    total = cur.execute(f"SELECT COUNT(*) AS c FROM products WHERE {where_sql}", tuple(params)).fetchone()["c"]
    rows = cur.execute(
        f"""
        SELECT
          sku_id,
          product_name,
          COALESCE(product_code, '') AS product_code,
          COALESCE(manufacturer, '') AS manufacturer,
          COALESCE(department, '') AS department,
          COALESCE(item_code, '') AS item_code,
          COALESCE(level1_category, '') AS level1_category,
          category,
          role,
          cost,
          original_price,
          gross_margin_rate,
          active,
          updated_at
        FROM products
        WHERE {where_sql}
        ORDER BY updated_at DESC
        LIMIT ? OFFSET ?
        """,
        tuple(params + [limit, offset]),
    ).fetchall()
    conn.close()
    return {"total": total, "items": [dict(r) for r in rows]}


@app.post("/api/ops/inventory/upload")
async def ops_inventory_upload(
    file: UploadFile = File(...),
    default_role: str = Query(default="addon"),
) -> dict[str, Any]:
    return await ops_upload_library(file=file, default_role=default_role)


@app.get("/api/admin/budget")
def admin_budget() -> dict[str, Any]:
    setting = _refresh_ai_brain_from_setting()
    conn = db_conn()
    cur = conn.cursor()
    usage = cur.execute(
        """
        SELECT
          COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
          COALESCE(SUM(completion_tokens),0) AS completion_tokens,
          COALESCE(SUM(total_tokens),0) AS total_tokens,
          COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost
        FROM ai_usage_logs
        """
    ).fetchone()
    conn.close()
    monthly_budget = float(setting.get("monthly_budget_usd", 50) or 50)
    estimated = float(usage["estimated_cost"] or 0)
    rate = round((estimated / monthly_budget), 4) if monthly_budget > 0 else 0
    return {
        "provider": setting.get("provider", "bailian"),
        "model": setting.get("model", ai_brain.model),
        "enabled": bool(setting.get("enabled", 1)),
        "base_url": str(setting.get("base_url") or ai_brain.base_url),
        "timeout_sec": float(setting.get("timeout_sec") or ai_brain.timeout),
        "api_key_configured": bool(setting.get("api_key") or ai_brain.api_key),
        "monthly_budget_usd": monthly_budget,
        "input_cost_per_1k": float(setting.get("input_cost_per_1k", 0.0012) or 0.0012),
        "output_cost_per_1k": float(setting.get("output_cost_per_1k", 0.0024) or 0.0024),
        "prompt_tokens": int(usage["prompt_tokens"] or 0),
        "completion_tokens": int(usage["completion_tokens"] or 0),
        "total_tokens": int(usage["total_tokens"] or 0),
        "estimated_cost_usd": round(estimated, 6),
        "budget_used_rate": rate,
    }


class BudgetSettingIn(BaseModel):
    model: str
    api_key: str | None = None
    base_url: str | None = None
    timeout_sec: float = 1.2
    enabled: bool = True
    monthly_budget_usd: float = 50
    input_cost_per_1k: float = 0.0012
    output_cost_per_1k: float = 0.0024


@app.post("/api/admin/budget/model")
def admin_update_budget_setting(data: BudgetSettingIn) -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM llm_settings ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    api_key_to_save = (data.api_key or "").strip() if data.api_key is not None else (row["api_key"] if row else "")
    if row:
        cur.execute(
            """
            UPDATE llm_settings
            SET model=?, api_key=?, base_url=?, timeout_sec=?, enabled=?, monthly_budget_usd=?, input_cost_per_1k=?, output_cost_per_1k=?, updated_at=?
            WHERE id=?
            """,
            (
                data.model,
                api_key_to_save,
                (data.base_url or "").strip() or ai_brain.base_url,
                max(float(data.timeout_sec), 0.5),
                1 if data.enabled else 0,
                data.monthly_budget_usd,
                data.input_cost_per_1k,
                data.output_cost_per_1k,
                now_iso(),
                row["id"],
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO llm_settings (
              provider, model, api_key, base_url, timeout_sec, enabled, monthly_budget_usd, input_cost_per_1k, output_cost_per_1k, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bailian",
                data.model,
                api_key_to_save,
                (data.base_url or "").strip() or ai_brain.base_url,
                max(float(data.timeout_sec), 0.5),
                1 if data.enabled else 0,
                data.monthly_budget_usd,
                data.input_cost_per_1k,
                data.output_cost_per_1k,
                now_iso(),
            ),
        )
    conn.commit()
    conn.close()
    _refresh_ai_brain_from_setting()
    return {"message": "saved"}


@app.post("/api/ops/generate-strategies")
def ops_generate_strategies(
    batch_id: str,
    top_n: int = Query(default=300, ge=1, le=5000),
    sort_by: str = Query(default="qty"),
    candidate_source: str = Query(default="library"),
    use_ai: bool = Query(default=True),
    force_ai_only: bool = Query(default=False),
) -> dict[str, Any]:
    from collections import Counter

    conn = db_conn()
    cur = conn.cursor()
    relation_engine = BundleEngine()
    order_sql = "total_qty DESC, total_gmv DESC"
    if sort_by == "gmv":
        order_sql = "total_gmv DESC, total_qty DESC"
    mains = cur.execute(
        f"""
        SELECT
          sku_id,
          MIN(product_name) AS product_name,
          MIN(category) AS category,
          AVG(price) AS price,
          AVG(cost) AS cost,
          SUM(qty) AS total_qty,
          SUM(gmv) AS total_gmv
        FROM uploaded_products
        WHERE batch_id=?
        GROUP BY sku_id
        ORDER BY {order_sql}
        LIMIT ?
        """,
        (batch_id, top_n),
    ).fetchall()
    if not mains:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="该 batch_id 在数据库中没有待组货商品。请重新上传 Excel，或确认服务使用的 APP_DB_PATH 与上传时一致。",
        )
    batch_candidates = cur.execute(
        """
        SELECT
          sku_id,
          MIN(product_name) AS product_name,
          MIN(category) AS category,
          AVG(price) AS price,
          AVG(cost) AS cost
        FROM uploaded_products
        WHERE batch_id=?
        GROUP BY sku_id
        """,
        (batch_id,),
    ).fetchall()
    library_candidates = cur.execute(
        "SELECT sku_id, product_name, category, original_price AS price, cost FROM products WHERE active=1"
    ).fetchall()
    if candidate_source == "batch":
        candidate_rows = batch_candidates
    elif candidate_source == "mixed":
        candidate_rows = batch_candidates + library_candidates
    else:
        candidate_rows = library_candidates
    if not candidate_rows:
        candidate_rows = batch_candidates or library_candidates

    strategy = get_latest_strategy()
    created = 0
    ai_count = 0
    rule_count = 0
    selected_counter: Counter[str] = Counter()
    skip_no_candidates = 0
    skip_exception = 0
    errors: list[dict[str, str]] = []
    now = now_iso()
    try:
        _execute_with_retry(cur, "DELETE FROM bundle_recommendations WHERE batch_id=?", (batch_id,))
    except sqlite3.OperationalError as exc:
        conn.close()
        if "locked" in str(exc).lower():
            raise HTTPException(status_code=503, detail="数据库忙，请稍后重试生成") from exc
        raise
    for m in mains:
        policy = cur.execute(
            "SELECT * FROM policies WHERE category=? AND active=1 ORDER BY updated_at DESC LIMIT 1",
            (m["category"],),
        ).fetchone()
        if not policy:
            policy = {"logic_type": "慢病管理", "prompt_hint": "建议结合当前症状进行综合健康管理。", "margin_rate": 0.35}
        main_item = MainItem(
            sku_id=m["sku_id"],
            product_name=m["product_name"],
            category=m["category"],
            price=m["price"],
            cost=m["cost"],
        )
        main_cat = str(m["category"] or "")
        rel_tokens = set(relation_engine.relation_map.get(main_cat, set()))
        if main_cat:
            rel_tokens.add(main_cat)
        prefetched: list[CandidateItem] = []
        fallback_pool: list[CandidateItem] = []
        for a in candidate_rows:
            if a["sku_id"] == m["sku_id"]:
                continue
            item = CandidateItem(
                sku_id=a["sku_id"],
                product_name=a["product_name"],
                cost=a["cost"],
                original_price=a["price"],
                category=a["category"],
            )
            if relation_engine.is_addon_inappropriate_for_main(
                m["product_name"], m["category"], a["product_name"], a["category"]
            ):
                continue
            text = f"{a['product_name']}{a['category'] or ''}"
            if rel_tokens and any(t in text for t in rel_tokens):
                prefetched.append(item)
            else:
                fallback_pool.append(item)
        # 先用强相关候选，不足时补充，且限制总候选规模，避免几千SKU导致长时间卡住。
        candidate_pool = (prefetched[:260] + fallback_pool[:120])[:320]
        if not candidate_pool:
            skip_no_candidates += 1
            continue
        try:
            result = _build_recommendation_result(
                main_item=main_item,
                user_id=None,
                candidates=candidate_pool,
                policy_like={
                    "logic_type": policy["logic_type"],
                    "prompt_hint": policy["prompt_hint"],
                    "margin_rate": policy["margin_rate"],
                },
                strategy=strategy,
                variant="A",
                prefer_ai=use_ai,
                force_ai_only=force_ai_only,
            )
            # 限制同一副品在单批次中的占比，防止“单品灌全场”。
            rec_selected_name = result.get("recommendation", {}).get("product_name", "")
            if rec_selected_name:
                future_total = created + 1
                future_cnt = selected_counter[rec_selected_name] + 1
                max_share = 0.12
                if future_total >= 30 and (future_cnt / future_total) > max_share:
                    filtered_pool = [c for c in candidate_pool if c.product_name != rec_selected_name]
                    if filtered_pool:
                        result = _build_recommendation_result(
                            main_item=main_item,
                            user_id=None,
                            candidates=filtered_pool,
                            policy_like={
                                "logic_type": policy["logic_type"],
                                "prompt_hint": policy["prompt_hint"],
                                "margin_rate": policy["margin_rate"],
                            },
                            strategy=strategy,
                            variant="A",
                            prefer_ai=use_ai,
                            force_ai_only=False,
                        )
        except ValueError as exc:
            if "候选池为空" in str(exc):
                skip_no_candidates += 1
            else:
                skip_exception += 1
                if len(errors) < 20:
                    errors.append({"sku_id": m["sku_id"], "product_name": m["product_name"], "reason": str(exc)})
            continue
        except Exception as exc:
            skip_exception += 1
            if len(errors) < 20:
                errors.append({"sku_id": m["sku_id"], "product_name": m["product_name"], "reason": str(exc) or "unexpected_error"})
            continue
        rec = result["recommendation"]
        selected_counter[rec["product_name"]] += 1
        src = rec.get("decision_trace", {}).get("source", "rule_engine")
        if src == "bailian_llm":
            ai_count += 1
        else:
            rule_count += 1
        main_ic = _resolve_item_code(cur, str(m["sku_id"]))
        sel_ic = _resolve_item_code(cur, str(rec["selected_sku_id"]))
        pkg_nm = (rec.get("package_name") or "").strip() or BundleEngine().build_package_name(
            m["product_name"],
            m["category"],
            rec["product_name"],
            None,
            str(rec.get("medical_logic") or ""),
        )
        rec_out = dict(rec)
        rec_out["package_name"] = pkg_nm
        _execute_with_retry(
            cur,
            """
            INSERT INTO bundle_recommendations (
              batch_id, main_sku_id, main_product_name, main_category, selected_sku_id,
              selected_product_name, medical_logic, addon_price, projected_profit, sales_copy,
              package_name, main_item_code, selected_item_code,
              decision_payload, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                batch_id,
                m["sku_id"],
                m["product_name"],
                m["category"],
                rec["selected_sku_id"],
                rec["product_name"],
                rec["medical_logic"],
                rec["pricing_strategy"]["addon_price"],
                rec["projected_profit"],
                rec["sales_copy"],
                pkg_nm,
                main_ic,
                sel_ic,
                json.dumps(rec_out, ensure_ascii=False),
                now,
                now,
            ),
        )
        created += 1

    try:
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        conn.close()
        if "locked" in str(exc).lower():
            raise HTTPException(status_code=503, detail="数据库忙，请稍后重试生成") from exc
        raise
    conn.close()
    return {
        "batch_id": batch_id,
        "generated_count": created,
        "top_n": top_n,
        "sort_by": sort_by,
        "candidate_source": candidate_source,
        "use_ai": use_ai,
        "ai_runtime_enabled": _is_ai_allowed(),
        "ai_generated_count": ai_count,
        "rule_generated_count": rule_count,
        "diagnostics": {
            "main_items_processed": len(mains),
            "candidate_pool_size": len(candidate_rows),
            "skip_no_candidates": skip_no_candidates,
            "skip_exception": skip_exception,
            "top_selected_products": selected_counter.most_common(8),
            "errors": errors,
        },
    }


@app.get("/api/ops/strategies")
def ops_list_strategies(
    batch_id: str,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    where = "batch_id=?"
    params: list[Any] = [batch_id]
    if status:
        where += " AND status=?"
        params.append(status)
    total = cur.execute(
        f"SELECT COUNT(*) AS c FROM bundle_recommendations WHERE {where}",
        tuple(params),
    ).fetchone()["c"]
    rows = cur.execute(
        f"SELECT * FROM bundle_recommendations WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params + [limit, offset]),
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        source = "rule_engine"
        try:
            payload = json.loads(d.get("decision_payload") or "{}")
            source = payload.get("decision_trace", {}).get("source", "rule_engine")
        except Exception:
            source = "rule_engine"
        d["source"] = source
        d["source_label"] = "百炼AI" if source == "bailian_llm" else "规则引擎"
        _enrich_bundle_row(cur, d)
        items.append(d)
    conn.close()
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/api/ops/strategies/export")
def ops_export_strategies(batch_id: str) -> Response:
    """导出当前批次全部组货为 CSV（UTF-8 BOM，Excel 可直接打开）。"""
    conn = db_conn()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT * FROM bundle_recommendations
        WHERE batch_id=?
        ORDER BY id ASC
        """,
        (batch_id,),
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "ID",
            "套餐名称",
            "商品A",
            "商品A商品编码",
            "商品B",
            "商品B商品编码",
            "组货卖点",
            "商品价格",
            "参考毛利",
        ]
    )
    for r in rows:
        d = dict(r)
        _enrich_bundle_row(cur, d)
        writer.writerow(
            [
                d.get("id"),
                d.get("package_name") or "",
                d.get("main_product_name") or "",
                d.get("main_item_code") or d.get("main_sku_id") or "",
                d.get("selected_product_name") or "",
                d.get("selected_item_code") or d.get("selected_sku_id") or "",
                (d.get("sales_copy") or "").replace("\r\n", " ").replace("\n", " "),
                d.get("addon_price"),
                d.get("projected_profit"),
            ]
        )
    conn.close()
    raw = "\ufeff" + buf.getvalue()
    fname = f"bundles_{batch_id}.csv"
    return Response(
        content=raw.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/ops/strategies/{item_id}/confirm")
def ops_confirm_strategy(item_id: int) -> dict[str, str]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE bundle_recommendations SET status='confirmed', updated_at=? WHERE id=?", (now_iso(), item_id))
    conn.commit()
    conn.close()
    return {"message": "confirmed"}


@app.post("/api/ops/sync")
def ops_sync_confirmed(batch_id: str) -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT * FROM bundle_recommendations WHERE batch_id=? AND status='confirmed'",
        (batch_id,),
    ).fetchall()
    now = now_iso()
    synced = 0
    for r in rows:
        cur.execute(
            """
            INSERT INTO bundle_rules (
              main_sku_id, main_product_name, selected_sku_id, selected_product_name,
              addon_price, medical_logic, sales_copy, active, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(main_sku_id) DO UPDATE SET
              main_product_name=excluded.main_product_name,
              selected_sku_id=excluded.selected_sku_id,
              selected_product_name=excluded.selected_product_name,
              addon_price=excluded.addon_price,
              medical_logic=excluded.medical_logic,
              sales_copy=excluded.sales_copy,
              active=1,
              updated_at=excluded.updated_at
            """,
            (
                r["main_sku_id"],
                r["main_product_name"],
                r["selected_sku_id"],
                r["selected_product_name"],
                r["addon_price"],
                r["medical_logic"],
                r["sales_copy"],
                now,
            ),
        )
        cur.execute("UPDATE bundle_recommendations SET status='published', updated_at=? WHERE id=?", (now, r["id"]))
        synced += 1
    conn.commit()
    conn.close()
    return {"batch_id": batch_id, "synced_count": synced}


@app.get("/api/ops/workbench")
def ops_workbench(batch_id: str) -> dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) AS c FROM bundle_recommendations WHERE batch_id=?", (batch_id,)).fetchone()["c"]
    draft = cur.execute(
        "SELECT COUNT(*) AS c FROM bundle_recommendations WHERE batch_id=? AND status='draft'",
        (batch_id,),
    ).fetchone()["c"]
    confirmed = cur.execute(
        "SELECT COUNT(*) AS c FROM bundle_recommendations WHERE batch_id=? AND status='confirmed'",
        (batch_id,),
    ).fetchone()["c"]
    published = cur.execute(
        "SELECT COUNT(*) AS c FROM bundle_recommendations WHERE batch_id=? AND status='published'",
        (batch_id,),
    ).fetchone()["c"]
    sku_count = cur.execute(
        "SELECT COUNT(DISTINCT sku_id) AS c FROM uploaded_products WHERE batch_id=? AND role_hint='main'",
        (batch_id,),
    ).fetchone()["c"]
    top_qty_rows = cur.execute(
        """
        SELECT sku_id, MIN(product_name) AS product_name, SUM(qty) AS total_qty, SUM(gmv) AS total_gmv
        FROM uploaded_products
        WHERE batch_id=? AND role_hint='main'
        GROUP BY sku_id
        ORDER BY total_qty DESC, total_gmv DESC
        LIMIT 10
        """,
        (batch_id,),
    ).fetchall()
    conn.close()
    return {
        "batch_id": batch_id,
        "sku_count": sku_count,
        "total": total,
        "draft": draft,
        "confirmed": confirmed,
        "published": published,
        "top_products": [dict(r) for r in top_qty_rows],
    }

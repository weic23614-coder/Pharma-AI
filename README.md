# 1药网 AI 智能组货与动态定价引擎（本地可用版）

这是一套可直接演示和二次开发的本地系统，实现了：

- 结算侧推荐 API（输入主品+候选池，输出推荐 SKU、医嘱文案、换购价、预计毛利）
- 高并发兜底（1.5 秒超时即降级，不阻断交易主链路）
- 高频类目缓存（24h 缓存，降低模型调用成本）
- 操作后台（策略维护、商品池管理、策略版本发布、AB实验、指标看板、Demo 一键演示）
- 事件回传（曝光/点击/加购下单）与 AB 报表

## 1. 启动方式

```bash
cd /Users/weipeng/1yaowang-ai-mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8088
```

打开：

- 后台页面：`http://127.0.0.1:8088/admin`
- 健康检查：`http://127.0.0.1:8088/health`

## 2. 与 1药网现有系统对接路径

1. C 端进入结算页，原有结算 API 照常返回。
2. 在结算服务并行异步调用本服务 `/api/recommend`。
3. 本服务命中缓存则直接返回；否则实时生成推荐。
4. 1药网前端收到结果后渲染推荐卡片，用户勾选后以换购价加购 SKU。
5. 超时或异常时返回 `fallback=skip_module`，前端隐藏卡片，不影响主流程。

## 3. 推荐 API 示例

`POST /api/recommend`

请求体（精简）：

```json
{
  "user_intent": "checkout",
  "main_item": {
    "sku_id": "A300",
    "product_name": "缬沙坦胶囊",
    "category": "高血压药",
    "price": 32,
    "cost": 27
  },
  "candidate_pool": [
    {
      "sku_id": "B901",
      "product_name": "上臂式电子血压计",
      "cost": 88,
      "original_price": 259
    }
  ]
}
```

返回体（精简）：

```json
{
  "recommendation": {
    "selected_sku_id": "B901",
    "medical_logic": "慢病管理",
    "sales_copy": "【药师建议】......",
    "pricing_strategy": {
      "addon_price": 108.78,
      "display_tag": "加109元换购价"
    },
    "projected_profit": 20.78
  }
}
```

## 4. 后台能力（当前版本）

- 类目策略：类目、医学逻辑、医嘱提示、毛利率、启停状态
- 商品池：主品与副品统一维护（SKU、成本、原价、毛利率）
- 策略版本：草稿/发布管理，支持快速回滚思路
- AB 实验：A/B流量比例配置，输出 Variant 级报表
- 指标总览：请求量、成功率、缓存命中率、CTR、CVR、加购销售额、加购毛利
- 在线 Demo：降糖药/高血压药一键触发 + 漏斗事件模拟

## 5. 智能组货引擎结构

已将决策逻辑独立为 `app/bundle_engine.py`，采用分层流程：

- 拦截层：医学安全过滤（不符合主品类医学逻辑的副品直接剔除）
- 召回层：优先使用入参候选池，兜底从商品池召回可用副品
- 评分层：按 `医学匹配 + 毛利贡献 + 可负担性` 综合评分
- 定价层：`max(成本底价, 锚定价)` 输出换购价
- 文案层：医嘱式文案生成 + 禁用词过滤

接口层 `app/main.py` 只负责 API、缓存、日志、AB 分流，后续扩类目可只调策略数据。

## 6. API 一览

- `POST /api/recommend`：核心推荐接口
- `POST /api/events`：上报曝光/点击/加购事件
- `GET /api/admin/metrics`：总览指标
- `GET/POST /api/admin/policies`：类目策略管理
- `GET/POST /api/admin/products`：商品池管理
- `GET/POST /api/admin/strategies`：策略版本管理
- `POST /api/admin/strategies/{id}/publish`：发布版本
- `GET/POST /api/admin/experiments`：AB实验管理
- `GET /api/admin/ab-report`：AB 报表

## 7. 下一步建议（生产化）

- 把 `run_ai_logic` 替换为企业内模型服务（或任意 LLM 网关）
- 加入人工审核流（新策略先灰度，再全量）
- 加 AB 实验维度（人群分层、类目分层、文案版本、价格版本）
- 打通订单回传，计算真实 CVR 与绝对毛利提升

## 8. 订单清单导入（昨天售卖商品）

已提供导入脚本：`scripts/import_sales_catalog.py`

示例：

```bash
cd /Users/weipeng/1yaowang-ai-mvp
source .venv/bin/activate
python scripts/import_sales_catalog.py \
  --excel "/Users/weipeng/Desktop/你的订单清单.xlsx" \
  --default-role main
```

说明：

- 脚本会自动识别常见中文表头（SKU、商品名称、类目、价格、成本、销量）。
- 如缺少“成本”，会按 `价格 * default-cost-rate` 估算（默认 0.78）。
- 导入目标是 `products` 表，执行 upsert（同 SKU 自动更新）。

## 9. 运营快捷模式（你提的流程）

已在后台首页新增「运营快捷工作台」，支持一条龙：

1. 上传商品清单（Excel）
2. 自动生成组货策略（草稿）
3. 逐条确认
4. 一键同步到组货系统

对应 API：

- `POST /api/ops/upload-catalog` 上传清单，返回 `batch_id`
- `POST /api/ops/generate-strategies?batch_id=...` 自动生成策略
- `GET /api/ops/strategies?batch_id=...` 查看策略列表
- `POST /api/ops/strategies/{id}/confirm` 确认单条策略
- `POST /api/ops/sync?batch_id=...` 同步已确认策略到 `bundle_rules`
- `GET /api/ops/workbench?batch_id=...` 查看批次进度

## 10. 接入百炼大模型（AI优先，规则兜底）

已支持阿里云 DashScope（OpenAI兼容）接入，接入后推荐链路为：

1. 优先调用百炼模型做选品与文案
2. 本地引擎负责价格和毛利约束
3. 如果模型异常/超时，自动回退规则引擎

环境变量：

```bash
export ENABLE_AI_BRAIN=true
export BAILIAN_API_KEY="你的百炼API_KEY"
export BAILIAN_MODEL="qwen-plus"
export BAILIAN_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export BAILIAN_TIMEOUT_SEC=1.2
```

验证接口：

- `GET /api/admin/ai-status` 查看AI开关和模型状态

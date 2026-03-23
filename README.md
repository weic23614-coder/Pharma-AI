# 1药网 AI 智能组货与动态定价引擎（本地可用版）

这是一个可直接演示、可灰度接入、可二次开发的本地 MVP，面向结算场景提供智能组货与换购定价能力。

核心能力：

- 结算侧推荐 API（输入主品 + 候选池，输出推荐 SKU、医嘱文案、换购价、预计毛利）
- 高并发兜底（超时降级，不阻断交易主链路）
- 高频类目缓存（24h，降低模型调用成本）
- 运营后台（策略、商品池、策略版本、AB 实验、指标看板、在线 Demo）
- 事件回传（曝光/点击/加购下单）与 AB 报表
- AI 接入（百炼优先，规则兜底）

---

## 1. 快速开始

### 1.1 环境要求

- Python 3.10+
- macOS / Linux（Windows 可用 WSL）
- 可选：阿里云 DashScope API Key（启用 AI 推荐时需要）

### 1.2 启动服务

```bash
cd /Users/weipeng/zhinengzuhuo-backup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8088
```

访问地址：

- 后台页面：`http://127.0.0.1:8088/admin`
- 健康检查：`http://127.0.0.1:8088/health`
- OpenAPI 文档：`http://127.0.0.1:8088/docs`

---

## 2. 项目目录结构

```text
zhinengzuhuo-backup/
├── app/
│   ├── main.py                  # API 入口、路由、缓存、AB 分流
│   ├── bundle_engine.py         # 规则引擎（过滤/召回/评分/定价/文案）
│   ├── ai_brain.py              # LLM 调用封装（百炼）
│   └── templates/admin.html     # 运营后台页面
├── scripts/
│   └── import_sales_catalog.py  # 商品清单导入脚本
├── requirements.txt
└── README.md
```

---

## 3. 与 1药网现有系统对接路径

1. C 端进入结算页，原有结算 API 保持不变。
2. 结算服务并行异步调用本服务 `POST /api/recommend`。
3. 本服务命中缓存则直接返回；未命中则进行实时推荐。
4. 前端渲染推荐卡片，用户勾选后按换购价加购 SKU。
5. 超时或异常时返回 `fallback=skip_module`，前端直接隐藏推荐卡片，不影响主流程。

---

## 4. 推荐 API（核心）

`POST /api/recommend`

请求体（示例）：

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

返回体（示例）：

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

推荐响应关键字段：

- `selected_sku_id`：命中的副品 SKU
- `medical_logic`：医学推荐逻辑标签（用于合规解释）
- `sales_copy`：前台展示文案
- `pricing_strategy.addon_price`：最终换购价
- `projected_profit`：预计毛利（用于运营评估）

---

## 5. 智能组货引擎设计

核心逻辑在 `app/bundle_engine.py`，采用分层处理：

- 拦截层：医学安全过滤（不符合主品医学逻辑的副品直接剔除）
- 召回层：优先使用入参候选池，兜底从商品池召回
- 评分层：`医学匹配 + 毛利贡献 + 可负担性` 综合评分
- 定价层：`max(成本底价, 锚定价)` 生成换购价
- 文案层：医嘱式文案 + 禁用词过滤

`app/main.py` 主要负责接口编排、缓存、日志与 AB 分流，后续扩品类可通过配置与数据驱动，不必大改流程代码。

---

## 6. 运营后台能力（当前版本）

- 类目策略：类目、医学逻辑、医嘱提示、毛利率、启停状态
- 商品池：主品与副品统一维护（SKU、成本、原价、毛利率）
- 策略版本：草稿/发布管理，支持快速回滚
- AB 实验：A/B 流量比例配置，输出 Variant 级报表
- 指标总览：请求量、成功率、缓存命中率、CTR、CVR、加购销售额、加购毛利
- 在线 Demo：降糖药/高血压药一键触发 + 漏斗事件模拟

---

## 7. API 一览

### 7.1 推荐与事件

- `POST /api/recommend`：核心推荐接口
- `POST /api/events`：上报曝光/点击/加购事件

### 7.2 后台管理

- `GET /api/admin/metrics`：总览指标
- `GET/POST /api/admin/policies`：类目策略管理
- `GET/POST /api/admin/products`：商品池管理
- `GET/POST /api/admin/strategies`：策略版本管理
- `POST /api/admin/strategies/{id}/publish`：发布版本
- `GET/POST /api/admin/experiments`：AB 实验管理
- `GET /api/admin/ab-report`：AB 报表
- `GET /api/admin/ai-status`：AI 开关和模型状态

### 7.3 运营快捷工作台

- `POST /api/ops/upload-catalog`：上传清单，返回 `batch_id`
- `POST /api/ops/generate-strategies?batch_id=...`：自动生成策略
- `GET /api/ops/strategies?batch_id=...`：查看策略列表
- `POST /api/ops/strategies/{id}/confirm`：确认单条策略
- `POST /api/ops/sync?batch_id=...`：同步已确认策略到 `bundle_rules`
- `GET /api/ops/workbench?batch_id=...`：查看批次进度

---

## 8. 订单清单导入（昨天售卖商品）

导入脚本：`scripts/import_sales_catalog.py`

示例：

```bash
cd /Users/weipeng/zhinengzuhuo-backup
source .venv/bin/activate
python scripts/import_sales_catalog.py \
  --excel "/Users/weipeng/Desktop/你的订单清单.xlsx" \
  --default-role main
```

说明：

- 自动识别常见中文表头（SKU、商品名称、类目、价格、成本、销量）
- 如缺少成本，按 `价格 * default-cost-rate` 估算（默认 0.78）
- 导入目标为 `products` 表，按 SKU upsert（存在即更新）

---

## 9. 接入百炼大模型（AI优先，规则兜底）

推荐链路：

1. 优先调用百炼模型做选品与文案
2. 本地引擎负责价格与毛利约束
3. 模型异常/超时时自动回退规则引擎

环境变量（示例）：

```bash
export ENABLE_AI_BRAIN=true
export BAILIAN_API_KEY="你的百炼API_KEY"
export BAILIAN_MODEL="qwen-plus"
export BAILIAN_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export BAILIAN_TIMEOUT_SEC=1.2
```

建议：

- 线上环境请使用密钥管理服务，不要明文写入仓库
- 建议设置超时 `1.0 ~ 1.5s`，超时后快速回退规则兜底
- 发生 key 泄露时务必立即旋转并废弃旧 key

---

## 10. 端到端演示流程（给运营/业务看）

1. 启动服务并打开后台 `http://127.0.0.1:8088/admin`
2. 在“商品池”导入或维护主品/副品
3. 在“类目策略”配置医学逻辑与毛利目标
4. 在“运营快捷工作台”上传 Excel 并生成策略草稿
5. 逐条确认策略后执行一键同步
6. 通过 Demo 触发推荐并观察漏斗数据变化
7. 检查 AB 报表，对比 Variant 表现

---

## 11. 常见问题排查

- **服务启动失败**
  - 检查 Python 版本与虚拟环境是否激活
  - 重新执行 `pip install -r requirements.txt`
- **推荐结果为空**
  - 检查 `main_item.category` 与策略类目是否匹配
  - 检查候选池是否有可用副品、成本/原价字段是否完整
- **AI 不生效**
  - 检查 `ENABLE_AI_BRAIN=true`
  - 调用 `GET /api/admin/ai-status` 检查模型连通
- **推荐变慢**
  - 检查外部模型超时设置和网络波动
  - 观察缓存命中率与降级比例

---

## 12. 生产化建议（下一步）

- 接入企业内部 LLM 网关并做统一可观测（延迟、成功率、Token 成本）
- 增加人工审核流：新策略先灰度，观察指标后再全量
- 增加 AB 维度：人群、类目、文案、价格多维实验
- 打通订单回传，计算真实 CVR、增量 GMV、增量毛利
- 建立安全治理：密钥轮换、日志脱敏、权限分层

---

## 13. GitHub 备份仓库

当前项目已备份到：

- [https://github.com/weic23614-coder/zhinengzuhuo](https://github.com/weic23614-coder/zhinengzuhuo)

建议后续流程：

1. 每次需求迭代先新建分支
2. 功能完成后提 PR 自检
3. 合并到 `main` 前跑一遍接口回归与 Demo 冒烟

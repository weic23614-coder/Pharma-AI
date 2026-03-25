# API 调用示例（API_EXAMPLES）

本文档提供可直接复制执行的接口样例，默认服务地址：`http://127.0.0.1:8089`。

---

## 1. 健康检查

```bash
curl -s http://127.0.0.1:8089/health
```

---

## 2. 核心推荐接口

### 2.1 请求示例

```bash
curl -s -X POST "http://127.0.0.1:8089/api/recommend" \
  -H "Content-Type: application/json" \
  -d '{
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
      },
      {
        "sku_id": "B902",
        "product_name": "电子体温计",
        "cost": 12,
        "original_price": 39
      }
    ]
  }'
```

### 2.2 返回示例

```json
{
  "recommendation": {
    "selected_sku_id": "B901",
    "medical_logic": "慢病管理",
    "sales_copy": "【药师建议】慢病管理建议搭配家庭监测设备，便于日常记录。",
    "pricing_strategy": {
      "addon_price": 108.78,
      "display_tag": "加109元换购价"
    },
    "projected_profit": 20.78
  }
}
```

---

## 3. 事件上报接口

### 3.1 曝光事件

```bash
curl -s -X POST "http://127.0.0.1:8089/api/events" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "exposure",
    "sku_id": "B901",
    "user_id": "u_10001",
    "session_id": "s_abc_001",
    "variant": "A",
    "ts": 1710000000
  }'
```

### 3.2 点击事件

```bash
curl -s -X POST "http://127.0.0.1:8089/api/events" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "click",
    "sku_id": "B901",
    "user_id": "u_10001",
    "session_id": "s_abc_001",
    "variant": "A",
    "ts": 1710000010
  }'
```

### 3.3 加购事件

```bash
curl -s -X POST "http://127.0.0.1:8089/api/events" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "add_to_cart",
    "sku_id": "B901",
    "user_id": "u_10001",
    "session_id": "s_abc_001",
    "variant": "A",
    "ts": 1710000020
  }'
```

---

## 4. 后台指标与报表

### 4.1 总览指标

```bash
curl -s "http://127.0.0.1:8089/api/admin/metrics"
```

### 4.2 AB 报表

```bash
curl -s "http://127.0.0.1:8089/api/admin/ab-report"
```

---

## 5. 策略与商品池管理

> 下述字段为通用演示格式，若你的本地模型定义略有差异，以 `/docs` 为准。

### 5.1 新增类目策略

```bash
curl -s -X POST "http://127.0.0.1:8089/api/admin/policies" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "高血压药",
    "medical_logic": "慢病管理",
    "advice": "建议搭配家庭监测设备",
    "target_margin": 0.2,
    "enabled": true
  }'
```

### 5.2 新增商品

```bash
curl -s -X POST "http://127.0.0.1:8089/api/admin/products" \
  -H "Content-Type: application/json" \
  -d '{
    "sku_id": "B901",
    "product_name": "上臂式电子血压计",
    "category": "医疗器械",
    "cost": 88,
    "original_price": 259,
    "role": "addon",
    "enabled": true
  }'
```

---

## 6. 运营快捷工作台接口

### 6.1 上传商品清单

```bash
curl -s -X POST "http://127.0.0.1:8089/api/ops/upload-catalog" \
  -F "file=@/Users/weipeng/Desktop/你的订单清单.xlsx"
```

### 6.2 根据 batch 生成策略

```bash
curl -s -X POST "http://127.0.0.1:8089/api/ops/generate-strategies?batch_id=your_batch_id"
```

### 6.3 查看策略列表

```bash
curl -s "http://127.0.0.1:8089/api/ops/strategies?batch_id=your_batch_id"
```

### 6.4 确认单条策略

```bash
curl -s -X POST "http://127.0.0.1:8089/api/ops/strategies/your_strategy_id/confirm"
```

### 6.5 同步已确认策略

```bash
curl -s -X POST "http://127.0.0.1:8089/api/ops/sync?batch_id=your_batch_id"
```

### 6.6 查询批次进度

```bash
curl -s "http://127.0.0.1:8089/api/ops/workbench?batch_id=your_batch_id"
```

---

## 7. AI 状态检查

```bash
curl -s "http://127.0.0.1:8089/api/admin/ai-status"
```

如果返回 AI 未启用，请检查：

- `ENABLE_AI_BRAIN` 是否为 `true`
- `BAILIAN_API_KEY` 是否有效
- `BAILIAN_BASE_URL` 与网络是否可达

---

## 8. Python 调用示例

```python
import requests

url = "http://127.0.0.1:8089/api/recommend"
payload = {
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

resp = requests.post(url, json=payload, timeout=1.5)
print(resp.status_code)
print(resp.json())
```


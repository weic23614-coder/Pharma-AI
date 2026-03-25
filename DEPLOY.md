# 部署手册（DEPLOY）

本文档用于把 `zhinengzuhuo-backup` 从本地开发环境部署到可稳定运行的单机环境（Linux/macOS）。

---

## 1. 部署目标

- 对外提供推荐接口：`POST /api/recommend`
- 提供运营后台：`/admin`
- 保证主链路可用：AI 异常时规则兜底
- 支持基础可观测：健康检查、日志、关键指标

---

## 2. 环境要求

- Python 3.10+
- 2C4G 起步（建议）
- 操作系统：Ubuntu 20.04+ / macOS
- 可选：Nginx（反向代理）、systemd（守护进程）

---

## 3. 代码准备

```bash
cd /opt
git clone https://github.com/weic23614-coder/zhinengzuhuo.git
cd zhinengzuhuo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. 环境变量

建议通过 `.env` 或系统环境变量管理，不要把密钥提交到仓库。

```bash
# 基础
export HOST=0.0.0.0
export PORT=8089

# AI（可选）
export ENABLE_AI_BRAIN=true
export BAILIAN_API_KEY="替换为你的key"
export BAILIAN_MODEL="qwen-plus"
export BAILIAN_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export BAILIAN_TIMEOUT_SEC=1.2
```

---

## 5. 启动方式

### 5.1 开发/测试启动

```bash
cd /opt/zhinengzuhuo
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8089 --reload
```

### 5.2 生产启动（推荐）

```bash
cd /opt/zhinengzuhuo
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8089
```

---

## 6. systemd 守护（Linux）

文件：`/etc/systemd/system/zhinengzuhuo.service`

```ini
[Unit]
Description=Zhinengzuhuo FastAPI Service
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/zhinengzuhuo
Environment="HOST=0.0.0.0"
Environment="PORT=8089"
Environment="ENABLE_AI_BRAIN=true"
Environment="BAILIAN_MODEL=qwen-plus"
Environment="BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1"
Environment="BAILIAN_TIMEOUT_SEC=1.2"
Environment="BAILIAN_API_KEY=替换为你的key"
ExecStart=/opt/zhinengzuhuo/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8089
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启停命令：

```bash
sudo systemctl daemon-reload
sudo systemctl enable zhinengzuhuo
sudo systemctl start zhinengzuhuo
sudo systemctl status zhinengzuhuo
```

---

## 7. Nginx 反向代理（可选）

示例配置：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8089;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## 8. 验收清单

部署后至少验证以下接口：

```bash
curl -s http://127.0.0.1:8089/health
curl -s http://127.0.0.1:8089/api/admin/ai-status
curl -s http://127.0.0.1:8089/api/admin/metrics
```

浏览器验证：

- `/admin` 可访问并正常渲染
- Demo 可触发推荐
- 上报事件后指标有变化

---

## 9. 回滚策略

- 代码回滚：`git checkout <last-stable-tag-or-commit>`
- 配置回滚：恢复上个环境变量快照
- 运行回滚：`systemctl restart zhinengzuhuo`

建议每次上线前打 Tag，便于快速回滚。

---

## 10. 安全与运维建议

- 立即旋转任何暴露过的 API Key
- 把密钥迁移到 Secret Manager（如阿里云 KMS、Vault）
- 生产日志避免打印明文用户信息和密钥
- 对外入口加限流和 WAF
- 每日备份业务数据库（如 `app.db`）并保留最近 7~30 天快照

---

## 11. 公网开放（非本机、所有人可访问）

### 11.1 与「本地版」的本质区别

| 本地 | 公网 |
|------|------|
| `--host 127.0.0.1` 仅本机 | `--host 0.0.0.0` 监听所有网卡 |
| 不暴露端口 | 云安全组/防火墙放行 **80/443**（或你自定义端口） |
| 无域名 | 建议 **域名 + HTTPS**（Let’s Encrypt / 云厂商证书） |

启动示例（单机云服务器上）：

```bash
export APP_DB_PATH="/var/lib/zhinengzuhuo/app.db"   # 固定路径，避免重启丢数据
uvicorn app.main:app --host 0.0.0.0 --port 8089
```

前面仍可挂 **Nginx** 反代到 `127.0.0.1:8089`，对外只开 443。

### 11.2 推荐架构（MVP → 小规模真实使用）

1. **一台云主机**（阿里云 ECS / 腾讯云 CVM / AWS EC2 等，2C4G 起）。  
2. **Ubuntu + systemd** 守护 uvicorn（见上文 §6）。  
3. **Nginx** 反向代理 + **HTTPS**（见 §7，证书用 certbot 或云证书）。  
4. **SQLite** 数据库文件放在**持久盘**（如云盘挂载目录），并配置 `APP_DB_PATH`，避免容器/PaaS 无持久卷时数据丢失。

### 11.3 ⚠️ 当前代码的重要安全事实

- **`/admin` 与 `/api/admin/*`、`/api/ops/*` 目前没有登录鉴权**。  
- 一旦公网开放，**任何人**可改策略、上传库存、看到你的百炼 Key（若写在页面里）。  

**上线前至少选一种：**

- **Nginx Basic Auth**（用户名密码保护整个 `/admin` 与敏感 API）；或  
- **IP 白名单**（仅公司出口 IP 可访问后台）；或  
- 在应用内加 **登录 / Token**（需开发，适合长期开放）。

对外若只想开放 **`POST /api/recommend`** 等只读/业务接口，可用 Nginx **按路径**限制：`/admin` 仅内网。

### 11.4 使用 PaaS（Railway / Render / Fly.io 等）

- 多数平台**文件系统重启会清空**，SQLite 需绑定 **持久 Volume**，否则数据丢失。  
- 并发高时建议后续迁 **PostgreSQL**（本仓库当前为 SQLite，迁库需改 `db_conn` 与 SQL，属下一阶段）。

### 11.5 验收（公网）

```bash
curl -s https://你的域名/health
```

浏览器打开：`https://你的域名/admin`（若已加认证，会先弹登录）。

---

## 12. 「开放给大家用」的两种含义

| 目标 | 做法 |
|------|------|
| **演示 / 内测** | 云主机 + HTTPS + **后台必须加密码或白名单**，只把链接发给信任的人。 |
| **正式产品** | 同上 + **登录体系**、审计日志、独立数据库备份、限流与监控；API Key 只放服务端环境变量，永不进前端。 |


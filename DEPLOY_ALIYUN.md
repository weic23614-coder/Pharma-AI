# 阿里云 ECS 部署清单（智能组货 MVP）

面向：把 `zhinengzuhuo-backup` 部署到阿里云，让外网通过 **域名 + HTTPS** 访问（或先用 IP 测试）。

通用说明见 [DEPLOY.md](./DEPLOY.md)；本文只补充 **阿里云控制台与推荐命令顺序**。

---

## 一、买哪类资源

1. **云服务器 ECS**（不用轻量也可以，轻量应用服务器对新手更省事，步骤类似）。
2. **地域**：选离用户近的（如华东、华南）。
3. **镜像**：**Ubuntu 22.04 64 位**（与下文命令一致）。
4. **规格**：2 核 4 GB 内存起步即可 MVP。
5. **公网 IP**：勾选分配公网 IPv4（按流量或固定带宽均可，演示阶段 1～5 Mbps 够用）。
6. **系统盘**：40 GB 起，**数据盘可选**：若 SQLite 要单独盘，可挂载后把 `APP_DB_PATH` 指到挂载点（如 `/data/zhinengzuhuo/app.db`）。

---

## 二、安全组（必做）

在 ECS 控制台 → 本实例 → **安全组** → 入方向规则，至少：

| 端口 | 用途 | 来源建议 |
|------|------|----------|
| **22** | SSH 维护 | 仅你的办公 / 家庭公网 IP（最安全） |
| **80** | HTTP（证书验证、跳转 HTTPS） | `0.0.0.0/0` 或按需收紧 |
| **443** | HTTPS 对外服务 | `0.0.0.0/0` |

**不要**把 **8088** 对全网开放（若用 Nginx 反代，只本机访问 8088 即可）。

---

## 三、域名与备案（中国大陆用户访问）

- 若使用 **国内 ECS + 国内域名** 在 **80/443** 提供网站服务，通常需要 **ICP 备案**（在阿里云备案系统提交，按流程约 1～2 周量级）。
- **仅 SSH 22 调试、或先不绑域名** 可先跳过备案，用 **公网 IP + 非 80 端口** 做内测（不推荐长期对外营业）。
- **香港地域 ECS** 或 **境外服务器** 可绕开国内备案，但合规与访问速度需自行评估。

---

## 四、登录服务器并安装环境

用你的 SSH 客户端连接（控制台可下载密钥或设密码）：

```bash
ssh root@你的ECS公网IP
```

### 4.1 更新系统并安装依赖

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git nginx
```

### 4.2 部署目录与代码

```bash
mkdir -p /opt
cd /opt
# 若代码在 GitHub（替换为你的仓库地址与分支）
git clone https://github.com/weic23614-coder/zhinengzuhuo.git
cd zhinengzuhuo

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

> 若仓库名仍是 `zhinengzuhuo`，路径为 `/opt/zhinengzuhuo`；若你只在本地有 `zhinengzuhuo-backup`，需先 **push 到 Git** 或用 `scp` 上传整目录到 `/opt/zhinengzuhuo`。

### 4.3 数据库目录（持久化）

```bash
mkdir -p /var/lib/zhinengzuhuo
chown -R www-data:www-data /var/lib/zhinengzuhuo
```

首次启动后 SQLite 会生成在 `APP_DB_PATH` 指定路径。

---

## 五、环境变量（不要写进 Git）

创建 `/etc/zhinengzuhuo.env`（权限收紧：`chmod 600`），示例：

```bash
APP_DB_PATH=/var/lib/zhinengzuhuo/app.db
HOST=0.0.0.0
PORT=8088
# 可选：服务端百炼（也可只在后台页面配置，二选一即可）
# ENABLE_AI_BRAIN=true
# BAILIAN_API_KEY=你的key
# BAILIAN_MODEL=qwen-plus
# BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# BAILIAN_TIMEOUT_SEC=30
```

---

## 六、systemd 常驻进程

创建 `/etc/systemd/system/zhinengzuhuo.service`：

```ini
[Unit]
Description=Zhinengzuhuo FastAPI
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/zhinengzuhuo
EnvironmentFile=/etc/zhinengzuhuo.env
ExecStart=/opt/zhinengzuhuo/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8088
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
chown -R www-data:www-data /opt/zhinengzuhuo
systemctl daemon-reload
systemctl enable zhinengzuhuo
systemctl start zhinengzuhuo
systemctl status zhinengzuhuo
```

本机验收：

```bash
curl -s http://127.0.0.1:8088/health
```

---

## 七、Nginx 反向代理 + HTTPS

### 7.1 先把域名 A 记录指到 ECS 公网 IP

在阿里云 **云解析 DNS** 里配置。

### 7.2 HTTP 站点（临时，用于申请证书）

`/etc/nginx/sites-available/zhinengzuhuo`：

```nginx
server {
    listen 80;
    server_name 你的域名.com;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
        proxy_connect_timeout 60s;
        client_max_body_size 50m;
    }
}
```

启用：

```bash
ln -sf /etc/nginx/sites-available/zhinengzuhuo /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

### 7.3 申请 Let’s Encrypt 证书（Certbot）

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d 你的域名.com
```

按提示完成；certbot 会改 Nginx 为 **443 + 自动续期**。

---

## 八、安全（上线前必看）

当前 **`/admin` 无登录**。公网开放后务必至少做一项：

1. **Nginx Basic Auth**（最快）：对 `location /admin` 与敏感 `location /api/admin`、`/api/ops` 加 `auth_basic`；或  
2. **安全组限制**：仅公司出口 IP 访问 443；或  
3. 后续在应用内加 **Token / 登录**。

密钥：**只放在服务器环境变量**，不要提交到 Git；轮换已泄露的 Key。

---

## 九、阿里云上可再考虑的增强

- **云备份**：对系统盘或数据盘做自动快照（控制台「快照策略」）。  
- **WAF / SLB**：流量大或要防爬时再上。  
- **日志**：`journalctl -u zhinengzuhuo -f` 看应用日志；Nginx access/error 在 `/var/log/nginx/`。

---

## 十、验收清单

```bash
curl -s https://你的域名.com/health
```

浏览器打开：`https://你的域名.com/admin`（若已加 Basic Auth 会先弹账号密码）。

---

## 十一、常见问题

| 现象 | 处理 |
|------|------|
| 外网打不开 | 查安全组是否放行 80/443；`systemctl status zhinengzuhuo`、`nginx -t` |
| 502 Bad Gateway | uvicorn 未启动或端口不是 8088 |
| 上传 Excel 失败 | `client_max_body_size` 调大（上文已示例 50m） |
| 数据库丢了 | 是否把 `APP_DB_PATH` 指到临时目录；应固定到 `/var/lib/zhinengzuhuo/` 并做快照 |

---

更通用的 systemd / 环境变量说明见 **[DEPLOY.md](./DEPLOY.md)**。

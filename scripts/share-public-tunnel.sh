#!/usr/bin/env bash
# 生成本机服务的临时公网 HTTPS 链接（Quick Tunnel），便于把 /admin 发给同事试用。
# 依赖：Cloudflare 官方客户端 cloudflared（免费，无需把任何密钥发给第三方 AI）。
#
# 安装（macOS）：brew install cloudflared
#
# 使用前请先在本机启动服务，例如：
#   ./scripts/start-dev.sh
set -euo pipefail
PORT="${PORT:-8089}"
LOCAL="http://127.0.0.1:${PORT}"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "未找到 cloudflared。"
  echo "macOS 可执行: brew install cloudflared"
  echo "其它系统见: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  exit 1
fi

echo "────────────────────────────────────────"
echo "将本机 ${LOCAL} 暴露为临时公网地址（关闭本终端即失效）。"
echo "把打印出的 https://....trycloudflare.com 发给对方，路径加上 /admin 即可打开后台。"
echo "注意：任何拿到链接的人都能访问你电脑上的该服务，仅用于演示，用完请 Ctrl+C 结束。"
echo "────────────────────────────────────────"
exec cloudflared tunnel --url "$LOCAL"

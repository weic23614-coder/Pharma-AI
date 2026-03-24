#!/usr/bin/env bash
# 同一局域网内其他设备可访问：监听所有网卡（0.0.0.0），默认端口 8089
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
PORT="${PORT:-8089}"
# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
if [[ -z "${LAN_IP}" ]]; then
  LAN_IP="$(ipconfig getifaddr en1 2>/dev/null || true)"
fi
if [[ -z "${LAN_IP}" ]]; then
  LAN_IP="（请在本机终端执行 ifconfig 或「系统设置 → 网络」查看 IPv4）"
fi

echo "────────────────────────────────────────"
echo "本机访问:    http://127.0.0.1:${PORT}/admin"
echo "同事访问:    http://${LAN_IP}:${PORT}/admin"
echo "（需同一 Wi‑Fi/有线网段；若 IP 不对，换网线接口或查 VPN）"
echo "────────────────────────────────────────"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload

#!/usr/bin/env bash
# 安装「登录后自动启动」到本机用户 LaunchAgents，监听 127.0.0.1:8089（无 --reload，适合常驻）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UVICORN="$REPO_ROOT/.venv/bin/uvicorn"
if [[ ! -x "$UVICORN" ]]; then
  echo "未找到可执行文件: $UVICORN"
  echo "请先在项目根目录执行: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

LABEL="com.local.zhinengzuhuo.dev"
PLIST_NAME="${LABEL}.plist"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS}/${PLIST_NAME}"

mkdir -p "$REPO_ROOT/.logs"
mkdir -p "$LAUNCH_AGENTS"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>WorkingDirectory</key>
    <string>${REPO_ROOT}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${UVICORN}</string>
        <string>app.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8089</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${REPO_ROOT}/.logs/uvicorn-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${REPO_ROOT}/.logs/uvicorn-stderr.log</string>
</dict>
</plist>
EOF

UIDSTR="$(id -u)"
launchctl bootout "gui/${UIDSTR}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/${UIDSTR}" "$PLIST_PATH"

echo "已安装登录自启动: ${LABEL}"
echo "访问: http://127.0.0.1:8089/admin"
echo "日志: ${REPO_ROOT}/.logs/uvicorn-stdout.log"
echo "卸载请运行: scripts/uninstall-login-startup.sh"

#!/bin/zsh
# Codex 轨迹查看器 —— 双击运行（macOS 会在终端里打开）
# 使用 CODEX_HOME 或当前用户默认 Codex 数据目录，服务只监听 127.0.0.1。

cd "$(dirname "$0")" || exit 1

PORT="${1:-8765}"

PY="$(command -v python3)"
if [ -z "$PY" ]; then
  echo "找不到 python3，请先安装 Python 3。"
  read -r "?按回车关闭…"
  exit 1
fi

echo "正在启动 Codex 轨迹查看器…"
echo "（关闭本窗口或按 Ctrl+C 即停止服务）"
echo

exec "$PY" server.py --port "$PORT" --open-browser

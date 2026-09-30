#!/usr/bin/env bash
# 一条本地命令：重置虚构演示数据（含拒领、缺页、止付样例）并启动系统。
# 用法：./run.sh            （默认端口 8000）
#       PORT=8080 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
DB="${DB:-data/checkbook.db}"
VOUCHERS="${VOUCHERS:-data/vouchers}"
PORT="${PORT:-8000}"

echo "==> 准备虚构演示数据（拒领 / 缺页 / 止付 / 待销毁 等样例）"
"$PYTHON" -m ccb_checkbook.seed --db "$DB" --vouchers "$VOUCHERS"

echo "==> 启动支票簿核验系统：http://127.0.0.1:${PORT}/"
exec "$PYTHON" -m ccb_checkbook.webapp \
  --db "$DB" --vouchers "$VOUCHERS" --port "$PORT" --scan-interval 30

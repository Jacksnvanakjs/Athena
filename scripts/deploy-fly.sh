#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

FLY="/opt/homebrew/bin/fly"
SCRAPE_SECRET="${SCRAPE_SECRET:-$(openssl rand -hex 16)}"

echo "==> 检查 Fly.io 登录状态..."
$FLY auth whoami

echo "==> 创建应用（如已存在会跳过）..."
$FLY launch --no-deploy --yes --copy-config 2>/dev/null || true

echo "==> 创建持久化存储卷..."
$FLY volumes list -a athena-fund 2>/dev/null | grep -q athena_data || \
  $FLY volumes create athena_data --region sin --size 1 -a athena-fund

echo ""
echo "请设置 Server酱 SendKey（推送通知必需）："
read -r -p "SERVERCHAN_SENDKEY: " SENDKEY

echo "==> 配置环境变量..."
$FLY secrets set \
  SERVERCHAN_SENDKEY="$SENDKEY" \
  SCRAPE_SECRET="$SCRAPE_SECRET" \
  TIMEZONE="Asia/Shanghai" \
  ENABLE_SCHEDULER="true" \
  DATABASE_URL="sqlite:////app/data/funds.db" \
  -a athena-fund

echo "==> 部署中..."
$FLY deploy -a athena-fund

echo ""
echo "=========================================="
echo "部署完成！"
echo "访问地址: https://athena-fund.fly.dev"
echo "SCRAPE_SECRET: $SCRAPE_SECRET"
echo "=========================================="

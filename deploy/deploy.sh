#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  520量化系统 · 增量部署脚本
#  用法：bash deploy/deploy.sh [选项]
#
#  选项：
#    --code-only     只同步代码，不重启服务
#    --restart-only  只重启服务，不同步代码
#    --web-only      只重启 Web 服务
#    --monitor-only  只重启监控服务
#
#  首次部署请先在服务器执行：bash deploy/setup.sh
# ─────────────────────────────────────────────────────────────

set -euo pipefail

# ── 配置（按实际情况修改） ──────────────────────────────────
SERVER="root@120.26.180.6"
REMOTE_DIR="/opt/520quant"
SSH_KEY="$HOME/.ssh/id_520quant"   # SSH 密钥路径，免密部署；留空则改用密码
SSH_PORT="22"
# ───────────────────────────────────────────────────────────

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 颜色输出 ──────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $*${NC}"; }
info() { echo -e "${YELLOW}▶  $*${NC}"; }
err()  { echo -e "${RED}❌ $*${NC}"; exit 1; }

# ── SSH / rsync 公共参数 ───────────────────────────────────
SSH_OPTS="-p $SSH_PORT -o StrictHostKeyChecking=no -o ConnectTimeout=10"
[ -n "$SSH_KEY" ] && SSH_OPTS="$SSH_OPTS -i $SSH_KEY"

# ── 参数解析 ──────────────────────────────────────────────
DO_SYNC=true
DO_RESTART=true
RESTART_WEB=true
RESTART_MONITOR=true

for arg in "$@"; do
  case $arg in
    --code-only)     DO_RESTART=false ;;
    --restart-only)  DO_SYNC=false ;;
    --web-only)      DO_SYNC=false; RESTART_MONITOR=false ;;
    --monitor-only)  DO_SYNC=false; RESTART_WEB=false ;;
  esac
done

echo ""
echo "════════════════════════════════════════════════"
echo "  520量化系统  增量部署"
echo "  目标：$SERVER:$REMOTE_DIR"
echo "════════════════════════════════════════════════"
echo ""

# ── 1. 连通性检测 ─────────────────────────────────────────
info "检测服务器连通性..."
if ! ssh $SSH_OPTS "$SERVER" "echo ok" &>/dev/null; then
  echo ""
  echo "  SSH 连接失败。常见解决方法："
  echo ""
  echo "  方法 A（密码登录）："
  echo "    在脚本顶部设置 SSH_KEY=\"\" 即可，rsync 会提示输入密码"
  echo ""
  echo "  方法 B（免密钥，一次配置永久免密）："
  echo "    ssh-keygen -t ed25519 -f ~/.ssh/id_520quant   # 生成密钥"
  echo "    ssh-copy-id -p $SSH_PORT -i ~/.ssh/id_520quant $SERVER   # 上传"
  echo "    # 然后将脚本顶部 SSH_KEY 改为 ~/.ssh/id_520quant"
  echo ""
  err "请配置好 SSH 后重试"
fi
ok "服务器连通"

# ── 2. 同步代码 ──────────────────────────────────────────
if $DO_SYNC; then
  info "同步代码到服务器..."
  rsync -avz --progress \
    -e "ssh $SSH_OPTS" \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.DS_Store' \
    --exclude='venv/' \
    --exclude='.env' \
    --exclude='*.log' \
    "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"
  ok "代码同步完成"
else
  info "跳过代码同步 (--restart-only)"
fi

# ── 3. 服务器端操作 ──────────────────────────────────────
if $DO_RESTART; then
  info "在服务器执行重启..."
  ssh $SSH_OPTS "$SERVER" bash <<REMOTE
set -e
cd $REMOTE_DIR

echo "── 安装/更新 Python 依赖..."
$REMOTE_DIR/venv/bin/pip install -q --upgrade -r $REMOTE_DIR/requirements.txt
echo "   依赖检查完成"

$(if $RESTART_MONITOR; then echo "
echo '── 重启监控服务...'
systemctl restart 520quant-monitor
sleep 2
systemctl is-active --quiet 520quant-monitor && echo '   520quant-monitor: running ✅' || echo '   ⚠️  520quant-monitor 启动异常，请检查日志'
"; fi)

$(if $RESTART_WEB; then echo "
echo '── 重启 Web 服务...'
systemctl restart 520quant-web
sleep 2
systemctl is-active --quiet 520quant-web && echo '   520quant-web: running ✅' || echo '   ⚠️  520quant-web 启动异常，请检查日志'
"; fi)

echo ""
echo "── 当前服务状态"
systemctl status 520quant-monitor --no-pager -l | tail -4
systemctl status 520quant-web     --no-pager -l | tail -4
REMOTE

  ok "服务重启完成"
else
  info "跳过服务重启 (--code-only)"
fi

# ── 4. 完成 ──────────────────────────────────────────────
SERVER_IP=$(echo "$SERVER" | sed 's/.*@//')
echo ""
echo "════════════════════════════════════════════════"
ok "部署完成！"
echo ""
echo "  🌐 Web：  http://$SERVER_IP:5000"
echo ""
echo "  📋 查看日志："
echo "    ssh $SERVER 'journalctl -u 520quant-web -f'"
echo "    ssh $SERVER 'journalctl -u 520quant-monitor -f'"
echo "════════════════════════════════════════════════"
echo ""

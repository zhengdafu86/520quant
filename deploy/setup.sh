#!/bin/bash
# 520量化系统 - 阿里云 Ubuntu 一键部署脚本
# 使用方式：bash deploy/setup.sh

set -e
APP_DIR="/opt/520quant"
VENV_DIR="$APP_DIR/venv"
USER=$(whoami)

echo "================================================"
echo "  520量化系统 部署脚本"
echo "================================================"

# ── 1. 系统依赖 ──────────────────────────────────
echo "[1/5] 安装系统依赖..."
sudo apt update -q
sudo apt install -y python3 python3-pip python3-venv git -q

# ── 2. 创建部署目录 ───────────────────────────────
echo "[2/5] 创建部署目录 $APP_DIR ..."
sudo mkdir -p "$APP_DIR"
sudo chown "$USER:$USER" "$APP_DIR"

# 把当前代码同步到部署目录（若已在 APP_DIR 内则跳过）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
    echo "     复制代码到 $APP_DIR ..."
    rsync -a --exclude='.git' --exclude='__pycache__' \
          --exclude='*.pyc' --exclude='venv' \
          "$SCRIPT_DIR/" "$APP_DIR/"
fi

# ── 3. Python 虚拟环境 + 依赖 ─────────────────────
echo "[3/5] 创建虚拟环境并安装依赖..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "     依赖安装完成 ✅"

# ── 4. 注册 systemd 服务 ─────────────────────────
echo "[4/5] 注册系统服务..."

# 替换服务文件中的路径和用户
sed "s|__APP_DIR__|$APP_DIR|g; s|__VENV_DIR__|$VENV_DIR|g; s|__USER__|$USER|g" \
    "$APP_DIR/deploy/520quant-monitor.service" \
    | sudo tee /etc/systemd/system/520quant-monitor.service > /dev/null

sed "s|__APP_DIR__|$APP_DIR|g; s|__VENV_DIR__|$VENV_DIR|g; s|__USER__|$USER|g" \
    "$APP_DIR/deploy/520quant-web.service" \
    | sudo tee /etc/systemd/system/520quant-web.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable 520quant-monitor 520quant-web
echo "     服务已注册 ✅"

# ── 5. 开放防火墙端口 ────────────────────────────
echo "[5/5] 配置防火墙..."
if command -v ufw &>/dev/null; then
    sudo ufw allow 5000/tcp
    echo "     ufw 已开放 5000 端口 ✅"
else
    echo "     (未检测到 ufw，请手动在阿里云安全组开放 TCP 5000)"
fi

# ── 完成 ─────────────────────────────────────────
SERVER_IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")
echo ""
echo "================================================"
echo "  部署完成！"
echo ""
echo "  启动服务："
echo "    sudo systemctl start 520quant-monitor"
echo "    sudo systemctl start 520quant-web"
echo ""
echo "  访问地址：http://$SERVER_IP:5000"
echo ""
echo "  查看日志："
echo "    journalctl -u 520quant-monitor -f"
echo "    journalctl -u 520quant-web -f"
echo "================================================"

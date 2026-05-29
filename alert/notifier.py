"""
推送层：终端打印 + 多渠道推送
支持：飞书机器人 / Server酱(微信) / 企业微信 / 钉钉
"""
from __future__ import annotations

import re
import requests
from datetime import datetime

# ── 推送渠道配置（填一个或多个）──────────────────────────
# 飞书机器人（推荐，卡片消息最好看）
# 群设置 → 机器人 → 添加自定义机器人 → 关键词填「520」
FEISHU_WEBHOOK = ""   # https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# Server酱 → 微信公众号「方糟」接收
# 注册：https://sct.ftqq.com  微信扫码拿 Key
SERVERCHAN_KEY = ""   # SCTxxxxxxxxxxxxxxxxxxxxxxxxxx

# 企业微信机器人
WECOM_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=bb481221-cce2-4f60-af22-eee51bae4d21"

# 钉钉机器人（安全设置选关键词「520」）
DINGTALK_WEBHOOK = ""


# ── 工具函数 ──────────────────────────────────────────────

ICONS = {"INFO": "ℹ️ ", "WARN": "⚠️ ", "BUY": "✅ ", "SELL": "🔴 ", "ERR": "❌ "}

def _now()  -> str: return datetime.now().strftime("%H:%M:%S")
def _date() -> str: return datetime.now().strftime("%Y-%m-%d")

def log(msg: str, level: str = "INFO"):
    print(f"[{_now()}] {ICONS.get(level,'')}{msg}")


# ── 飞书卡片推送 ──────────────────────────────────────────

# 卡片颜色映射
_FEISHU_COLOR = {
    "BUY":    "green",
    "SELL":   "red",
    "WARN":   "orange",
    "INFO":   "blue",
    "SUMMARY":"turquoise",
}

def _send_feishu(title: str, body: str, color: str = "blue"):
    """
    飞书卡片消息推送
    card 格式：标题栏（带颜色）+ Markdown 正文
    """
    if not FEISHU_WEBHOOK:
        return
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"【520量化】{title}"},
                "template": color,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body},
                },
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text",
                         "content": f"🕐 {_date()} {_now()}"}
                    ],
                },
            ],
        },
    }
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=8)
        data = resp.json()
        if data.get("StatusCode", data.get("code", -1)) not in (0, 200):
            log(f"飞书推送失败: {data}", "ERR")
        else:
            log(f"飞书已推送: {title}", "INFO")
    except Exception as e:
        log(f"飞书推送异常: {e}", "ERR")


# ── 其他渠道推送 ──────────────────────────────────────────

def _send_serverchan(title: str, body: str):
    if not SERVERCHAN_KEY:
        return
    try:
        url  = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
        resp = requests.post(url, data={"title": title, "desp": body}, timeout=8)
        if resp.json().get("code", -1) != 0:
            log(f"Server酱推送失败: {resp.json().get('message','')}", "ERR")
    except Exception as e:
        log(f"Server酱异常: {e}", "ERR")


def _send_wecom(title: str, body: str):
    if not WECOM_WEBHOOK:
        return
    try:
        content = f"### {title}\n{body}"
        requests.post(
            WECOM_WEBHOOK,
            json={"msgtype": "markdown", "markdown": {"content": content}},
            timeout=5,
        )
    except Exception as e:
        log(f"企业微信推送异常: {e}", "ERR")


def _send_dingtalk(title: str, body: str):
    if not DINGTALK_WEBHOOK:
        return
    text = re.sub(r"[#*>`\-]", "", body).strip()
    try:
        requests.post(
            DINGTALK_WEBHOOK,
            json={"msgtype": "text",
                  "text": {"content": f"【520量化】{title}\n{text}"},
                  "at": {"isAtAll": False}},
            timeout=5,
        )
    except Exception as e:
        log(f"钉钉推送异常: {e}", "ERR")


def _push(title: str, body: str, level: str = "INFO"):
    """统一推送：自动发送到所有已配置的渠道"""
    color = _FEISHU_COLOR.get(level, "blue")
    _send_feishu(title, body, color)
    _send_serverchan(title, body)
    _send_wecom(title, body)
    _send_dingtalk(title, body)

    if not any([FEISHU_WEBHOOK, SERVERCHAN_KEY, WECOM_WEBHOOK, DINGTALK_WEBHOOK]):
        log("未配置推送渠道（终端打印模式）", "WARN")


# ── 消息模板 ──────────────────────────────────────────────

def notify_buy(code: str, name: str, price: float, shares: int,
               reason: str, stop_price: float):
    total   = round(price * shares, 0)
    pct_to_stop = round((price - stop_price) / price * 100, 1)
    title   = f"✅ 买入信号  {name}（{code}）"
    body    = (
        f"**成交价：{price:.2f} 元**\n"
        f"数量：{shares} 股　金额：{total:.0f} 元\n\n"
        f"🛡️ 止损线：**{stop_price:.2f}**（下跌 {pct_to_stop}% 触发）\n"
        f"📌 {reason}"
    )
    log(f"买入 {name}({code}) {price:.2f}×{shares}股 止损={stop_price}", "BUY")
    _push(title, body, level="BUY")


def notify_sell(code: str, name: str, price: float, shares: int,
                cost: float, reason: str):
    pnl     = round((price - cost) * shares, 0)
    pnl_pct = round((price - cost) / cost * 100, 2)
    is_win  = pnl >= 0
    icon    = "🟢 止盈" if is_win else "🔴 止损"
    title   = f"{icon}  {name}（{code}）"
    body    = (
        f"**成交价：{price:.2f} 元**　成本：{cost:.2f}\n\n"
        f"📊 盈亏：**{pnl:+.0f} 元**（{pnl_pct:+.1f}%）\n"
        f"📌 {reason}"
    )
    log(f"卖出 {name}({code}) {price:.2f} 盈亏={pnl:+.0f}元({pnl_pct:+.1f}%)",
        "BUY" if is_win else "SELL")
    _push(title, body, level="BUY" if is_win else "SELL")


def notify_warning(code: str, name: str, price: float,
                   cost: float, warning: str):
    pnl_pct = round((price - cost) / cost * 100, 2)
    title   = f"⚠️ 风险预警  {name}（{code}）"
    body    = (
        f"当前价：**{price:.2f}**　浮动盈亏：{pnl_pct:+.1f}%\n\n"
        f"⚠️ {warning}"
    )
    log(f"预警 {name}({code}) {warning}", "WARN")
    _push(title, body, level="WARN")


def notify_stop_raised(code: str, name: str, price: float,
                       old_stop: float, new_stop: float,
                       gain_pct: float, milestone: str):
    """止损线上调通知（追踪止损触发关键档位）"""
    title = f"🔒 止损上调  {name}（{code}）"
    body  = (
        f"当前价：**{price:.2f}**　浮盈：**{gain_pct:+.1f}%**\n\n"
        f"止损线：~~{old_stop:.2f}~~ → **{new_stop:.2f}**\n"
        f"🎯 {milestone}"
    )
    log(f"止损上调 {name}({code}) {old_stop:.2f}→{new_stop:.2f} [{milestone}]", "INFO")
    _push(title, body, level="INFO")


def notify_daily_summary(positions: list[dict], signals: list[dict],
                         account: dict = None):
    """
    每日 15:30 收盘汇总推送
    positions: 持仓列表，含 code/name/cost/price/pnl/pnl_pct/stop_price
    signals:   候选股列表，含 code/name/price/signal
    account:   模拟账户概览（来自 PaperAccount.summary()），可为 None
    """
    # ── 账户总览 ──
    if account:
        ret    = account["total_return"]
        ret_icon = "🟢" if ret >= 0 else "🔴"
        acct_line = (
            f"总资产：**{account['total_assets']:,.0f} 元**　"
            f"{ret_icon} 累计收益：**{ret:+.2f}%**\n"
            f"持仓市值：{account['pos_value']:,.0f}　"
            f"现金：{account['cash']:,.0f}"
        )
    else:
        acct_line = "_（未启用模拟账户）_"

    # ── 持仓明细 ──
    if positions:
        pos_lines = "\n".join(
            f"{'🟢' if p['pnl_pct'] >= 0 else '🔴'} "
            f"**{p['name']}**({p['code']})　"
            f"现价 {p['price']:.2f}　"
            f"盈亏 **{p['pnl_pct']:+.1f}%**（{p['pnl']:+.0f}元）　"
            f"止损 {p['stop_price']:.2f}"
            for p in positions
        )
    else:
        pos_lines = "_暂无持仓_"

    # ── 候选股 ──
    if signals:
        sig_lines = "\n".join(
            f"⭕ **{s['name']}**({s['code']})　"
            f"现价 {s['price']:.2f}　{s['signal']}"
            for s in signals
        )
    else:
        sig_lines = "_暂无候选_"

    title = f"📊 收盘汇总  {_date()}"
    body  = (
        f"**【账户总览】**\n{acct_line}\n\n"
        f"**【持仓状态】**\n{pos_lines}\n\n"
        f"**【明日候选】**\n{sig_lines}"
    )
    log("发送每日收盘汇总", "INFO")
    _push(title, body, level="SUMMARY")


# ── 测试推送 ──────────────────────────────────────────────

def test_push():
    """运行后对应渠道收到消息即成功"""
    title = "🎉 系统连接成功"
    body  = (
        "✅ 推送渠道正常\n"
        "✅ 520量化监控就绪\n\n"
        "买入 / 止损 / 预警 / 收盘汇总\n"
        "将实时推送到此渠道"
    )
    _push(title, body, level="INFO")


if __name__ == "__main__":
    test_push()

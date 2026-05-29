"""
520量化 Web 后端
Flask API + 静态页面服务
"""
from __future__ import annotations

import sys
import os
import urllib.request
from pathlib import Path
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

# 确保项目根目录在 path 里
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from trader.paper import PaperAccount

app  = Flask(__name__)
CORS(app)

# ── 工具函数 ──────────────────────────────────────────

def _get_paper() -> PaperAccount:
    return PaperAccount()


def _live_quotes(codes: list[str]) -> dict[str, float]:
    """从腾讯 API 获取一批股票的实时价格"""
    if not codes:
        return {}
    items = [("sh" if c.startswith(("6","9")) else "sz") + c for c in codes]
    url   = "https://qt.gtimg.cn/q=" + ",".join(items)
    prices: dict[str, float] = {}
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        raw = urllib.request.urlopen(req, timeout=6).read().decode("gbk")
        for line in raw.strip().split("\n"):
            if '="' not in line:
                continue
            try:
                vals = line.split('="')[1].rstrip('";').split("~")
                if len(vals) < 4:
                    continue
                code  = vals[2]          # vals[2] 已是纯代码，如 "002156"
                price = float(vals[3]) if vals[3] else 0.0
                if code and price > 0:
                    prices[code] = price
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    return prices


# ── API 路由 ──────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/account")
def api_account():
    """账户总览 + 持仓列表"""
    paper = _get_paper()
    pos   = paper.positions()
    codes = list(pos.keys())
    prices = _live_quotes(codes)
    data   = paper.summary(prices)
    return jsonify(data)


@app.get("/api/positions")
def api_positions():
    """持仓实时行情"""
    paper  = _get_paper()
    pos    = paper.positions()
    codes  = list(pos.keys())
    prices = _live_quotes(codes)

    result = []
    for code, p in pos.items():
        price   = prices.get(code, p.cost)
        pnl     = round((price - p.cost) * p.shares, 2)
        pnl_pct = round((price - p.cost) / p.cost * 100, 2) if p.cost else 0
        result.append({
            "code":       code,
            "name":       p.name,
            "cost":       p.cost,
            "shares":     p.shares,
            "price":      price,
            "pnl":        pnl,
            "pnl_pct":    pnl_pct,
            "stop_price": p.stop_price,
            "mkt_value":  round(price * p.shares, 0),
            "entry_time": p.entry_time,
        })
    return jsonify(result)


@app.get("/api/watchlist")
def api_watchlist():
    """自选股 + 实时报价"""
    paper  = _get_paper()
    wlist  = paper.get_watchlist()
    codes  = [w["code"] for w in wlist]
    prices = _live_quotes(codes)

    for w in wlist:
        w["price"] = prices.get(w["code"], 0.0)
    return jsonify(wlist)


@app.post("/api/watchlist/add")
def api_watchlist_add():
    """添加自选股"""
    data   = request.get_json() or {}
    code   = data.get("code", "").strip()
    name   = data.get("name", "").strip()
    signal = data.get("signal", "手动添加")

    if not code:
        return jsonify({"ok": False, "msg": "code 不能为空"}), 400

    # 自动获取名称（若未传）
    if not name:
        try:
            prefix = "sh" if code.startswith(("6","9")) else "sz"
            url = f"https://qt.gtimg.cn/q={prefix}{code}"
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            raw = urllib.request.urlopen(req, timeout=5).read().decode("gbk")
            vals = raw.split('="')[1].rstrip('";').split("~") if '="' in raw else []
            name = vals[1] if len(vals) > 1 else code
        except Exception:
            name = code

    paper = _get_paper()
    ok    = paper.add_to_watchlist(code, name, signal)
    return jsonify({"ok": ok, "code": code, "name": name})


@app.delete("/api/watchlist/<code>")
def api_watchlist_remove(code: str):
    """移出自选股"""
    paper = _get_paper()
    paper.remove_from_watchlist(code)
    return jsonify({"ok": True, "code": code})


@app.get("/api/scan")
def api_scan():
    """最新一次扫描结果"""
    paper = _get_paper()
    return jsonify(paper.get_scan_results())


@app.get("/api/scan/run")
def api_scan_run():
    """手动触发一次扫描（异步，立即返回，结果推送微信）"""
    import threading
    from scanner.market_scan import scanner

    def _run():
        scanner.run()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "扫描已在后台启动，完成后推送企业微信"})


# ── 启动 ──────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  🚀 520量化 Web  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)

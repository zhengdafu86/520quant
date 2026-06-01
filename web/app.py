"""
520量化 Web 后端
Flask API + 静态页面服务
"""
from __future__ import annotations

import sys
import os
import time
import threading
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

# ── 日线数据缓存（避免每次 API 调用都走 mootdx）──────────────
# {code: (DataFrame, fetch_timestamp)}
_daily_cache: dict[str, tuple] = {}
_daily_cache_lock = threading.Lock()
_DAILY_TTL = 3600   # 1 小时后重新拉取

def _get_daily_df(code: str):
    """带 TTL 缓存的日线数据获取，1 小时内复用"""
    with _daily_cache_lock:
        if code in _daily_cache:
            df, ts = _daily_cache[code]
            if time.time() - ts < _DAILY_TTL:
                return df
    try:
        from data.fetcher import db as _mdb
        df = _mdb.get(code, freq="day", bars=65)
        if df is not None and not df.empty:
            with _daily_cache_lock:
                _daily_cache[code] = (df, time.time())
            return df
    except Exception:
        pass
    return None


def _live_signal(code: str) -> dict:
    """
    实时运行 520 策略信号分析（基于当日日线数据）。
    日线数据有 1 小时 TTL 缓存，每次刷新都能拿到当天收盘后更新的 K 线。
    返回 {} 表示当前无买点信号。
    """
    try:
        from strategy.signal_520 import strategy, Signal
        df = _get_daily_df(code)
        if df is None or df.empty or len(df) < 30:
            return {}
        result = strategy.analyze(df)
        if result.signal not in (
            Signal.BUY_GOLDEN_CROSS,
            Signal.BUY_PULLBACK,
            Signal.BUY_SQUEEZE,
        ):
            return {}
        return {
            "signal":     result.signal.value,
            "reason":     result.reason,
            "stop_price": result.stop_price or 0.0,
            "score":      result.score or 0,
            "cross_date": result.cross_date or "",
        }
    except Exception:
        return {}

# ── 工具函数 ──────────────────────────────────────────

def _get_paper() -> PaperAccount:
    return PaperAccount()


def _parse_quotes_raw(raw: str) -> dict[str, dict]:
    """解析腾讯报价 API 响应，返回完整字段"""
    result: dict[str, dict] = {}
    for line in raw.strip().split("\n"):
        if '="' not in line:
            continue
        try:
            vals = line.split('="')[1].rstrip('";').split("~")
            if len(vals) < 38:
                continue
            code = vals[2]
            if not code:
                continue

            def _v(idx, cast=float, default=0.0):
                try:
                    return cast(vals[idx]) if len(vals) > idx and vals[idx] else default
                except (ValueError, IndexError):
                    return default

            result[code] = {
                "price":        _v(3),
                "change_pct":   _v(32),     # 涨跌幅 %
                "vol_ratio":    _v(49),     # 量比
                "turnover_pct": _v(38),     # 换手率 %
                "amount_wan":   _v(37),     # 成交额（万元）
            }
        except Exception:
            continue
    return result


def _live_quotes(codes: list[str]) -> dict[str, float]:
    """从腾讯 API 获取一批股票的实时价格（兼容旧接口）"""
    if not codes:
        return {}
    items = [("sh" if c.startswith(("6", "9", "5")) else "sz") + c for c in codes]
    url   = "https://qt.gtimg.cn/q=" + ",".join(items)
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        raw = urllib.request.urlopen(req, timeout=6).read().decode("gbk")
        return {code: q["price"] for code, q in _parse_quotes_raw(raw).items()
                if q["price"] > 0}
    except Exception:
        return {}


def _live_quotes_full(codes: list[str]) -> dict[str, dict]:
    """从腾讯 API 获取完整报价（含涨跌幅、量比、换手率）"""
    if not codes:
        return {}
    items = [("sh" if c.startswith(("6", "9", "5")) else "sz") + c for c in codes]
    url   = "https://qt.gtimg.cn/q=" + ",".join(items)
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        raw = urllib.request.urlopen(req, timeout=6).read().decode("gbk")
        return _parse_quotes_raw(raw)
    except Exception:
        return {}


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
    """持仓实时行情（含账户汇总，共用同一次行情请求）"""
    paper  = _get_paper()
    pos    = paper.positions()
    codes  = list(pos.keys())
    prices = _live_quotes(codes)

    result = []
    pos_value = 0.0
    for code, p in pos.items():
        price     = prices.get(code, p.cost)
        pnl       = round((price - p.cost) * p.shares, 2)
        pnl_pct   = round((price - p.cost) / p.cost * 100, 2) if p.cost else 0
        mkt_value = round(price * p.shares, 0)
        pos_value += mkt_value
        result.append({
            "code":       code,
            "name":       p.name,
            "cost":       p.cost,
            "shares":     p.shares,
            "price":      price,
            "pnl":        pnl,
            "pnl_pct":    pnl_pct,
            "stop_price": p.stop_price,
            "mkt_value":  mkt_value,
            "entry_time": p.entry_time,
        })

    total_assets = round(paper.cash + pos_value, 2)
    total_return = round(
        (total_assets - paper.init_capital) / paper.init_capital * 100, 2
    ) if paper.init_capital else 0

    return jsonify({
        "positions":    result,
        "cash":         round(paper.cash, 2),
        "pos_value":    round(pos_value, 2),
        "total_assets": total_assets,
        "total_return": total_return,
    })


@app.get("/api/watchlist")
def api_watchlist():
    """
    自选股 + 实时报价（涨跌幅/量比）+ 实时520信号（日线缓存1h）
    信号来源：
      ① 实时：_live_signal() 当场跑 520 策略，日线数据 1h TTL 缓存
      ② 兜底：上次扫描结果（RS分 / 板块方向，实时计算成本高故复用）
    """
    paper  = _get_paper()
    wlist  = paper.get_watchlist()
    codes  = [w["code"] for w in wlist]

    # 实时报价（价格 / 涨跌幅 / 量比 / 换手率）
    quotes = _live_quotes_full(codes)

    # 历史扫描结果，仅用于补充 RS分 和 板块方向（这两项计算需要大盘数据，按需复用）
    scan_data = paper.get_scan_results()
    scan_map  = {r["code"]: r for r in (scan_data.get("results") or [])}

    for w in wlist:
        code = w["code"]
        q    = quotes.get(code, {})

        # ── 实时行情 ──────────────────────────────────
        w["price"]        = round(q.get("price", 0.0), 2)
        w["change_pct"]   = round(q.get("change_pct", 0.0), 2)
        w["vol_ratio"]    = round(q.get("vol_ratio", 0.0), 2)
        w["turnover_pct"] = round(q.get("turnover_pct", 0.0), 2)

        # ── 实时 520 信号（日线缓存，当日有效）─────────
        live = _live_signal(code)
        if live:
            w["scan_signal"]     = live["signal"]
            w["scan_reason"]     = live["reason"]
            w["scan_stop"]       = live["stop_price"]
            w["scan_score"]      = live["score"]
            w["scan_cross_date"] = live["cross_date"]
        else:
            w["scan_signal"]     = ""
            w["scan_reason"]     = ""
            w["scan_stop"]       = 0.0
            w["scan_score"]      = 0
            w["scan_cross_date"] = ""

        # ── RS分 和 板块方向：复用上次扫描结果 ──────────
        hit = scan_map.get(code)
        w["scan_rs"]         = hit.get("rs_score", None) if hit else None
        w["scan_sector_dir"] = hit.get("sector_dir", "") if hit else ""

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


@app.post("/api/positions/<code>/sell")
def api_position_sell(code: str):
    """手动卖出持仓（模拟）"""
    data = request.get_json() or {}
    try:
        price = float(data.get("price", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "msg": "价格格式错误"}), 400
    if price <= 0:
        return jsonify({"ok": False, "msg": "价格必须大于 0"}), 400

    paper = _get_paper()
    ok, msg = paper.sell(code, price, signal="手动卖出")
    return jsonify({"ok": ok, "msg": msg})


@app.delete("/api/watchlist/<code>")
def api_watchlist_remove(code: str):
    """移出自选股"""
    paper = _get_paper()
    paper.remove_from_watchlist(code)
    return jsonify({"ok": True, "code": code})


@app.patch("/api/watchlist/<code>/priority")
def api_watchlist_priority(code: str):
    """设置自选股优先级（P1 / P2 / P3 / 空字符串清除）"""
    data     = request.get_json() or {}
    priority = data.get("priority", "").strip()
    if priority not in ("", "P1", "P2", "P3"):
        return jsonify({"ok": False, "msg": "priority 须为 P1 / P2 / P3 或空"}), 400
    paper = _get_paper()
    paper.update_watchlist_priority(code, priority)
    return jsonify({"ok": True, "code": code, "priority": priority})


@app.get("/api/trades")
def api_trades():
    """交易记录，以股票为维度聚合，含每笔收益详情"""
    paper = _get_paper()
    rows  = paper._conn.execute(
        "SELECT id,code,name,side,price,shares,amount,signal,timestamp "
        "FROM orders ORDER BY id"
    ).fetchall()

    # 按股票分组
    stock_map: dict[str, dict] = {}
    for id_, code, name, side, price, shares, amount, signal, ts in rows:
        if code not in stock_map:
            stock_map[code] = {"code": code, "name": name, "orders": [], "trades": []}
        stock_map[code]["orders"].append({
            "id": id_, "side": side, "price": price,
            "shares": shares, "amount": amount,
            "signal": signal, "timestamp": ts,
        })

    result = []
    for code, data in stock_map.items():
        orders = data["orders"]

        # 用队列匹配买卖，计算每笔完整交易收益
        buy_queue = []
        completed = []
        for o in orders:
            if o["side"] == "BUY":
                buy_queue.append(o)
            elif o["side"] == "SELL" and buy_queue:
                buy = buy_queue.pop(0)
                commission = round((buy["amount"] + o["amount"]) * 0.0003, 2)
                stamp_tax  = round(o["amount"] * 0.001, 2)
                pnl        = round(o["amount"] - buy["amount"] - commission - stamp_tax, 2)
                pnl_pct    = round(pnl / buy["amount"] * 100, 2)
                completed.append({
                    "buy_price":  buy["price"],
                    "sell_price": o["price"],
                    "shares":     o["shares"],
                    "buy_amount": buy["amount"],
                    "sell_amount": o["amount"],
                    "pnl":        pnl,
                    "pnl_pct":   pnl_pct,
                    "buy_time":  buy["timestamp"],
                    "sell_time": o["timestamp"],
                    "sell_signal": o["signal"],
                })

        # 还在持仓中的买单（未平仓）
        open_buy = buy_queue[0] if buy_queue else None
        total_pnl = round(sum(t["pnl"] for t in completed), 2)

        result.append({
            "code":       code,
            "name":       data["name"],
            "orders":     orders,
            "trades":     completed,
            "open":       open_buy is not None,
            "open_price": open_buy["price"] if open_buy else None,
            "open_shares": open_buy["shares"] if open_buy else None,
            "open_time":  open_buy["timestamp"] if open_buy else None,
            "total_pnl":  total_pnl,
            "win":        len([t for t in completed if t["pnl"] > 0]),
            "loss":       len([t for t in completed if t["pnl"] <= 0]),
        })

    # 按总收益降序排列
    result.sort(key=lambda x: x["total_pnl"], reverse=True)
    return jsonify(result)


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

"""
520量化 Web 后端
Flask API + 静态页面服务
"""
from __future__ import annotations

import sys
import os
import json
import time
import threading
import secrets
import urllib.request
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, render_template, session, redirect
from flask_cors import CORS

# 确保项目根目录在 path 里
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from trader.paper import PaperAccount, BASE_DIR, delete_user_data
from auth.users import (
    verify_user, is_admin, list_users, create_user, delete_user,
)

app  = Flask(__name__)
CORS(app, supports_credentials=True)   # 允许携带 session cookie


def _load_secret_key() -> str:
    """
    持久化 Flask secret key，避免重启后所有用户被登出。
    多 worker（gunicorn）并发安全：用 O_CREAT|O_EXCL 原子创建，
    抢占失败者回读胜出者写入的同一把 key，杜绝多 worker key 不一致导致的 session 失效。
    """
    key_file = BASE_DIR / ".flask_secret"
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        # 已存在直接读
        if key_file.exists():
            existing = key_file.read_text().strip()
            if existing:
                return existing
        # 原子独占创建
        key = secrets.token_hex(32)
        try:
            fd = os.open(str(key_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, key.encode())
            finally:
                os.close(fd)
            return key
        except FileExistsError:
            # 另一 worker 已抢先写入，回读其值
            return key_file.read_text().strip() or key
    except Exception:
        # 极端情况下退回到进程内随机（重启会登出，但不影响功能）
        return secrets.token_hex(32)


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=7 * 24 * 3600,   # 登录态保持 7 天
)

# 无需登录即可访问的路径（登录页本身 + 登录接口 + 静态资源）
_PUBLIC_PATHS = {"/login", "/api/login"}


@app.before_request
def _require_login():
    """全局登录守卫：除白名单外须登录；/admin 与 /api/admin/ 额外要求管理员"""
    path = request.path
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return None
    user = session.get("user")
    if not user:
        # 未登录：API 返回 401，页面跳转登录
        if path.startswith("/api/"):
            return jsonify({"ok": False, "auth": False, "msg": "未登录或登录已过期"}), 401
        return redirect("/login")
    # 管理员守卫
    if path == "/admin" or path.startswith("/api/admin/"):
        if not is_admin(user):
            if path.startswith("/api/"):
                return jsonify({"ok": False, "msg": "需要管理员权限"}), 403
            return redirect("/")
    return None

# ── 扫描运行状态（跨请求共享，防重复触发）──────────────────────
_scan_state: dict = {"running": False, "started_at": None}

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
            "signal":       result.signal.value,
            "reason":       result.reason,
            "stop_price":   result.stop_price or 0.0,
            "score":        result.score or 0,
            "cross_date":   result.cross_date or "",
            "score_detail": result.score_detail or [],
        }
    except Exception:
        return {}

# ── 工具函数 ──────────────────────────────────────────

def _get_paper() -> PaperAccount:
    """返回当前登录用户的隔离账户（数据物理隔离在各自 DB 文件）"""
    return PaperAccount(user=session.get("user"))


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

@app.get("/login")
def login_page():
    """登录页（已登录则直接跳转首页）"""
    if session.get("user"):
        return redirect("/")
    return render_template("login.html")


@app.post("/api/login")
def api_login():
    """登录：校验用户名 / 密码，写入 session"""
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "msg": "请输入用户名和密码"}), 400
    if not verify_user(username, password):
        return jsonify({"ok": False, "msg": "用户名或密码错误"}), 401
    session.permanent = True
    session["user"] = username
    return jsonify({"ok": True, "user": username})


@app.post("/api/logout")
def api_logout():
    """登出：清除 session"""
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    """当前登录用户 + 是否管理员"""
    user = session.get("user")
    return jsonify({"user": user, "is_admin": is_admin(user) if user else False})


@app.get("/")
def index():
    return render_template("index.html")


# ── 用户管理（仅管理员，已在 before_request 守卫）──────────────

@app.get("/admin")
def admin_page():
    return render_template("admin.html")


@app.get("/api/admin/users")
def api_admin_users():
    """列出所有用户（含角色、创建时间），标记当前登录用户"""
    me = session.get("user")
    users = list_users()
    for u in users:
        u["is_self"] = (u["username"] == me)
    return jsonify({"users": users, "me": me})


@app.post("/api/admin/users")
def api_admin_user_create():
    """新增用户（默认普通用户）"""
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    make_admin = bool(data.get("is_admin", False))
    ok, msg = create_user(username, password, is_admin=make_admin)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.delete("/api/admin/users/<username>")
def api_admin_user_delete(username: str):
    """删除用户 + 其隔离数据目录；禁止删除自己"""
    me = session.get("user")
    if username == me:
        return jsonify({"ok": False, "msg": "不能删除当前登录的自己"}), 400
    ok, msg = delete_user(username)
    if ok:
        # 连带清理该用户的隔离业务数据（持仓/记录/自选/账户）
        delete_user_data(username)
        msg = f"{msg}，其持仓/记录/自选/账户数据已一并清除"
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


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
            "stop_price":   p.stop_price,
            "mkt_value":    mkt_value,
            "entry_time":   p.entry_time,
            "entry_signal": p.entry_signal,
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
    自选股 + 实时报价（涨跌幅/量比）+ 买点标签（仅按最近扫描结果匹配）
    买点标签来源：最近一次扫描结果（scan_results）匹配 code；
    不再实时重算 _live_signal——是否真能买由引擎 check_entry 实时确认，互不影响。
    """
    paper  = _get_paper()
    wlist  = paper.get_watchlist()
    codes  = [w["code"] for w in wlist]

    # 实时报价（价格 / 涨跌幅 / 量比 / 换手率）
    quotes = _live_quotes_full(codes)

    # 历史扫描结果，仅用于补充 RS分 和 板块方向（这两项计算需要大盘数据，按需复用）
    scan_data = paper.get_scan_results()
    scan_map  = {r["code"]: r for r in (scan_data.get("results") or [])}

    # 当前用户持仓集合 —— 标注自选股是否已持仓
    held_set  = set(paper.positions().keys())

    for w in wlist:
        code = w["code"]
        q    = quotes.get(code, {})
        w["held"] = code in held_set

        # ── 实时行情 ──────────────────────────────────
        w["price"]        = round(q.get("price", 0.0), 2)
        w["change_pct"]   = round(q.get("change_pct", 0.0), 2)
        w["vol_ratio"]    = round(q.get("vol_ratio", 0.0), 2)
        w["turnover_pct"] = round(q.get("turnover_pct", 0.0), 2)

        # ── 买点标签：只按扫描结果匹配显示（不再实时重算 _live_signal）──
        #    即展示「最近一次扫描时是否买点」的快照；是否真能买由引擎实时确认，互不影响。
        hit = scan_map.get(code)
        if hit:
            w["scan_signal"]       = hit.get("signal", "")
            w["scan_reason"]       = hit.get("reason", "")
            w["scan_stop"]         = hit.get("stop_price", 0.0)
            w["scan_score"]        = hit.get("score", 0)
            w["scan_cross_date"]   = hit.get("cross_date", "")
            w["scan_score_detail"] = hit.get("score_detail", [])
        else:
            w["scan_signal"]       = ""
            w["scan_reason"]       = ""
            w["scan_stop"]         = 0.0
            w["scan_score"]        = 0
            w["scan_cross_date"]   = ""
            w["scan_score_detail"] = []

        # ── RS分、板块方向、行业名、AI评分：同取自扫描结果 ──────
        w["scan_rs"]          = hit.get("rs_score",    None) if hit else None
        w["scan_sector_dir"]  = hit.get("sector_dir",  "")   if hit else ""
        w["scan_sector_name"] = hit.get("sector_name", "")   if hit else ""
        w["ai_score"]         = hit.get("ai_score",    0)    if hit else 0
        w["ai_comment"]       = hit.get("ai_comment",  "")   if hit else ""

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
    """交易记录，以股票为维度聚合，含每笔收益详情（失效交易单独标记，不计入统计）"""
    paper = _get_paper()
    rows  = paper._conn.execute(
        "SELECT id,code,name,side,price,shares,amount,signal,timestamp,"
        "COALESCE(voided,0),COALESCE(conditions,'[]') "
        "FROM orders ORDER BY id"
    ).fetchall()

    # 按股票分组
    stock_map: dict[str, dict] = {}
    for id_, code, name, side, price, shares, amount, signal, ts, voided, conditions_json in rows:
        if code not in stock_map:
            stock_map[code] = {"code": code, "name": name, "orders": [], "trades": []}
        stock_map[code]["orders"].append({
            "id": id_, "side": side, "price": price,
            "shares": shares, "amount": amount,
            "signal": signal, "timestamp": ts,
            "voided": bool(voided),
            "conditions": json.loads(conditions_json) if conditions_json else [],
        })

    result = []
    for code, data in stock_map.items():
        orders = data["orders"]

        # 按【股数】FIFO 匹配买卖（支持分批卖出：1个买单可对多个卖单）
        # 每个卖单从最前买单逐股扣减，盈亏只算匹配到的股数
        buy_queue = []   # 每项含 remaining 剩余未匹配股数
        completed = []
        for o in orders:
            if o["side"] == "BUY":
                buy_queue.append({**o, "remaining": o["shares"]})
            elif o["side"] == "SELL":
                sell_rem = o["shares"]
                matched_shares = 0
                matched_buy_amount = 0.0
                first_buy = None
                while sell_rem > 0 and buy_queue:
                    b = buy_queue[0]
                    take = min(sell_rem, b["remaining"])
                    if first_buy is None:
                        first_buy = b
                    matched_shares     += take
                    matched_buy_amount += take * b["price"]
                    b["remaining"] -= take
                    sell_rem       -= take
                    if b["remaining"] <= 0:
                        buy_queue.pop(0)
                if matched_shares <= 0 or first_buy is None:
                    continue
                sell_amount = round(o["price"] * matched_shares, 2)
                buy_amount  = round(matched_buy_amount, 2)
                commission  = round((buy_amount + sell_amount) * 0.0001, 2)
                stamp_tax   = round(sell_amount * 0.001, 2)
                pnl         = round(sell_amount - buy_amount - commission - stamp_tax, 2)
                pnl_pct     = round(pnl / buy_amount * 100, 2) if buy_amount else 0
                completed.append({
                    "sell_order_id":  o["id"],
                    "buy_price":      first_buy["price"],
                    "sell_price":     o["price"],
                    "shares":         matched_shares,
                    "buy_amount":     buy_amount,
                    "sell_amount":    sell_amount,
                    "pnl":            pnl,
                    "pnl_pct":        pnl_pct,
                    "buy_time":       first_buy["timestamp"],
                    "sell_time":      o["timestamp"],
                    "buy_signal":     first_buy["signal"],
                    "sell_signal":    o["signal"],
                    "voided":         o["voided"],
                    "buy_conditions":  first_buy.get("conditions", []),
                    "sell_conditions": o.get("conditions", []),
                })

        # 还在持仓中的买单（未平仓）：剩余未匹配股数
        open_buy = buy_queue[0] if buy_queue else None
        open_shares = sum(b["remaining"] for b in buy_queue) if buy_queue else None

        # 仅有效（非失效）交易计入统计
        active = [t for t in completed if not t["voided"]]
        total_pnl    = round(sum(t["pnl"] for t in active), 2)
        voided_count = len([t for t in completed if t["voided"]])

        result.append({
            "code":          code,
            "name":          data["name"],
            "orders":        orders,
            "trades":        completed,
            "open":          open_buy is not None,
            "open_price":    open_buy["price"]      if open_buy else None,
            "open_shares":   open_shares            if open_buy else None,
            "open_time":     open_buy["timestamp"]  if open_buy else None,
            "open_signal":   open_buy["signal"]     if open_buy else "",
            "open_conditions": open_buy.get("conditions", []) if open_buy else [],
            "total_pnl":     total_pnl,        # 仅有效交易
            "voided_count":  voided_count,     # 已失效笔数
            "win":           len([t for t in active if t["pnl"] > 0]),
            "loss":          len([t for t in active if t["pnl"] <= 0]),
        })

    # 按总收益降序排列（基于有效交易）
    result.sort(key=lambda x: x["total_pnl"], reverse=True)
    return jsonify(result)


@app.post("/api/trades/<int:order_id>/void")
def api_trade_void(order_id: int):
    """切换一笔已平仓交易的失效状态（失效↔有效）"""
    data   = request.get_json() or {}
    voided = bool(data.get("voided", True))
    paper  = _get_paper()
    ok, msg = paper.void_trade(order_id, voided)
    return jsonify({"ok": ok, "msg": msg})


@app.get("/api/scan")
def api_scan():
    """最新一次扫描结果（标注是否已持仓 + 实时当日涨跌幅/现价）"""
    paper = _get_paper()
    data  = paper.get_scan_results()
    held  = set(paper.positions().keys())
    results = data.get("results", [])
    # 实时报价覆盖：价格 / 当日涨跌幅（取不到则保留扫描时静态值，如停牌）
    quotes = _live_quotes_full([r["code"] for r in results]) if results else {}
    for r in results:
        r["held"] = r["code"] in held
        q = quotes.get(r["code"])
        if q and q.get("price", 0) > 0:
            r["price"]      = round(q["price"], 2)
            r["change_pct"] = round(q.get("change_pct", 0.0), 2)
            r["live"]       = True
        else:
            r["live"] = False
    return jsonify(data)


@app.get("/api/hot")
def api_hot():
    """今日热门题材榜（同花顺热点聚合）"""
    try:
        from scanner.hot_sectors import get_hot
        return jsonify(get_hot())
    except Exception as e:
        return jsonify({"date": "", "themes": [], "error": str(e)})


@app.get("/api/strength")
def api_strength():
    """市场强弱仪表盘：指数趋势+全市场涨跌家数+本账户近10笔胜率 → 强/中性/弱。"""
    from collections import defaultdict, deque
    try:
        from scanner import market_strength
        paper = _get_paper()
        rows = paper._conn.execute(
            "SELECT code,side,price,shares FROM orders "
            "WHERE COALESCE(voided,0)=0 ORDER BY id").fetchall()
        q = defaultdict(deque); wins = []
        for code, side, price, shares in rows:
            if side == "BUY":
                q[code].append([price, shares])
            else:
                rem = shares; cost = 0.0; ms = 0
                while rem > 0 and q[code]:
                    b = q[code][0]; take = min(rem, b[1])
                    cost += b[0] * take; ms += take; b[1] -= take; rem -= take
                    if b[1] <= 0:
                        q[code].popleft()
                if ms > 0:
                    wins.append(1 if price * ms - cost > 0 else 0)
        recent = wins[-10:]
        wr = (sum(recent) / len(recent) * 100) if recent else None
        d = market_strength.compute(recent_winrate=wr, recent_n=len(recent))
        d["pos_cap"] = paper.get_pos_cap()          # 当前持仓数上限(供前端展示+调节)
        return jsonify(d)
    except Exception as e:
        return jsonify({"verdict": "—", "advice": "数据获取失败", "error": str(e)[:80]})


@app.post("/api/settings/poscap")
def api_set_poscap():
    """设置持仓数上限(0-4)。0=不开新仓(弱市持币)；只限开仓数量，不改每仓大小。"""
    paper = _get_paper()
    try:
        n = int((request.get_json(silent=True) or {}).get("pos_cap"))
    except Exception:
        return jsonify({"ok": False, "msg": "参数无效"})
    paper.set_pos_cap(n)
    return jsonify({"ok": True, "pos_cap": paper.get_pos_cap()})


@app.get("/api/scan/status")
def api_scan_status():
    """当前扫描运行状态（前端用于轮询进度）"""
    return jsonify({
        "running":    _scan_state["running"],
        "started_at": _scan_state["started_at"],
    })


@app.get("/api/scan/run")
def api_scan_run():
    """手动触发一次扫描（异步，立即返回；扫描进行中时拒绝重复触发）"""
    if _scan_state["running"]:
        return jsonify({
            "ok":  False,
            "msg": f"扫描正在执行中（{_scan_state['started_at']} 开始），请稍候",
        })

    from scanner.market_scan import scanner

    _scan_state["running"]    = True
    _scan_state["started_at"] = datetime.now().strftime("%H:%M:%S")

    def _run():
        try:
            scanner.run()
        finally:
            _scan_state["running"] = False
            # 扫描结束后清空日线缓存，确保下次查看自选股时重新拉取数据计算信号
            with _daily_cache_lock:
                _daily_cache.clear()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": "扫描已在后台启动，完成后推送企业微信"})


# ── 启动 ──────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  🚀 D-Trade Web  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)

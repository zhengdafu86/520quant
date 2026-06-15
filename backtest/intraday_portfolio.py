"""
Phase 2 · 完整组合级忠实回测（分钟级回放实盘进出场逻辑）
================================================
与日线回测(开盘/收盘撮合)不同，本回测在真实 5 分钟K上逐根运行实盘的
check_entry / check_position，并复刻追踪止损、6 仓限制、T+1、佣金+印花，
得到"与实盘进出场逻辑完全一致"的结果。

数据：~/.520quant/intraday.db（需先用 backfill_baostock 回补 5 分钟历史）。
宇宙：build_universe(sample, seed)，须与回补的样本一致。

简化与口径（务必知悉）：
  - 量比按 1.0 放行（5分钟量与日线量纲不一致，无法精确重建）→ 进场略宽松。
  - 相对强弱 market_chg 置 0 → 同上。
  - 候选优先级简化为"粘合优先 → 评分降序"（实盘是盈亏比优先，影响二阶）。
  - 日线 MA 取截至 T-1（与实盘候选/持仓所用的最近完整日线一致）。
  - 复权：5分钟与日线均不复权，口径一致。

用法:
  python -m backtest.intraday_portfolio --sample 150 --seed 42
        --start 2025-05-22 --end 2026-06-02 --max-pos 6 --slippage 0.0
"""
from __future__ import annotations

import os
import sys
import bisect
import argparse
import statistics
import hashlib
import pickle
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from data.fetcher import db
from data import intraday_store as ids
from strategy.signal_520 import strategy, Signal
import monitor.intraday as IT
from monitor.intraday import engine as IE, Action
from monitor.realtime import is_buy_window as _RBW, is_profit_exit_window as _RPEW
from monitor.engine import SIGNAL_SIZE, MonitorEngine
from backtest.replay import build_universe

COMMISSION = 0.0001   # 万1 佣金（买卖双向）
STAMP_TAX  = 0.001
TIERS = MonitorEngine._TRAIL_TIERS
BUY_SIGS = (Signal.BUY_GOLDEN_CROSS, Signal.BUY_PULLBACK, Signal.BUY_SQUEEZE)

# ── 回放上下文 + 打桩（让实盘函数读 point-in-time 数据 / 按 bar 时间判窗）──
_CTX = {"dt": None, "mindf": None}
IT.is_buy_window         = lambda now=None, signal_type="": _RBW(now=_CTX["dt"], signal_type=signal_type)
IT.is_profit_exit_window = lambda now=None: _RPEW(now=_CTX["dt"])
IT.get_minute_bars       = lambda code, freq="5m", count=20: (
    _CTX["mindf"].tail(count) if _CTX["mindf"] is not None else pd.DataFrame())


def _elapsed_min(dt_str: str) -> int:
    """从开盘到该 5 分钟K结束时刻的累计交易分钟（午休不计）"""
    hh, mm = int(dt_str[11:13]), int(dt_str[14:16])
    t = hh * 60 + mm
    open1, close1, open2 = 9 * 60 + 30, 11 * 60 + 30, 13 * 60
    if t <= close1:
        return max(0, t - open1)
    return 120 + max(0, t - open2)


def _atr(daily, n: int = 14) -> float:
    """日线 ATR(n)（价格单位）——用于波动率自适应止损"""
    if daily is None or len(daily) < n + 1:
        return 0.0
    h = daily["high"].astype(float)
    l = daily["low"].astype(float)
    c = daily["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


def _sig_mult(sig: str) -> float:
    if "粘合" in sig or "发散" in sig:
        return SIGNAL_SIZE[Signal.BUY_SQUEEZE]
    if "回踩" in sig:
        return SIGNAL_SIZE[Signal.BUY_PULLBACK]
    return SIGNAL_SIZE[Signal.BUY_GOLDEN_CROSS]


def _update_stop(pos: dict, price: float, ma5: float, tiers=None, ma5_min: float = 10.0):
    """复刻 engine._update_trailing_stops：止损线只升不降。
    tiers: 自定义分档(默认现行 TIERS)；ma5_min: 达此浮盈档起，止损还与 MA5×0.97 取高。"""
    tiers = tiers if tiers is not None else TIERS
    gain = (price - pos["cost"]) / pos["cost"] * 100
    cand = pos["stop"]
    for min_gain, _label, mult in tiers:
        if gain >= min_gain:
            base = pos["cost"] * mult
            cand = max(base, round(ma5 * 0.97, 2)) if (min_gain >= ma5_min and ma5 > 0) else base
            break
    if cand > pos["stop"]:
        pos["stop"] = round(cand, 2)


def _load(code: str):
    """返回 (daily_df_with_date, m5_by_date, date_list)"""
    daily = db.get(code, freq="day", bars=int(os.environ.get("BT_DAILY_BARS", 320)))
    if daily is None or daily.empty:
        return None, None, None
    daily = daily.copy()
    daily["d"] = daily["datetime"].astype(str).str[:10]
    bars = ids.get_bars(code, "5m")            # [(dt,o,h,l,c,v), ...]
    if not bars:
        return None, None, None
    # 5分钟仅载入所需窗口，避免把全库(含2024回补)都读进内存→OOM
    _lo = os.environ.get("BT_MIN_DATE", "")
    _hi = os.environ.get("BT_MAX_DATE", "")
    m5 = {}
    for dt, o, h, l, c, v in bars:
        d = dt[:10]
        if (_lo and d < _lo) or (_hi and d > _hi):
            continue
        m5.setdefault(d, []).append((dt, o, h, l, c, v))
    return daily, m5, daily["d"].tolist()


def _precompute_candidates(daily: pd.DataFrame) -> dict:
    """走一遍日线：返回 {候选日: (信号类型, asof_end)}，asof_end=daily.iloc[:asof_end] 即截至T-1"""
    out = {}
    n = len(daily)
    dlist = daily["d"].tolist()
    for j in range(24, n - 1):
        res = strategy.analyze(daily.iloc[: j + 1])
        if res.signal in BUY_SIGS:
            out[dlist[j + 1]] = (res.signal.value, j + 1, res.score or 0)  # (信号, asof, 评分)
    return out


_CACHE_DIR = Path.home() / ".520quant" / "bt_cache"


def _ctx_cache_key(codes) -> str:
    """缓存键：股票池 + DB版本 + signal_520/fetcher内容哈希 + 候选相关flag。
    任一变化 → 键变 → 自动重算（杜绝陈旧候选）。"""
    import os
    import strategy.signal_520 as s
    root = Path(__file__).parent.parent
    db_file = Path.home() / ".520quant" / "intraday.db"
    try:
        st = db_file.stat(); dbsig = f"{st.st_mtime_ns}-{st.st_size}"
    except Exception:
        dbsig = "nodb"
    def _h(p):
        try:
            return hashlib.md5((root / p).read_bytes()).hexdigest()[:10]
        except Exception:
            return "x"
    flags = (getattr(s, "MULTIDAY_SHRINK", None), getattr(s, "MULTIDAY_SHRINK_RATIO", None),
             getattr(s, "MULTIDAY_SHRINK_BONUS", None), getattr(s, "SCORE_MAX", None),
             getattr(s, "UPSIDE_ROOM_FILTER", None), getattr(s, "UPSIDE_LOOKBACK", None),
             getattr(s, "UPSIDE_MIN_ROOM", None))
    raw = (f"{len(codes)}|{'|'.join(sorted(codes))}|{dbsig}|"
           f"sig{_h('strategy/signal_520.py')}|fet{_h('data/fetcher.py')}|{flags}")
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def load_ctx(codes):
    """带缓存：键不变则秒级读盘，跳过~10分钟的加载+候选预计算。
    环境变量 BT_NO_CACHE=1 可强制重算。"""
    import os
    if not os.environ.get("BT_NO_CACHE"):
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            f = _CACHE_DIR / f"ctx_{_ctx_cache_key(codes)}.pkl"
            if f.exists():
                with open(f, "rb") as fh:
                    ctx = pickle.load(fh)
                print(f"[ctx缓存命中] {f.name}（跳过加载，秒级）")
                return ctx
        except Exception as e:
            print(f"[ctx缓存读取跳过] {e}")
            f = None
    else:
        f = None
    ctx = _load_ctx_compute(codes)
    if f is not None:
        try:
            with open(f, "wb") as fh:
                pickle.dump(ctx, fh, protocol=pickle.HIGHEST_PROTOCOL)
            print(f"[ctx已缓存] {f.name}（下次秒级读取）")
        except Exception as e:
            print(f"[ctx缓存写入失败] {e}")
    return ctx


def _load_ctx_compute(codes):
    """一次性加载：日线 + 5分钟 + 候选 + 各日全天量。返回 ctx 供多次 simulate 复用。"""
    daily_map, m5_map, cand_map, day_total, sdates = {}, {}, {}, {}, {}
    for c in codes:
        d, m5, _ = _load(c)
        if d is None:
            continue
        daily_map[c] = d
        m5_map[c] = m5
        cand_map[c] = _precompute_candidates(d)
        day_total[c] = {dt: sum(float(b[5]) for b in bars) for dt, bars in m5.items()}
        sdates[c] = sorted(m5.keys())
    mk = db.get_market(bars=int(os.environ.get("BT_DAILY_BARS", 320))).copy()
    mk["d"] = mk["datetime"].astype(str).str[:10]
    return {"daily": daily_map, "m5": m5_map, "cand": cand_map,
            "day_total": day_total, "sdates": sdates, "mk": mk}


def load_daily_cands(codes, bars=320):
    """日线+候选(全市场) 磁盘缓存 —— 避免每次回测重复 ~14min 候选预计算。
    缓存键=最新交易日+bars+股数(新交易日数据到来自动失效)。返回 (daily_map, cand_map, mk)。
    候选不含防御过滤(调用方按需 {} if SM in DEF)，保持通用。"""
    mk = db.get_market(bars=bars).copy()
    mk["d"] = mk["datetime"].astype(str).str[:10]
    cache_dir = os.path.expanduser("~/.520quant/bt_cache")
    os.makedirs(cache_dir, exist_ok=True)
    last = str(mk["d"].iloc[-1]) if len(mk) else "na"
    path = os.path.join(cache_dir, f"dc_{last}_{bars}_{len(codes)}.pkl")
    if os.path.exists(path):
        with open(path, "rb") as f:
            daily_map, cand_map = pickle.load(f)
        return daily_map, cand_map, mk
    daily_map, cand_map = {}, {}
    for c in codes:
        d = db.get(c, freq="day", bars=bars)
        if d is None or d.empty:
            continue
        d = d.copy(); d["d"] = d["datetime"].astype(str).str[:10]
        daily_map[c] = d
        cand_map[c] = _precompute_candidates(d)
    ok_rate = len(daily_map) / max(1, len(codes))
    if ok_rate >= 0.97:
        with open(path, "wb") as f:
            pickle.dump((daily_map, cand_map), f)
    else:   # 数据源抖动导致大量取空 → 不冻结残缺宇宙, 下次重试
        print(f"[缓存跳过] 仅{len(daily_map)}/{len(codes)}只({ok_rate:.0%}<97%),疑mootdx抖动,不缓存")
    return daily_map, cand_map, mk


def load_5m_window(codes, start, end):
    """只载入 [start,end] 区间5分钟 + 派生 day_total/sdates(省内存)。"""
    m5_map, day_total, sdates = {}, {}, {}
    for c in codes:
        bars = ids.get_bars(c, "5m")
        if not bars:
            continue
        m5 = {}
        for dt, o, h, l, cl, v in bars:
            dd = dt[:10]
            if start <= dd <= end:
                m5.setdefault(dd, []).append((dt, o, h, l, cl, v))
        if m5:
            m5_map[c] = m5
            day_total[c] = {dt: sum(float(b[5]) for b in bs) for dt, bs in m5.items()}
            sdates[c] = sorted(m5.keys())
    return m5_map, day_total, sdates


def simulate(ctx, names, start, end, max_pos=6, capital=200_000, slippage=0.0,
             top_n=8, atr_mult=0.0, scale_pct=0.0, priority="squeeze_risk",
             tiers=None, ma5_min=10.0, stop_on_low=False, tail_entry=False,
             market_mode="ma20_ma60", macd_confirm="off", top_div="off",
             limit_lock="off", open_buffer=0, cap_map=None,
             rs_override=0.0, rs_weak_cap=2, breadth_map=None, breadth_thresh=1.0,
             topwarn_days=0, topwarn_vol=False, er_min=0.0, cross_max=0,
             reentry_cd=0):
    """在 ctx 上跑一次忠实回测（使用 intraday 模块当前的止损/止盈参数）。
    top_n: 每日"精选"上限——只在评分最高的 top_n 只信号股里建仓（0=不设上限）。
    返回 (trades, equity_curve, positions)。"""
    daily_map = ctx["daily"]; m5_map = ctx["m5"]; cand_map = ctx["cand"]
    day_total = ctx["day_total"]; sdates = ctx["sdates"]; mk = ctx["mk"]

    def _risk_dist(code, sig, asof):
        """到止损距离%（盈亏比代理）：回踩/粘合用MA20、金叉用MA5×0.97；越小越优先"""
        last = daily_map[code].iloc[asof - 1]
        px = float(last["close"]); ma20 = float(last["ma20"]); ma5 = float(last["ma5"])
        stop_ref = ma20 if ("粘合" in sig or "发散" in sig or "回踩" in sig) else ma5 * 0.97
        if px <= 0 or stop_ref <= 0 or stop_ref >= px:
            return 9999.0
        return (px - stop_ref) / px * 100

    def _multiday_shrink(code, asof, ratio: float = 0.8):
        """近3日均量 ≤ 近20日均量×ratio（截至信号日 asof-1）→ 多日持续缩量。"""
        v = daily_map[code]["vol"].iloc[:asof].astype(float)
        if len(v) < 20:
            return False
        a20 = v.tail(20).mean()
        return a20 > 0 and v.tail(3).mean() <= a20 * ratio

    # 市场20日收益（按日期）→ 用于 RS 相对强度
    _mk = ctx["mk"]
    _mk_close = _mk["close"].astype(float).tolist()
    _mk_d = _mk["d"].tolist()
    _mk_ret20 = {}
    for _i in range(20, len(_mk_d)):
        if _mk_close[_i - 20] > 0:
            _mk_ret20[_mk_d[_i]] = _mk_close[_i] / _mk_close[_i - 20] - 1

    def _rs(code, asof):
        """相对强度 = 个股近20日收益 − 沪深300同期收益（截至信号日 asof-1）。越大越强。"""
        d = daily_map[code]
        if asof < 21:
            return -9.9
        c_now = float(d.iloc[asof - 1]["close"]); c_20 = float(d.iloc[asof - 21]["close"])
        if c_20 <= 0:
            return -9.9
        return (c_now / c_20 - 1) - _mk_ret20.get(str(d.iloc[asof - 1]["d"]), 0.0)

    def _macd_ok(code, asof, mode):
        """MACD柱动能确认（信号日=asof-1）：
        'up'     柱拐头向上（由缩转增的"增"）：macd[j] > macd[j-1]
        'trough' 缩转增（更严）：前一根还在缩(macd[j-1]<=macd[j-2])，这根转增
        数据不足一律放行(返回True)，不误杀。"""
        if mode == "off":
            return True
        d = daily_map[code]
        j = asof - 1
        if j < 2 or "macd" not in d.columns:
            return True
        m = d["macd"].values
        if pd.isna(m[j]) or pd.isna(m[j - 1]):
            return True
        up = m[j] > m[j - 1]
        if mode == "trough":
            return bool(up and (pd.isna(m[j - 2]) or m[j - 1] <= m[j - 2]))
        return bool(up)   # 'up'

    def _er(code, asof, N=20):
        """效率系数: |N日净涨幅|/Σ|每日涨跌|。→1干净趋势, →0震荡。数据不足返回1(放行)。"""
        d = daily_map[code]; j = asof - 1
        if j < N:
            return 1.0
        cl = d["close"].astype(float).values
        net = abs(cl[j] - cl[j - N])
        path = sum(abs(cl[k] - cl[k - 1]) for k in range(j - N + 1, j + 1))
        return net / path if path > 0 else 1.0

    def _ma20_cross(code, asof, N=20):
        """近N日收盘穿越MA20的次数。多=震荡。数据不足返回0(放行)。"""
        d = daily_map[code]; j = asof - 1
        if j < N or "ma20" not in d.columns:
            return 0
        cl = d["close"].astype(float).values
        ma = d["ma20"].astype(float).values
        sign = [1 if cl[k] >= ma[k] else -1 for k in range(j - N + 1, j + 1) if not pd.isna(ma[k])]
        return sum(1 for a, b in zip(sign, sign[1:]) if a != b)

    def _top_div(sub, N=20):
        """MACD顶背离（用截至T-1的日线sub）：价格较"前期DIF高点日"创了更高高点，
        但当前DIF反而更低，且仍在零轴上方(高位)。捕捉均线看不到的价/动能背离。"""
        if sub is None or len(sub) < N + 2 or "dif" not in sub.columns:
            return False
        dif = sub["dif"].values
        cl = sub["close"].values
        j = len(sub) - 1
        if pd.isna(dif[j]):
            return False
        # 前N根(不含当前)里 DIF 的高点位置
        lo = j - N
        p, pv = -1, -1e18
        for k in range(lo, j):
            if not pd.isna(dif[k]) and dif[k] > pv:
                pv, p = dif[k], k
        if p < 0:
            return False
        return bool(cl[j] > cl[p] and dif[j] < dif[p] and dif[j] > 0)

    def vol_ratio(code, T, i, day):
        elapsed = _elapsed_min(day[i][0])
        if elapsed <= 0:
            return 0.0
        cum = sum(float(day[j][5]) for j in range(i + 1))
        sd = sdates[code]
        idx = bisect.bisect_left(sd, T)
        prior = sd[max(0, idx - 5):idx]
        if not prior:
            return 1.0
        avg = sum(day_total[code][p] for p in prior) / len(prior)
        return (cum / elapsed) / (avg / 240.0) if avg > 0 else 1.0

    def market_up(date_t):
        sub = mk[mk["d"] < date_t]
        if sub.empty:
            return True
        last = sub.iloc[-1]
        try:
            ma20 = float(last["ma20"]); ma60 = float(last["ma60"])
            cross = ma20 > ma60
            if market_mode == "ma20_ma60":           # 现行：金叉
                return cross
            slope = float(last.get("ma20_slope", 0))
            rising = slope > 0                        # MA20 转头向上
            if market_mode == "slope":                # 仅看 MA20 斜率转正(更灵敏)
                return rising
            if market_mode == "either":               # 金叉 或 斜率转正(更快回场)
                return cross or rising
            if market_mode == "cross_slope":          # 金叉 且 MA20斜率≥0(大盘走平/回落即暂停新建仓)
                return cross and slope >= 0
            if market_mode == "breadth":              # 宇宙涨跌比(已滞后到T-1)≥阈值才建仓
                return True if breadth_map is None else (breadth_map.get(date_t, breadth_thresh) >= breadth_thresh)
            if market_mode == "cross_or_breadth":     # 金叉 或 宽度转强(更快回场, 治Q4踏空)
                if breadth_map is not None and breadth_map.get(date_t, 0) >= breadth_thresh:
                    return True
                return cross
            if market_mode == "always":               # 无大盘过滤(参照)
                return True
            return cross
        except Exception:
            return True

    def market_cap(date_t):
        """建仓容量系数: 1.0满仓 / 0.5半仓 / 0.0空仓。
        tiered_half: 金叉→满仓; 金叉未到但MA20斜率转正(回升初期,Q4场景)→半仓博反弹;
                     斜率向下(真下跌,如崩盘)→空仓(护盾不变)。其余模式: market_up布尔→1/0。"""
        if market_mode != "tiered_half":
            return 1.0 if market_up(date_t) else 0.0
        sub = mk[mk["d"] < date_t]
        if sub.empty:
            return 1.0
        last = sub.iloc[-1]
        try:
            cross = float(last["ma20"]) > float(last["ma60"])
            if cross:
                return 1.0
            return 0.5 if float(last.get("ma20_slope", 0)) > 0 else 0.0
        except Exception:
            return 1.0

    def daily_asof(code, date_t):
        d = daily_map[code]
        sub = d[d["d"] < date_t]
        return sub if len(sub) >= 25 else None

    # 尾盘建仓模式：把候选改按"信号日"索引（信号日收盘形成信号 → 当日尾盘买）
    cand_tail = {}
    if tail_entry:
        for c, cm in cand_map.items():
            d = daily_map[c]
            sd_map = {}
            for T_act, (sig, asof, score) in cm.items():
                if asof - 1 < len(d):
                    sd = str(d.iloc[asof - 1]["d"])   # 信号形成日
                    sd_map[sd] = (sig, asof, score)
            cand_tail[c] = sd_map

    all_dates = set()
    for c in m5_map:
        all_dates |= set(m5_map[c].keys())
    sim_dates = sorted(d for d in all_dates if start <= d <= end)
    date_idx = {d: i for i, d in enumerate(sim_dates)}   # 交易日序号，用于算持有天数

    cash = float(capital)
    grid = capital / max_pos
    positions: dict[str, dict] = {}
    trades = []
    equity_curve = []
    _stop_cd = {}   # 再入冷却: code -> 最近一次止损出场的交易日序号

    for T in sim_dates:
        cap = market_cap(T)
        mkt_ok = cap > 0
        eff_max = max(1, int(round(max_pos * cap))) if cap > 0 else 0
        # cap_map: 按日期指定持仓数上限(强弱自动减仓回测用)，覆盖 market_mode
        if cap_map is not None:
            eff_max = int(cap_map.get(T, max_pos))
            mkt_ok = eff_max > 0
        # RS-override: 大盘金叉→正常满仓; 大盘弱→不空仓而是降仓(rs_weak_cap)+只放行RS≥阈值的强势股
        _rs_weak = False
        if rs_override > 0:
            _sub = mk[mk["d"] < T]
            _cross = bool(float(_sub.iloc[-1]["ma20"]) > float(_sub.iloc[-1]["ma60"])) if len(_sub) else True
            mkt_ok = True
            eff_max = max_pos if _cross else rs_weak_cap
            _rs_weak = not _cross
        # 见顶防御早警: 大盘连续 topwarn_days 日收盘<MA20(可选+缩量) → 强制停新建仓(比金叉关得早)
        if topwarn_days > 0:
            sub = mk[mk["d"] < T].tail(topwarn_days)
            if len(sub) == topwarn_days:
                below = (sub["close"].astype(float) < sub["ma20"].astype(float)).all()
                shrink = True
                if topwarn_vol and "vol" in sub.columns:
                    v = sub["vol"].astype(float)
                    shrink = bool(v.iloc[-1] < v.mean())   # 末日量低于本段均量=缩量
                if below and shrink:
                    mkt_ok = False; eff_max = 0
        # 当日候选（粘合优先，再评分降序——评分用 analyze 复算一次）
        todays = []
        _src = cand_tail if tail_entry else cand_map
        for c in daily_map:
            cm = _src.get(c) if tail_entry else cand_map[c]
            if cm and T in cm:
                sig, asof, score = cm[T]
                todays.append((c, sig, asof, score))
        # 方案A: MACD柱动能确认作为进场前置条件（off=不启用，与现行等价）
        if macd_confirm != "off":
            todays = [t for t in todays if _macd_ok(t[0], t[2], macd_confirm)]
        # RS-override 弱市段: 只放行 RS(个股20日 − 沪深300) ≥ 阈值的强势龙头
        if _rs_weak:
            todays = [t for t in todays if _rs(t[0], t[2]) >= rs_override]
        # 趋势质量闸: 过滤震荡股(效率系数太低/MA20穿越太多)，防whipsaw
        if er_min > 0:
            todays = [t for t in todays if _er(t[0], t[2]) >= er_min]
        if cross_max > 0:
            todays = [t for t in todays if _ma20_cross(t[0], t[2]) <= cross_max]
        # A: 精选 Top-N —— 按评分降序只保留前 top_n 只（复刻实盘"只买精选"）
        if top_n and top_n > 0 and len(todays) > top_n:
            todays.sort(key=lambda it: -it[3])
            todays = todays[:top_n]
        # B: 买入优先级
        if priority == "score":
            # 按评分降序优先买（评分高者先建仓）
            todays.sort(key=lambda it: -it[3])
        elif priority == "shrink":
            # 多日持续缩量优先（独立条件，隔离其影响）→ 再粘合 → 再盈亏比
            todays.sort(key=lambda it: (0 if _multiday_shrink(it[0], it[2]) else 1,
                                        0 if ("粘合" in it[1] or "发散" in it[1]) else 1,
                                        _risk_dist(it[0], it[1], it[2])))
        elif priority == "rs":
            # 粘合首位 → RS(相对强度)高优先 → 盈亏比（粘合仍最优先，RS作次级排序）
            todays.sort(key=lambda it: (0 if ("粘合" in it[1] or "发散" in it[1]) else 1,
                                        -_rs(it[0], it[2]),
                                        _risk_dist(it[0], it[1], it[2])))
        else:
            # 默认：粘合优先 → 盈亏比(到止损距离)升序
            todays.sort(key=lambda it: (0 if ("粘合" in it[1] or "发散" in it[1]) else 1,
                                        _risk_dist(it[0], it[1], it[2])))

        for i in range(48):
            # ── 出场（先于进场，释放仓位）──
            for code in list(positions):
                pos = positions[code]
                if pos["entry_date"] == T:
                    continue   # T+1
                day = m5_map[code].get(T)
                if not day or i >= len(day):
                    continue
                sub = daily_asof(code, T)
                if sub is None:
                    continue
                bar = day[i]
                price = float(bar[4])
                ts = datetime.strptime(bar[0], "%Y-%m-%d %H:%M:%S")
                last = sub.iloc[-1]
                last_close = float(last["close"]); ma5 = float(last["ma5"])
                chg = (price - last_close) / last_close * 100 if last_close else 0.0
                pnl = (price - pos["cost"]) / pos["cost"] * 100
                pos["peak"] = max(pos["peak"], pnl)
                if ("粘合" in pos["sig"] or "发散" in pos["sig"]) and chg >= 9.0 and not pos["flu"]:
                    pos["flu"] = T
                _update_stop(pos, price, ma5, tiers=tiers, ma5_min=ma5_min)
                runhigh = max(float(day[j][2]) for j in range(i + 1))
                runlow  = min(float(day[j][3]) for j in range(i + 1))
                _CTX["dt"] = ts
                _CTX["mindf"] = pd.DataFrame(
                    {"close": [float(day[j][4]) for j in range(i + 1)]})
                quote = {"price": price, "last_close": last_close, "open": float(day[0][1]),
                         "high": runhigh, "low": runlow, "change_pct": chg, "vol_ratio": 1.0}
                hd = date_idx.get(T, 0) - date_idx.get(pos["entry_date"], 0)
                pos["peak_price"] = max(pos.get("peak_price", price), price)

                def _sell(qty, exit_name, at_price=None):
                    nonlocal cash
                    base = at_price if at_price is not None else price
                    exec_p = round(base * (1 - slippage), 3)
                    gross = exec_p * qty
                    fee = round(gross * (COMMISSION + STAMP_TAX), 2)
                    net = gross - fee
                    cost_amt = pos["cost"] * qty
                    cash += net
                    trades.append({"code": code, "name": names.get(code, code),
                                   "buy_date": pos["entry_date"], "sell_date": T,
                                   "buy": pos["cost"], "sell": exec_p,
                                   "pnl": round(net - cost_amt, 2),
                                   "pnl_pct": round((net - cost_amt) / cost_amt * 100, 2),
                                   "sig": pos["sig"], "exit": exit_name, "bar": i})
                    if "STOP" in exit_name:   # 止损出场 → 记录冷却起点
                        _stop_cd[code] = date_idx.get(T, 0)

                # 盘中最低价口径：本根高点棘轮抬止损，低点触及即按止损价成交
                # （贴近实盘10秒轮询的瞬时触发，能捕捉"涨5%后回踩到成本被震出"）
                if stop_on_low:
                    _update_stop(pos, float(bar[2]), ma5, tiers=tiers, ma5_min=ma5_min)
                    if float(bar[3]) < pos["stop"]:
                        _sell(pos["shares"], "SELL_STOP_LOW", at_price=pos["stop"])
                        del positions[code]
                        continue

                # 分批止盈：达到 scale_pct 先卖一半，剩余继续按规则跑
                if scale_pct > 0 and not pos["scaled"] and pnl >= scale_pct:
                    half = (pos["shares"] // 200) * 100
                    if half >= 100:
                        _sell(half, "SCALE_OUT")
                        pos["shares"] -= half
                        pos["scaled"] = True

                # 方案B: MACD顶背离 → 减半仓(half)/清仓(full)。日级信号,每仓一生只触发一次。
                if top_div != "off" and not pos.get("div_cut") and _top_div(sub):
                    pos["div_cut"] = True
                    if top_div == "full":
                        _sell(pos["shares"], "TOP_DIV"); del positions[code]; continue
                    half = (pos["shares"] // 200) * 100
                    if half >= 100:
                        _sell(half, "TOP_DIV_HALF"); pos["shares"] -= half

                # 止损/止盈决策
                sold = False
                if atr_mult > 0 and pos["atr"] > 0:
                    chand = pos["peak_price"] - atr_mult * pos["atr"]
                    if price <= chand:
                        _sell(pos["shares"], "ATR_STOP"); del positions[code]; sold = True
                if not sold:
                    sig = IE.check_position(code, sub, quote, cost=pos["cost"],
                                            stop_price=pos["stop"], peak_pnl=pos["peak"],
                                            entry_signal=pos["sig"], first_limit_up_date=pos["flu"],
                                            hold_days=hd)
                    # ATR 模式下止损由吊灯接管，只采纳 check_position 的止盈
                    take = (sig.action == Action.SELL_PROFIT) if atr_mult > 0 \
                           else (sig.action in (Action.SELL_STOP, Action.SELL_PROFIT))
                    # 开盘缓冲: 前 open_buffer 根5分钟不执行止损(让开盘跳空企稳)，止盈不受限
                    if open_buffer > 0 and i < open_buffer and sig.action == Action.SELL_STOP:
                        take = False
                    if take:
                        is_limit = "涨停" in (getattr(sig, "reason", "") or "")
                        if is_limit and limit_lock == "disable":
                            pass            # ① 不锁利：忽略涨停止盈，交给追踪止损管理
                        elif is_limit and limit_lock == "half":
                            if not pos.get("ll_half"):   # ② 减半留底仓(只减一次)
                                _half = (pos["shares"] // 200) * 100
                                if _half >= 100:
                                    _sell(_half, "LIMIT_LOCK_HALF"); pos["shares"] -= _half
                                pos["ll_half"] = True
                            # 留底仓不del，继续由追踪止损/其它规则管理
                        else:
                            _nm = "LIMIT_LOCK" if is_limit else sig.action.name
                            _sell(pos["shares"], _nm); del positions[code]

            # ── 进场 ──（尾盘模式跳过盘中进场，改在日终统一尾盘买）
            if mkt_ok and not tail_entry:
                for code, stype, asof, _score in todays:
                    if code in positions or len(positions) >= eff_max:
                        continue
                    if reentry_cd > 0 and code in _stop_cd and date_idx.get(T, 0) - _stop_cd[code] < reentry_cd:
                        continue   # 再入冷却: 止损后N日内不回购同股
                    day = m5_map[code].get(T)
                    if not day or i >= len(day):
                        continue
                    sub = daily_map[code].iloc[:asof]
                    bar = day[i]
                    price = float(bar[4])
                    ts = datetime.strptime(bar[0], "%Y-%m-%d %H:%M:%S")
                    last_close = float(sub.iloc[-1]["close"])
                    chg = (price - last_close) / last_close * 100 if last_close else 0.0
                    runhigh = max(float(day[j][2]) for j in range(i + 1))
                    runlow  = min(float(day[j][3]) for j in range(i + 1))
                    _CTX["dt"] = ts
                    _CTX["mindf"] = pd.DataFrame(
                        {"close": [float(day[j][4]) for j in range(i + 1)]})
                    vr = vol_ratio(code, T, i, day)
                    quote = {"price": price, "last_close": last_close, "open": float(day[0][1]),
                             "high": runhigh, "low": runlow, "change_pct": chg, "vol_ratio": vr}
                    sig = IE.check_entry(code, sub, quote, signal_type=stype, market_chg=0.0)
                    if sig.action == Action.BUY:
                        exec_p = round(price * (1 + slippage), 3)
                        slot = grid * _sig_mult(stype)
                        shares = int(slot / exec_p / 100) * 100
                        if shares < 100:
                            continue
                        amt = exec_p * shares
                        fee = round(amt * COMMISSION, 2)
                        if cash < amt + fee:
                            continue
                        cash -= amt + fee
                        positions[code] = {"cost": exec_p, "shares": shares,
                                           "stop": round(exec_p * 0.95, 2), "peak": 0.0,
                                           "sig": stype, "entry_date": T, "flu": "",
                                           "atr": _atr(sub), "peak_price": exec_p,
                                           "scaled": False}

        # ── 尾盘建仓：信号日收盘形成信号 → 当日最后一根5分钟K(尾盘)买入 ──
        # （避开次日跳空买不进；信号已在收盘成立，直接按收盘价建仓）
        if tail_entry and mkt_ok:
            for code, stype, asof, _score in todays:
                if code in positions or len(positions) >= eff_max:
                    continue
                day = m5_map[code].get(T)
                if not day:
                    continue
                price = float(day[-1][4])           # 当日尾盘收盘价
                exec_p = round(price * (1 + slippage), 3)
                slot = grid * _sig_mult(stype)
                shares = int(slot / exec_p / 100) * 100
                if shares < 100:
                    continue
                amt = exec_p * shares
                fee = round(amt * COMMISSION, 2)
                if cash < amt + fee:
                    continue
                cash -= amt + fee
                sub = daily_map[code].iloc[:asof]
                positions[code] = {"cost": exec_p, "shares": shares,
                                   "stop": round(exec_p * 0.95, 2), "peak": 0.0,
                                   "sig": stype, "entry_date": T, "flu": "",
                                   "atr": _atr(sub), "peak_price": exec_p, "scaled": False}

        # 当日收盘市值（按各股当日最后一根5分钟收盘）
        pv = 0.0
        for code, pos in positions.items():
            day = m5_map[code].get(T)
            px = float(day[-1][4]) if day else pos["cost"]
            pv += px * pos["shares"]
        equity_curve.append((T, round(cash + pv, 2)))

    return trades, equity_curve, positions


def metrics(trades, equity_curve, positions, capital, mk) -> dict:
    """汇总指标字典（供 _report 与 sweep 共用）"""
    if not equity_curve:
        return {}
    final_eq = equity_curve[-1][1]
    ret = (final_eq - capital) / capital * 100
    peak = capital; mdd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak * 100)
    wins = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]
    avg_w = statistics.mean([t["pnl_pct"] for t in wins]) if wins else 0.0
    avg_l = statistics.mean([t["pnl_pct"] for t in losers]) if losers else 0.0
    seg = mk[(mk["d"] >= equity_curve[0][0]) & (mk["d"] <= equity_curve[-1][0])]
    bench = ((float(seg.iloc[-1]["close"]) - float(seg.iloc[0]["close"]))
             / float(seg.iloc[0]["close"]) * 100) if len(seg) else 0.0
    from collections import Counter
    return {"ret": round(ret, 2), "mdd": round(mdd, 2),
            "calmar": round(ret / mdd, 2) if mdd else 0.0,
            "trades": len(trades),
            "win": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "pnl_ratio": round(abs(avg_w / avg_l), 2) if avg_l else 0.0,
            "alpha": round(ret - bench, 2), "bench": round(bench, 2),
            "open_pos": len(positions),
            "exits": dict(Counter(t["exit"] for t in trades))}


def _report(m, equity_curve):
    print("\n" + "=" * 66)
    print("  Phase 2 忠实回测结果（分钟级回放实盘进出场）")
    print("=" * 66)
    if not m:
        print("  无交易日数据"); return
    print(f"  区间: {equity_curve[0][0]} ~ {equity_curve[-1][0]} ({len(equity_curve)}日)")
    print(f"  总收益: {m['ret']:+.2f}%   最大回撤: {m['mdd']:.2f}%   卡玛: {m['calmar']}")
    print(f"  交易: {m['trades']}笔  胜率: {m['win']}%  盈亏比: {m['pnl_ratio']}  未平仓: {m['open_pos']}只")
    print(f"  出场分布: {m['exits']}")
    print(f"  沪深300同期: {m['bench']:+.2f}%   超额Alpha: {m['alpha']:+.2f}%")
    print("\n⚠️ 量比已按5分钟量重建 / 相对强弱跳过 / 优先级简化——进出场时点为真实分钟级。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", default="2025-05-22")
    ap.add_argument("--end", default="2026-06-02")
    ap.add_argument("--max-pos", type=int, default=6)
    ap.add_argument("--capital", type=float, default=200_000)
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--top-n", type=int, default=8, help="每日精选上限(0=不限)")
    ap.add_argument("--atr-mult", type=float, default=0.0, help="ATR吊灯止损倍数(0=关,用现行MA20止损)")
    ap.add_argument("--scale-pct", type=float, default=0.0, help="分批止盈:浮盈达此%先卖半仓(0=关)")
    a = ap.parse_args()

    universe = build_universe(a.sample, a.seed)
    names = {s["code"]: s["name"] for s in universe}
    print(f"忠实回测：{len(names)} 只 | {a.start}~{a.end} | {a.max_pos}仓 | "
          f"精选Top{a.top_n} | 滑点{a.slippage*100:.1f}%")
    ctx = load_ctx(list(names))
    print(f"  有效数据: {len(ctx['daily'])} 只")
    trades, ec, pos = simulate(ctx, names, a.start, a.end, a.max_pos, a.capital,
                               a.slippage, top_n=a.top_n,
                               atr_mult=a.atr_mult, scale_pct=a.scale_pct)
    _report(metrics(trades, ec, pos, a.capital, ctx["mk"]), ec)


if __name__ == "__main__":
    main()

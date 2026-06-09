"""
主监控引擎
- 持仓表管理
- 每30秒轮询报价
- 触发信号 → 打印 / 推送 / 可接自动下单
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from data.fetcher import db
from strategy.signal_520 import strategy, Signal

# 各信号仓位倍数（相对基准格 = 总资金 / max_positions）
# 粘合发散最早期信号给满1.5格，金叉标准1格，回踩信心稍低给0.7格（可后续加仓）
# 以 200K / 6 = 33K 基准格为例：粘合≈50K / 金叉≈33K / 回踩≈23K
SIGNAL_SIZE = {
    Signal.BUY_GOLDEN_CROSS: 1.0,   # 金叉：1格（基准）
    Signal.BUY_PULLBACK:     1.0,   # 回踩：1格（与金叉同等，信号最常见）
    Signal.BUY_SQUEEZE:      1.5,   # 粘合：1.5格（重点仓位）
}

# 分批止盈：浮盈达此值先卖半仓、剩余继续按规则跑（回测跨样本4/4验证有效）
SCALE_PROFIT_PCT = 12.0
from monitor.realtime import get_quotes, is_trading_time, is_market_open
from monitor.intraday import engine as intraday_engine, Action
from alert.notifier import (log, notify_buy, notify_sell, notify_warning,
                            notify_daily_summary, notify_stop_raised)
from trader.paper import PaperAccount


# ── 数据结构 ──────────────────────────────────────────

@dataclass
class Position:
    """持仓记录"""
    code:         str
    name:         str
    cost:         float
    shares:       int
    stop_price:   float
    entry_time:   str   = ""
    hold_days:    int   = 0
    peak_pnl:           float = 0.0   # 历史最高盈利%，用于回落保护止盈
    entry_signal:       str   = ""    # 买入触发信号（粘合/金叉/回踩），用于差异化止盈策略
    first_limit_up_date: str  = ""    # 粘合发散：首次当日涨幅≥9%的日期（YYYY-MM-DD）；空=未出现过
    scaled:             bool  = False # 是否已分批止盈卖出过半仓

    @property
    def market_value(self) -> float:
        return self.cost * self.shares   # 实时更新在外层

    def pnl(self, price: float) -> float:
        return (price - self.cost) * self.shares

    def pnl_pct(self, price: float) -> float:
        return (price - self.cost) / self.cost * 100


_PRIORITY_KEY = {"P1": 1, "P2": 2, "P3": 3}   # 数字越小越优先


@dataclass
class WatchItem:
    """候选股（日线已触发买点，等日内确认）"""
    code:       str
    name:       str
    signal:     str
    daily_df:   pd.DataFrame
    priority:   str = ""    # "P1" / "P2" / "P3"；空字符串 = 扫描器自动加入，排最后
    score:      int = 0     # 信号评分（来自扫描结果），用于购买优先级排序
    added_time: str = ""

    def __post_init__(self):
        if not self.added_time:
            self.added_time = datetime.now().strftime("%H:%M:%S")


# ── 主引擎 ────────────────────────────────────────────

class MonitorEngine:

    def __init__(self, interval: int = 30, paper_mode: bool = True,
                 user: str | None = None):
        self.interval       = interval      # 轮询间隔（秒）
        self.paper_mode     = paper_mode    # True=模拟交易 / False=实盘（需接券商）
        self.user           = user          # 该引擎服务的用户（数据隔离）
        self.max_positions  = 4             # 最多同时持仓4只（寻优跨牛熊验证：降回撤）
        self.init_capital   = 200_000.0     # 总资金，用于计算每仓金额
        self.positions:  dict[str, Position]  = {}
        self.watchlist:  dict[str, WatchItem] = {}
        self._running    = False
        self._lock       = threading.Lock()
        self._broker     = None          # 可注入券商接口
        self._paper      = PaperAccount(user=user) if paper_mode else None
        self._warn_cooldown: dict[str, float] = {}   # code -> 上次预警时间戳
        self._amp_dead: set[str] = set()  # 当日因振幅出局的票（振幅不可逆，整日跳过）
        self._amp_dead_date = ""          # _amp_dead 所属日期，跨日清空
        self._wl_sig = None               # 自选表签名，用于盘中检测变化即时重载

    # ── 持仓管理 ──────────────────────────────────

    def add_position(self, code: str, cost: float, shares: int,
                     name: str = "", stop_price: float = 0.0,
                     entry_signal: str = ""):
        """手动添加持仓（或自动买入后调用）"""
        if not stop_price:
            daily_df  = db.get(code)
            stop_price = round(float(daily_df.iloc[-1]["ma5"]) * 0.97, 2) \
                         if not daily_df.empty else round(cost * 0.95, 2)
        with self._lock:
            self.positions[code] = Position(
                code=code, name=name or code,
                cost=cost, shares=shares,
                stop_price=stop_price,
                entry_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
                entry_signal=entry_signal,
            )
        log(f"持仓录入: {name}({code}) 成本={cost} 数量={shares} 止损={stop_price}", "INFO")

    def remove_position(self, code: str):
        with self._lock:
            self.positions.pop(code, None)

    @staticmethod
    def _is_bought_today(pos: "Position") -> bool:
        """T+1：判断持仓是否为当日买入（当日买入不可当日卖出）"""
        et = getattr(pos, "entry_time", "") or ""
        return et[:10] == datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _risk_distance_pct(item: "WatchItem", price: float) -> float:
        """
        盈亏比代理——候选买入的"到止损距离%"，越小=止损越紧=风险越低=优先级越高。
        止损参考（与各信号的技术止损一致）：
          回踩 / 粘合发散 → MA20（跌破即破位）
          金叉 / 默认     → MA5 × 0.97
        无法计算或价格已在止损参考下方 → 返回大值，排到最后（check_entry 也会拦）。
        """
        if price <= 0 or item is None or getattr(item, "daily_df", None) is None:
            return 9999.0
        try:
            last = item.daily_df.iloc[-1]
            ma20 = float(last["ma20"])
            ma5  = float(last["ma5"])
        except Exception:
            return 9999.0
        sig = item.signal or ""
        stop_ref = ma20 if ("粘合" in sig or "发散" in sig or "回踩" in sig) else ma5 * 0.97
        if stop_ref <= 0 or stop_ref >= price:
            return 9999.0
        return (price - stop_ref) / price * 100.0

    def add_watch(self, code: str, name: str, signal: str,
                  priority: str = "", score: int = 0):
        """加入候选股监控
        priority: "P1" / "P2" / "P3"（手动 WATCHLIST 设置）；
                  空字符串 = 扫描器自动发现，排在所有手动股票之后
        score:    信号评分（0-100），用于购买优先级排序（粘合发散优先，再按分数高低）
        """
        daily_df = db.get(code)
        with self._lock:
            self.watchlist[code] = WatchItem(
                code=code, name=name, signal=signal,
                daily_df=daily_df, priority=priority, score=score,
            )
        tag = priority if priority else "自动"
        log(f"候选股加入: {name}({code}) 信号={signal} 评分={score} [{tag}]", "INFO")

    def remove_watch(self, code: str):
        with self._lock:
            self.watchlist.pop(code, None)

    def load_from_paper(self):
        """从 paper 账户同步持仓（服务启动时调用，替代硬编码 POSITIONS）"""
        if not (self.paper_mode and self._paper):
            return
        paper_pos = self._paper.positions()
        if not paper_pos:
            log("paper 账户当前无持仓，以空仓启动", "INFO")
            return
        for code, p in paper_pos.items():
            with self._lock:
                self.positions[code] = Position(
                    code=code,
                    name=p.name,
                    cost=p.cost,
                    shares=p.shares,
                    stop_price=p.stop_price,
                    entry_time=getattr(p, "entry_time", ""),
                    entry_signal=getattr(p, "entry_signal", ""),
                    scaled=bool(getattr(p, "scaled", 0)),
                )
            log(f"加载持仓: {p.name}({code}) 成本={p.cost} "
                f"止损={p.stop_price} {p.shares}股", "INFO")
        log(f"共加载 {len(paper_pos)} 只持仓", "INFO")

    def load_watchlist_from_paper(self):
        """从该用户自己的账户库加载自选股（评分取自共享扫描结果）。
        已持仓的股票不重复加入候选。"""
        if not (self.paper_mode and self._paper):
            return
        scan_data  = self._paper.get_scan_results()   # 共享 market.db
        scan_score = {r["code"]: int(r.get("score") or 0)
                      for r in (scan_data.get("results") or [])}
        rows = self._paper.get_watchlist()
        table_codes = {w["code"] for w in rows}
        loaded = 0
        for w in rows:
            code = w["code"]
            if code in self.positions:
                continue   # 已持仓，不再作为候选
            self.add_watch(
                code=code, name=w["name"],
                signal=w.get("signal", "候选"),
                priority=w.get("priority", ""),
                score=scan_score.get(code, 0),
            )
            loaded += 1
        # 剪枝：把内存候选集对齐成「表中且未持仓」——移除已从表删除的旧候选
        # （手动删自选 / 候选被取消），以及已转持仓的残留。否则只增不删会残留。
        with self._lock:
            stale = [c for c in self.watchlist
                     if c not in table_codes or c in self.positions]
            for c in stale:
                self.watchlist.pop(c, None)
        if stale:
            log(f"[{self.user or '默认'}] 移除失效候选 {len(stale)} 只: "
                f"{','.join(stale)}", "INFO")
        self._wl_sig = self._wl_sig_of(rows)   # 记录本次加载后的表签名
        if loaded:
            log(f"[{self.user or '默认'}] 加载自选 {loaded} 只", "INFO")

    @staticmethod
    def _wl_sig_of(rows) -> tuple:
        """自选表签名：(code, signal, priority) 排序元组。
        增/删/改优先级/改信号都会改变签名，用于盘中检测表变化。"""
        return tuple(sorted(
            (w["code"], w.get("signal", "") or "", w.get("priority", "") or "")
            for w in rows
        ))

    def _current_wl_sig(self) -> tuple:
        return self._wl_sig_of(self._paper.get_watchlist())

    # ── 核心轮询 ──────────────────────────────────

    def _tick(self):
        """单次轮询：获取报价 → 检查信号"""
        # 跨日清空"振幅出局"名单（振幅当日不可逆，但隔日重新评估）
        _today = datetime.now().strftime("%Y-%m-%d")
        if self._amp_dead_date != _today:
            self._amp_dead = set()
            self._amp_dead_date = _today

        # 候选表变化即时重载：Web 手动加/删候选后，≤1个tick(约10s)内自动纳入监控，
        # 无需等次日09:15或盘后扫描。签名比对，仅在真变化时重载（无日志刷屏）。
        if self.paper_mode and self._paper:
            try:
                if self._current_wl_sig() != self._wl_sig:
                    log(f"[{self.user or '默认'}] 检测到自选变化，重载候选", "INFO")
                    self.load_watchlist_from_paper()   # 内部更新 _wl_sig
            except Exception as e:
                log(f"[{self.user or '默认'}] 自选变化检测失败: {e}", "WARN")

        with self._lock:
            pos_codes   = list(self.positions.keys())
            watch_codes = [c for c in self.watchlist.keys() if c not in self._amp_dead]

        # 510300（沪深300 ETF）随报价一并拉取，用于日内大盘监控
        all_codes = list(set(pos_codes + watch_codes + ["510300"]))
        if not all_codes:
            return

        quotes = get_quotes(all_codes)
        if not quotes:
            log("报价获取失败，跳过本轮", "WARN")
            return

        # 大盘日内急跌保护：跌幅 > 2% 时只平仓不建仓
        mkt_q = quotes.get("510300", {})
        mkt_chg = mkt_q.get("change_pct", 0) or 0
        market_hard_down = mkt_chg < -2.0
        if market_hard_down:
            log(f"⚠️ 大盘日内跌{mkt_chg:.1f}%，只平仓不建仓", "WARN")

        ts = datetime.now().strftime("%H:%M:%S")

        # ── 持仓检查 ──
        for code in pos_codes:
            quote = quotes.get(code)
            pos   = self.positions.get(code)
            if not quote or not pos:
                continue

            price = quote["price"]

            # 跌停板检测：无法成交，跳过本轮（止损指令也无法执行）
            last_close = quote.get("last_close", 0)
            if last_close > 0 and price <= last_close * 0.901:
                log(f"⚠️ {pos.name}({code}) 跌停板，止损指令跳过，等待明日", "WARN")
                continue
            with self._lock:
                daily_df = db.get(code)

            # 更新历史最高盈利（用于回落保护止盈）
            pnl_pct = pos.pnl_pct(price)
            with self._lock:
                if code in self.positions:
                    self.positions[code].peak_pnl = max(
                        self.positions[code].peak_pnl, pnl_pct
                    )
                    pos = self.positions[code]   # 取最新引用

            # 粘合发散：记录首次当日涨幅≥9%的日期
            # 必须在 check_position 之前完成，使当日首涨停不触发锁利
            if ("粘合" in pos.entry_signal or "发散" in pos.entry_signal):
                chg_now = float(quote.get("change_pct", 0.0) or 0.0)
                if chg_now >= 9.0 and not pos.first_limit_up_date:
                    _today = datetime.now().strftime("%Y-%m-%d")
                    with self._lock:
                        if code in self.positions:
                            self.positions[code].first_limit_up_date = _today
                            pos = self.positions[code]
                    log(f"📌 {pos.name}({code}) 粘合发散首涨停日={_today}，今日持有观察", "INFO")

            # 分批止盈：浮盈达 SCALE_PROFIT_PCT 先卖半仓，剩余继续按规则跑
            # （次日起执行，受 T+1 约束；每仓只分批一次）
            if (not pos.scaled and not self._is_bought_today(pos)
                    and pnl_pct >= SCALE_PROFIT_PCT and self.paper_mode and self._paper):
                half = (pos.shares // 200) * 100   # 卖一半，取整到100股
                if half >= 100:
                    ok, msg = self._paper.sell(
                        code, price, qty=half,
                        signal=f"分批止盈+{SCALE_PROFIT_PCT:.0f}%(卖半仓)")
                    if ok:
                        log(f"[分批] {msg}", "SELL")
                        with self._lock:
                            if code in self.positions:
                                self.positions[code].shares -= half
                                self.positions[code].scaled = True
                                pos = self.positions[code]

            sig = intraday_engine.check_position(
                code, daily_df, quote,
                cost=pos.cost, stop_price=pos.stop_price,
                peak_pnl=pos.peak_pnl, entry_signal=pos.entry_signal,
                first_limit_up_date=pos.first_limit_up_date,
            )

            if sig.action in (Action.SELL_STOP, Action.SELL_PROFIT):
                if self._is_bought_today(pos):
                    # T+1：当日买入不可当日卖，记录信号但不执行，次日交易日再处理
                    log(f"{pos.name}({code}) 触发卖出信号但 T+1 锁定（当日买入），"
                        f"明日再执行 | {sig.reason}", "INFO")
                else:
                    self._do_sell(pos, price, sig.reason, conditions=sig.conditions)  # notify_sell 移至内部，仅成功才推送
            else:
                pnl = pos.pnl_pct(price)
                if pos.stop_price and price > 0:
                    gap_pct = (price - pos.stop_price) / price * 100
                log(f"{pos.name}({code}) {price:.2f} | "
                    f"盈亏={pnl:+.1f}% | 峰值={pos.peak_pnl:.1f}% | {sig.reason}")

        # ── 追踪止损更新（持仓检查后执行，确保未被卖出的仓位才更新）──
        self._update_trailing_stops(quotes)

        # ── 候选股检查（先做大盘过滤）──
        # 大盘 MA20 > MA60 才允许新建仓（与回测逻辑保持一致）
        # 与个股520战法同逻辑：要求指数自身也处于金叉（短期均线在中期均线上方）
        market_up = True
        try:
            market_df = db.get_market(bars=80)   # MA60 需要至少 60 根
            if not market_df.empty and len(market_df) >= 60:
                import pandas as _pd
                last_mrow  = market_df.iloc[-1]
                m_close    = float(last_mrow["close"])
                m_ma60     = last_mrow.get("ma60", float("nan"))
                m_ma20 = last_mrow.get("ma20", float("nan"))
                if (not _pd.isna(m_ma20) and not _pd.isna(m_ma60)
                        and float(m_ma60) > 0):
                    # 大盘金叉：MA20 > MA60（与个股520战法同逻辑）
                    market_up = float(m_ma20) > float(m_ma60)
                    if not market_up:
                        log(f"⚠️ 大盘MA20({float(m_ma20):.3f}) < MA60({float(m_ma60):.3f})，"
                            f"指数死叉，今日暂停新建仓", "INFO")
                else:
                    # MA60 数据不足时降级用 MA20 方向
                    market_up = strategy.ma20_direction(market_df) == "up"
                    if not market_up:
                        log("⚠️ 大盘MA20未向上（MA60数据不足），今日暂停新建仓", "INFO")
        except Exception as _e:
            log(f"大盘数据获取失败，默认允许建仓: {_e}", "WARN")

        # 购买优先级：① 手动优先级 P1/P2/P3 最优先（你指定的先占坑；未标的排最后）
        #             ② 其次粘合发散优先（突破蓄势，信号最强）
        #             ③ 再按【盈亏比】：到止损距离越小=止损越紧=风险越低，优先买
        #             ④ 最后以评分兜底
        def _watch_sort_key(c: str):
            item = self.watchlist.get(c)
            if not item:
                return (99, 1, 9999.0, 0)
            pri        = _PRIORITY_KEY.get(item.priority, 99)   # P1=1<P2=2<P3=3<未标=99
            is_squeeze = 0 if ("粘合" in item.signal or "发散" in item.signal) else 1
            q     = quotes.get(c) or {}
            price = q.get("price", 0) or 0
            risk  = self._risk_distance_pct(item, price)   # 越小越优先
            return (pri, is_squeeze, risk, -item.score)

        watch_codes_sorted = sorted(watch_codes, key=_watch_sort_key)

        if watch_codes_sorted:
            top_items = [
                f"{self.watchlist[c].name}({c})"
                f"[{self.watchlist[c].priority or '自动'} "
                f"{'粘合' if '粘合' in self.watchlist[c].signal else self.watchlist[c].signal[:2]}"
                f" 止损距{self._risk_distance_pct(self.watchlist[c], (quotes.get(c) or {}).get('price', 0) or 0):.1f}%"
                f" 分{self.watchlist[c].score}]"
                for c in watch_codes_sorted if c in self.watchlist
            ]
            log(f"候选股检查: 共{len(watch_codes_sorted)}只 | "
                f"market_up={market_up} | hard_down={market_hard_down} | "
                f"持仓{len(self.positions)}/{self.max_positions}", "INFO")
            log(f"  检查顺序: {' → '.join(top_items)}", "INFO")

        for code in watch_codes_sorted:
            quote = quotes.get(code)
            item  = self.watchlist.get(code)
            if not quote or not item:
                log(f"候选 {code} 报价缺失，跳过", "WARN")
                continue

            # 只在正式开盘后检查入场
            if not is_market_open():
                continue

            # 大盘趋势或日内急跌 → 跳过入场
            if not market_up:
                log(f"候选 {item.name}({code}) ⛔ 大盘MA60过滤，暂不入场")
                continue
            if market_hard_down:
                log(f"候选 {item.name}({code}) ⛔ 大盘急跌{mkt_chg:.1f}%，暂不入场")
                continue

            sig = intraday_engine.check_entry(code, item.daily_df, quote,
                                              signal_type=item.signal,
                                              market_chg=mkt_chg)

            if sig.action == Action.BUY:
                if len(self.positions) >= self.max_positions:
                    log(f"候选 {item.name}({code}) ⛔ 已达最大持仓{self.max_positions}只，跳过")
                    continue
                price  = quote["price"]
                shares = self._calc_shares(price, signal_type=item.signal)
                self._do_buy(code, item.name, price, shares, sig.reason,
                            signal_type=item.signal, conditions=sig.conditions)
            else:
                # 振幅不可逆（当日最高/最低只会越拉越开）→ 一旦因振幅出局，整日不会再回到买点，
                # 标记后续轮询直接跳过；其余可逆条件（偏离/跌破MA20等）不标记，每轮照常重判。
                if any(c[0] == "当日振幅" and not c[1] for c in (sig.conditions or [])):
                    self._amp_dead.add(code)
                    log(f"候选 {item.name}({code}) 振幅出局，今日不再检查 | {sig.reason}")
                else:
                    log(f"候选 {item.name}({code}) {quote['price']:.2f} | {sig.reason}")

    def _do_buy(self, code: str, name: str, price: float,
                shares: int, reason: str, signal_type: str = "",
                conditions: list = None):
        """执行买入（可接券商API / 模拟账户）"""
        stop_price = round(price * 0.95, 2)
        entry_signal = f"{signal_type} | {reason}" if signal_type else reason

        if self.paper_mode and self._paper:
            ok, msg = self._paper.buy(
                code=code, name=name, price=price, shares=shares,
                signal=reason, stop_price=stop_price,
                entry_signal=entry_signal,
                conditions=conditions,
            )
            if ok:
                log(f"[模拟] {msg}", "BUY")
            else:
                log(f"[模拟] 买入失败: {msg}", "WARN")
                return
        elif self._broker:
            try:
                result = self._broker.buy(code, price, shares)
                log(f"下单成功: {result}", "BUY")
            except Exception as e:
                log(f"下单失败: {e}", "ERR")
                return

        self.add_position(
            code=code, cost=price, shares=shares, name=name,
            stop_price=stop_price, entry_signal=entry_signal,
        )
        self.remove_watch(code)

    def _do_sell(self, pos: Position, price: float, reason: str, conditions: list = None):
        """执行卖出（可接券商API / 模拟账户）"""
        if self.paper_mode and self._paper:
            ok, msg = self._paper.sell(
                code=pos.code, price=price, signal=reason,
                conditions=conditions,
            )
            if ok:
                log(f"[模拟] {msg}", "SELL")
            else:
                log(f"[模拟] 卖出失败: {msg}", "WARN")
                return   # 执行失败 → 不推通知，不移除持仓
        elif self._broker:
            try:
                result = self._broker.sell(pos.code, price, pos.shares)
                log(f"卖出下单成功: {result}", "INFO")
            except Exception as e:
                log(f"卖出下单失败: {e}", "ERR")
                return
        # 执行成功后才推企微通知并从内存移除
        notify_sell(pos.code, pos.name, price, pos.shares, pos.cost, reason)
        self.remove_position(pos.code)

    def _calc_shares(self, price: float, signal_type: str = "") -> int:
        """
        差异化仓位计算：基准格 × 信号倍数
        基准格 = 总资金 / max_positions
        粘合发散 1.5格 / 金叉 1格 / 回踩 0.7格
        """
        base_slot = self.init_capital / self.max_positions
        if "粘合" in signal_type or "发散" in signal_type:
            mult = SIGNAL_SIZE[Signal.BUY_SQUEEZE]
        elif "回踩" in signal_type:
            mult = SIGNAL_SIZE[Signal.BUY_PULLBACK]
        else:
            mult = SIGNAL_SIZE[Signal.BUY_GOLDEN_CROSS]
        slot_cap = base_slot * mult
        shares   = int(slot_cap / price / 100) * 100
        return max(100, shares)

    # ── 追踪止损 ──────────────────────────────────────

    # 关键档位：(最小浮盈%, 止损锁定描述, 止损倍数_相对成本)
    # ≥10% 档实际取 max(成本×倍数, MA5×0.97)，趋势中跟随 MA5；下列为"保底地板"
    _TRAIL_TIERS = [
        (30.0, "盈利超30%，锁定+20%保底",  1.20),
        (20.0, "盈利超20%，锁定+13%保底",  1.13),
        (10.0, "盈利超10%，锁定+5%保底",   1.05),
        ( 5.0, "盈利超5%，止损移至保本",    1.002),  # 1.002 覆盖手续费
    ]

    def _update_trailing_stops(self, quotes: dict):
        """
        遍历所有持仓，根据当前价格动态上调止损线。
        规则：止损线只升不降；触发关键档位时推送通知。
        """
        with self._lock:
            pos_snapshot = list(self.positions.items())

        for code, pos in pos_snapshot:
            quote = quotes.get(code)
            if not quote:
                continue
            price = quote.get("price", 0)
            if price <= 0 or pos.cost <= 0:
                continue

            gain_pct = (price - pos.cost) / pos.cost * 100

            # 获取 MA5 作为辅助参考（取当日日线最后一行）
            try:
                daily_df = db.get(code)
                ma5 = float(daily_df.iloc[-1]["ma5"]) if not daily_df.empty else 0.0
            except Exception:
                ma5 = 0.0

            # 根据浮盈档位计算候选止损
            candidate_stop = pos.stop_price   # 默认不变
            milestone_label = ""

            for min_gain, label, cost_mult in self._TRAIL_TIERS:
                if gain_pct >= min_gain:
                    base = round(pos.cost * cost_mult, 2)
                    # 浮盈≥10% 时，还与 MA5×0.97 取较高值（跟住均线）
                    if min_gain >= 10.0 and ma5 > 0:
                        candidate_stop = max(base, round(ma5 * 0.97, 2))
                    else:
                        candidate_stop = base
                    milestone_label = label
                    break   # 命中最高档即停

            # 止损线只升不降
            if candidate_stop <= pos.stop_price:
                continue

            old_stop = pos.stop_price

            # 判断是否跨越了关键里程碑（用于推送通知，避免每 tick 都推）
            crossed_milestone = self._crossed_key_level(
                old_stop, candidate_stop, pos.cost
            )

            # 更新内存持仓
            with self._lock:
                if code in self.positions:
                    self.positions[code].stop_price = candidate_stop

            # 更新模拟账户持久化
            if self.paper_mode and self._paper:
                self._paper.update_stop(code, candidate_stop)

            log(f"🔒 追踪止损 {pos.name}({code}) "
                f"{old_stop:.2f} → {candidate_stop:.2f} "
                f"（浮盈{gain_pct:+.1f}%）", "INFO")


    @staticmethod
    def _crossed_key_level(old_stop: float, new_stop: float, cost: float) -> bool:
        """
        判断止损线是否跨越了关键里程碑，决定是否触发推送通知。
        里程碑：保本(×1.002)、+5%(×1.05)、+13%(×1.13)、+20%(×1.20)
        """
        milestones = [cost * 1.002, cost * 1.05, cost * 1.13, cost * 1.20]
        for m in milestones:
            if old_stop < m <= new_stop:
                return True
        return False

    # ── 启动/停止 ──────────────────────────────────

    def send_daily_summary(self):
        """15:30 收盘汇总推送"""
        codes  = list(self.positions.keys())
        watch_codes = list(self.watchlist.keys())
        all_codes   = list(set(codes + watch_codes))
        quotes = get_quotes(all_codes) if all_codes else {}

        # ── 持仓明细 ──
        pos_list = []
        for code, pos in self.positions.items():
            price = quotes.get(code, {}).get("price", pos.cost)
            pos_list.append({
                "code":       code,
                "name":       pos.name,
                "cost":       pos.cost,
                "price":      price,
                "shares":     pos.shares,
                "pnl":        round(pos.pnl(price), 0),
                "pnl_pct":    round(pos.pnl_pct(price), 2),
                "stop_price": pos.stop_price,
            })

        # ── 候选股 ──
        sig_list = []
        for code, item in self.watchlist.items():
            price = quotes.get(code, {}).get("price", 0)
            sig_list.append({
                "code":   code,
                "name":   item.name,
                "price":  price,
                "signal": item.signal,
            })

        # ── 账户概览 ──
        account = None
        if self.paper_mode and self._paper:
            current_prices = {
                c: quotes.get(c, {}).get("price", 0)
                for c in codes if quotes.get(c, {}).get("price")
            }
            account = self._paper.summary(current_prices)
            self._paper.print_summary(current_prices)

        notify_daily_summary(pos_list, sig_list, account)

    def _refresh_watchlist_daily_df(self):
        """
        每日开盘前刷新候选股的日线数据（daily_df）。
        WatchItem.daily_df 在 add_watch() 时只抓取一次，
        跨天运行时会过期导致 MA20 偏差。
        """
        with self._lock:
            codes = list(self.watchlist.keys())
        for code in codes:
            try:
                fresh_df = db.get(code, freq="day", bars=65)
                with self._lock:
                    if code in self.watchlist and not fresh_df.empty:
                        self.watchlist[code].daily_df = fresh_df
                log(f"刷新日线数据: {code}", "INFO")
            except Exception as e:
                log(f"刷新日线失败 {code}: {e}", "WARN")

    def _loop(self):
        log("监控引擎启动 ✅")
        _summary_sent  = False
        _scan_sent     = False
        _df_refreshed  = False   # 每日开盘前刷新一次日线数据
        while self._running:
            now = datetime.now()

            # 09:15 刷新候选股日线数据（集合竞价前，每天一次）
            if now.hour == 9 and now.minute >= 15 and not _df_refreshed:
                self._refresh_watchlist_daily_df()
                _df_refreshed = True

            # 15:30 收盘汇总
            if now.hour == 15 and now.minute == 30 and not _summary_sent:
                self.send_daily_summary()
                _summary_sent = True

            # 15:35 市场扫描（后台线程，不阻塞主循环）
            if now.hour == 15 and now.minute == 35 and not _scan_sent:
                import threading
                from scanner.market_scan import scanner
                threading.Thread(target=scanner.run, daemon=True).start()
                _scan_sent = True

            # 次日重置所有每日标志
            if now.hour == 9 and now.minute < 15:
                _summary_sent = False
                _scan_sent    = False
                _df_refreshed = False

            if is_trading_time():
                try:
                    self._tick()
                except Exception as e:
                    log(f"轮询异常: {e}", "ERR")
            else:
                log("非交易时段，等待...")

            # 14:30-15:00 止盈窗口加密轮询，其余时段按 self.interval（默认10s）
            from monitor.realtime import is_profit_exit_window
            time.sleep(min(15, self.interval) if is_profit_exit_window() else self.interval)

    def start(self, background: bool = True):
        self._running = True
        if background:
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
        else:
            self._loop()

    def stop(self):
        self._running = False
        log("监控引擎已停止")

    # ── 状态展示 ──────────────────────────────────

    def status(self):
        """打印当前持仓、候选股状态 + 模拟账户"""
        codes = list(self.positions.keys()) + list(self.watchlist.keys())
        quotes = get_quotes(codes) if codes else {}

        print("\n" + "=" * 60)
        print(f"  520量化监控  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        print(f"\n【持仓】共 {len(self.positions)} 只")
        for code, pos in self.positions.items():
            price = quotes.get(code, {}).get("price", pos.cost)
            pnl   = pos.pnl_pct(price)
            flag  = "🟢" if pnl >= 0 else "🔴"
            print(f"  {flag} {pos.name}({code})  成本={pos.cost}  "
                  f"现价={price:.2f}  盈亏={pnl:+.1f}%  "
                  f"止损={pos.stop_price}  {pos.shares}股")

        print(f"\n【候选】共 {len(self.watchlist)} 只")
        # 按优先级排序展示
        sorted_watch = sorted(
            self.watchlist.items(),
            key=lambda kv: _PRIORITY_KEY.get(kv[1].priority, 99)
        )
        for code, item in sorted_watch:
            price    = quotes.get(code, {}).get("price", 0)
            pri_tag  = f"[{item.priority}] " if item.priority else "[自动] "
            print(f"  ⭕ {pri_tag}{item.name}({code})  现价={price:.2f}  "
                  f"信号={item.signal}  加入={item.added_time}")

        print("=" * 60 + "\n")

        # 模拟账户状态
        if self.paper_mode and self._paper:
            current_prices = {
                c: quotes.get(c, {}).get("price", 0)
                for c in list(self.positions.keys())
                if quotes.get(c, {}).get("price")
            }
            self._paper.print_summary(current_prices)


# ── 多用户编排器 ──────────────────────────────────────
# 单后台循环，逐用户在各自隔离账户上跑同一套策略；全市场扫描每日只跑一次（共享）。

class MultiUserMonitor:
    """
    多用户监控编排器。
    复用 MonitorEngine（每用户一个实例，交易逻辑原样不变），
    本类只负责：构建各用户引擎、统一调度日级任务（刷日线/收盘汇总/扫描）、
    每个 tick 逐用户调用 engine._tick()。
    """

    def __init__(self, interval: int = 30, paper_mode: bool = True):
        self.interval   = interval
        self.paper_mode = paper_mode
        self.engines: dict[str, MonitorEngine] = {}
        self._running   = False

    def build(self):
        """为每个注册用户构建引擎，加载各自持仓 + 自选。"""
        from auth.users import list_users
        users = [u["username"] for u in list_users()]
        for user in users:
            self.add_user(user, log_it=False)
        log(f"多用户引擎构建完成：{len(self.engines)} 个用户"
            f"（{', '.join(self.engines) or '无'}）", "INFO")
        return self

    def add_user(self, user: str, log_it: bool = True):
        """纳入一个用户引擎（运行中也可动态加入，如管理员新建用户后）。"""
        if user in self.engines:
            return self.engines[user]
        eng = MonitorEngine(interval=self.interval,
                            paper_mode=self.paper_mode, user=user)
        eng.load_from_paper()
        eng.load_watchlist_from_paper()
        self.engines[user] = eng
        if log_it:
            log(f"动态纳入用户引擎: {user}", "INFO")
        return eng

    def remove_user(self, user: str):
        """移除一个用户引擎（如管理员删除用户后）。"""
        self.engines.pop(user, None)

    def _sync_users(self):
        """与用户表对齐引擎名册：纳入新用户、移除已删用户。
        监控进程与 Web 进程独立，靠每日同步让新建/删除的用户次日自动生效，
        无需手动重启监控服务。"""
        try:
            from auth.users import list_users
            current = {u["username"] for u in list_users()}
        except Exception as e:
            log(f"用户名册同步失败: {e}", "WARN")
            return
        for u in current - set(self.engines):
            self.add_user(u)
        for u in set(self.engines) - current:
            self.remove_user(u)
            log(f"移除已删用户引擎: {u}", "INFO")

    def _loop(self):
        log(f"多用户监控引擎启动 ✅（{len(self.engines)} 用户）")
        _summary_sent   = False
        _scan_sent      = False
        _df_refreshed   = False
        _intraday_saved = False
        while self._running:
            now = datetime.now()

            # 09:15 同步用户名册 + 重载各用户自选(纳入昨日盘后扫描新候选) + 刷新日线
            # （集合竞价前，每天一次）
            # 注：load_watchlist_from_paper 会拉取 watchlist 表全量候选(含盘后新增)，
            #     add_watch 内部顺带取最新日线；之后 _refresh 再统一对齐 bars=65。
            if now.hour == 9 and now.minute >= 15 and not _df_refreshed:
                self._sync_users()
                for user, eng in list(self.engines.items()):
                    try:
                        eng.load_watchlist_from_paper()
                        eng._refresh_watchlist_daily_df()
                    except Exception as e:
                        log(f"[{user}] 盘前重载自选/刷新日线失败: {e}", "WARN")
                _df_refreshed = True

            # 15:30 各用户收盘汇总
            if now.hour == 15 and now.minute == 30 and not _summary_sent:
                for user, eng in list(self.engines.items()):
                    try:
                        eng.send_daily_summary()
                    except Exception as e:
                        log(f"[{user}] 收盘汇总失败: {e}", "WARN")
                _summary_sent = True

            # 15:35 全市场扫描（共享，只跑一次，不分用户）
            # 扫描完成后紧接着采集资金流（一波 clist，约6请求；顺序保证、IP友好）
            if now.hour == 15 and now.minute == 35 and not _scan_sent:
                def _scan_then_fund():
                    from scanner.market_scan import scanner
                    scanner.run()
                    # 扫描完成→立即重载各用户自选，让当晚即纳入新候选(不必等次日09:15)
                    for user, eng in list(self.engines.items()):
                        try:
                            eng.load_watchlist_from_paper()
                        except Exception as e:
                            log(f"[{user}] 扫描后重载自选失败: {e}", "WARN")
                    try:
                        from scanner.ai_score import collect_fund_to_db
                        n = collect_fund_to_db()
                        log(f"资金流采集入库: {n} 只", "INFO")
                    except Exception as e:
                        log(f"资金流采集失败: {e}", "WARN")
                    try:
                        from scanner.hot_sectors import collect_to_db as _hot
                        m = _hot()
                        log(f"热门题材采集入库: {m} 个", "INFO")
                    except Exception as e:
                        log(f"热门题材采集失败: {e}", "WARN")
                threading.Thread(target=_scan_then_fund, daemon=True).start()
                _scan_sent = True

            # 15:40 盘中分钟数据落库（扫描后，覆盖当日完整 5 分钟K；后台线程）
            # 为忠实回测 check_entry/check_position 向前积累历史
            if now.hour == 15 and now.minute == 40 and not _intraday_saved:
                def _save_intraday():
                    try:
                        from data.intraday_store import collect_default
                        ok, total, n = collect_default()
                        log(f"盘中分钟数据落库: {ok}/{n} 只，{total} 根5分钟K", "INFO")
                    except Exception as e:
                        log(f"分钟数据落库失败: {e}", "WARN")
                threading.Thread(target=_save_intraday, daemon=True).start()
                _intraday_saved = True

            # 次日重置每日标志
            if now.hour == 9 and now.minute < 15:
                _summary_sent = _scan_sent = _df_refreshed = _intraday_saved = False

            # 交易时段：逐用户轮询（各自隔离账户）
            if is_trading_time():
                for user, eng in list(self.engines.items()):
                    try:
                        eng._tick()
                    except Exception as e:
                        log(f"[{user}] 轮询异常: {e}", "ERR")
            else:
                log("非交易时段，等待...")

            from monitor.realtime import is_profit_exit_window
            time.sleep(min(15, self.interval) if is_profit_exit_window() else self.interval)

    def start(self, background: bool = True):
        self._running = True
        if background:
            threading.Thread(target=self._loop, daemon=True).start()
        else:
            self._loop()

    def stop(self):
        self._running = False
        log("多用户监控引擎已停止")

    def status(self):
        print("\n" + "=" * 60)
        print(f"  520量化 多用户监控  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  共 {len(self.engines)} 个用户")
        print("=" * 60)
        for user, eng in self.engines.items():
            print(f"\n──────── 用户: {user} ────────")
            eng.status()


# 全局单例（懒加载，避免 import 即创建遗留库）
_monitor_singleton: "MonitorEngine | None" = None


def __getattr__(name):
    # PEP 562：保留 `from monitor.engine import monitor` 的向后兼容（单用户引擎）
    global _monitor_singleton
    if name == "monitor":
        if _monitor_singleton is None:
            _monitor_singleton = MonitorEngine(interval=30)
        return _monitor_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

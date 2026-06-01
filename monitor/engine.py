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

# 各信号对应建仓比例（相对单只股票槽位 = 总资金 / max_positions）
SIGNAL_SIZE = {
    Signal.BUY_GOLDEN_CROSS: 0.30,   # 金叉：3成
    Signal.BUY_PULLBACK:     0.20,   # 回踩：加2成
    Signal.BUY_SQUEEZE:      0.50,   # 粘合：5成
}
from monitor.realtime import get_quotes, is_trading_time, is_market_open
from monitor.intraday import engine as intraday_engine, Action
from alert.notifier import (log, notify_buy, notify_sell, notify_warning,
                            notify_daily_summary, notify_stop_raised)
from trader.paper import PaperAccount


# ── 数据结构 ──────────────────────────────────────────

@dataclass
class Position:
    """持仓记录"""
    code:        str
    name:        str
    cost:        float
    shares:      int
    stop_price:  float
    entry_time:  str   = ""
    hold_days:   int   = 0
    peak_pnl:    float = 0.0   # 历史最高盈利%，用于回落保护止盈

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
    added_time: str = ""

    def __post_init__(self):
        if not self.added_time:
            self.added_time = datetime.now().strftime("%H:%M:%S")


# ── 主引擎 ────────────────────────────────────────────

class MonitorEngine:

    def __init__(self, interval: int = 30, paper_mode: bool = True):
        self.interval       = interval      # 轮询间隔（秒）
        self.paper_mode     = paper_mode    # True=模拟交易 / False=实盘（需接券商）
        self.max_positions  = 4             # 最多同时持仓4只
        self.init_capital   = 200_000.0     # 总资金，用于计算每仓金额
        self.positions:  dict[str, Position]  = {}
        self.watchlist:  dict[str, WatchItem] = {}
        self._running    = False
        self._lock       = threading.Lock()
        self._broker     = None          # 可注入券商接口
        self._paper      = PaperAccount() if paper_mode else None
        self._warn_cooldown: dict[str, float] = {}   # code -> 上次预警时间戳

    # ── 持仓管理 ──────────────────────────────────

    def add_position(self, code: str, cost: float, shares: int,
                     name: str = "", stop_price: float = 0.0):
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
            )
        log(f"持仓录入: {name}({code}) 成本={cost} 数量={shares} 止损={stop_price}", "INFO")

    def remove_position(self, code: str):
        with self._lock:
            self.positions.pop(code, None)

    def add_watch(self, code: str, name: str, signal: str, priority: str = ""):
        """加入候选股监控
        priority: "P1" / "P2" / "P3"（手动 WATCHLIST 设置）；
                  空字符串 = 扫描器自动发现，排在所有手动股票之后
        """
        daily_df = db.get(code)
        with self._lock:
            self.watchlist[code] = WatchItem(
                code=code, name=name, signal=signal,
                daily_df=daily_df, priority=priority
            )
        tag = priority if priority else "自动"
        log(f"候选股加入: {name}({code}) 信号={signal} [{tag}]", "INFO")

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
                )
            log(f"加载持仓: {p.name}({code}) 成本={p.cost} "
                f"止损={p.stop_price} {p.shares}股", "INFO")
        log(f"共加载 {len(paper_pos)} 只持仓", "INFO")

    # ── 核心轮询 ──────────────────────────────────

    def _tick(self):
        """单次轮询：获取报价 → 检查信号"""
        with self._lock:
            pos_codes   = list(self.positions.keys())
            watch_codes = list(self.watchlist.keys())

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

            sig = intraday_engine.check_position(
                code, daily_df, quote,
                cost=pos.cost, stop_price=pos.stop_price,
                peak_pnl=pos.peak_pnl,
            )

            if sig.action in (Action.SELL_STOP, Action.SELL_PROFIT):
                self._do_sell(pos, price, sig.reason)  # notify_sell 移至内部，仅成功才推送
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

        # P1 → P2 → P3 → 自动扫描（无优先级）
        watch_codes_sorted = sorted(
            watch_codes,
            key=lambda c: _PRIORITY_KEY.get(
                self.watchlist[c].priority if c in self.watchlist else "", 99
            )
        )

        if watch_codes_sorted:
            log(f"候选股检查: 共{len(watch_codes_sorted)}只 | "
                f"market_up={market_up} | hard_down={market_hard_down} | "
                f"持仓{len(self.positions)}/{self.max_positions}", "INFO")

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
                                              signal_type=item.signal)

            if sig.action == Action.BUY:
                if len(self.positions) >= self.max_positions:
                    log(f"候选 {item.name}({code}) ⛔ 已达最大持仓{self.max_positions}只，跳过")
                    continue
                price  = quote["price"]
                shares = self._calc_shares(price)
                self._do_buy(code, item.name, price, shares, sig.reason)
            else:
                log(f"候选 {item.name}({code}) {quote['price']:.2f} | {sig.reason}")

    def _do_buy(self, code: str, name: str, price: float,
                shares: int, reason: str):
        """执行买入（可接券商API / 模拟账户）"""
        stop_price = round(price * 0.95, 2)

        if self.paper_mode and self._paper:
            ok, msg = self._paper.buy(
                code=code, name=name, price=price, shares=shares,
                signal=reason, stop_price=stop_price
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
            stop_price=stop_price
        )
        self.remove_watch(code)

    def _do_sell(self, pos: Position, price: float, reason: str):
        """执行卖出（可接券商API / 模拟账户）"""
        if self.paper_mode and self._paper:
            ok, msg = self._paper.sell(
                code=pos.code, price=price, signal=reason
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

    def _calc_shares(self, price: float) -> int:
        """满仓计算：总资金 ÷ 最大持仓数 = 单仓资金"""
        slot_cap = self.init_capital / self.max_positions
        shares   = int(slot_cap / price / 100) * 100
        return max(100, shares)

    # ── 追踪止损 ──────────────────────────────────────

    # 关键档位：(最小浮盈%, 止损锁定描述, 止损倍数_相对成本)
    _TRAIL_TIERS = [
        (20.0, "盈利超20%，锁定+10%保底",  1.10),
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
        里程碑：成本价（保本）、成本×1.05（+5%）、成本×1.10（+10%）
        """
        milestones = [cost * 1.002, cost * 1.05, cost * 1.10]
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
            time.sleep(self.interval)

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


# 全局单例
monitor = MonitorEngine(interval=30)

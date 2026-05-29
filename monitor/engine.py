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
    entry_time:  str  = ""
    hold_days:   int  = 0

    @property
    def market_value(self) -> float:
        return self.cost * self.shares   # 实时更新在外层

    def pnl(self, price: float) -> float:
        return (price - self.cost) * self.shares

    def pnl_pct(self, price: float) -> float:
        return (price - self.cost) / self.cost * 100


@dataclass
class WatchItem:
    """候选股（日线已触发买点，等日内确认）"""
    code:       str
    name:       str
    signal:     str
    daily_df:   pd.DataFrame
    added_time: str = ""

    def __post_init__(self):
        if not self.added_time:
            self.added_time = datetime.now().strftime("%H:%M:%S")


# ── 主引擎 ────────────────────────────────────────────

class MonitorEngine:

    def __init__(self, interval: int = 30, paper_mode: bool = True):
        self.interval    = interval      # 轮询间隔（秒）
        self.paper_mode  = paper_mode    # True=模拟交易 / False=实盘（需接券商）
        self.positions:  dict[str, Position]  = {}
        self.watchlist:  dict[str, WatchItem] = {}
        self._running    = False
        self._lock       = threading.Lock()
        self._broker     = None          # 可注入券商接口
        self._paper      = PaperAccount() if paper_mode else None

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

    def add_watch(self, code: str, name: str, signal: str):
        """加入候选股监控"""
        daily_df = db.get(code)
        with self._lock:
            self.watchlist[code] = WatchItem(
                code=code, name=name, signal=signal, daily_df=daily_df
            )
        log(f"候选股加入: {name}({code}) 信号={signal}", "INFO")

    def remove_watch(self, code: str):
        with self._lock:
            self.watchlist.pop(code, None)

    # ── 核心轮询 ──────────────────────────────────

    def _tick(self):
        """单次轮询：获取报价 → 检查信号"""
        with self._lock:
            pos_codes   = list(self.positions.keys())
            watch_codes = list(self.watchlist.keys())

        all_codes = list(set(pos_codes + watch_codes))
        if not all_codes:
            return

        quotes = get_quotes(all_codes)
        if not quotes:
            log("报价获取失败，跳过本轮", "WARN")
            return

        ts = datetime.now().strftime("%H:%M:%S")

        # ── 持仓检查 ──
        for code in pos_codes:
            quote = quotes.get(code)
            pos   = self.positions.get(code)
            if not quote or not pos:
                continue

            price = quote["price"]
            with self._lock:
                daily_df = db.get(code)

            sig = intraday_engine.check_position(
                code, daily_df, quote,
                cost=pos.cost, stop_price=pos.stop_price
            )

            if sig.action in (Action.SELL_STOP, Action.SELL_PROFIT):
                notify_sell(
                    code, pos.name, price,
                    pos.shares, pos.cost, sig.reason
                )
                self._do_sell(pos, price, sig.reason)
            else:
                pnl = pos.pnl_pct(price)
                # 接近止损线预警（距止损价 < 2%）
                if pos.stop_price and price > 0:
                    gap_pct = (price - pos.stop_price) / price * 100
                    if 0 < gap_pct < 2.0:
                        notify_warning(
                            code, pos.name, price, pos.cost,
                            f"距止损线仅剩 {gap_pct:.1f}%（止损={pos.stop_price:.2f}）"
                        )
                log(f"{pos.name}({code}) {price:.2f} | "
                    f"盈亏={pnl:+.1f}% | {sig.reason}")

        # ── 追踪止损更新（持仓检查后执行，确保未被卖出的仓位才更新）──
        self._update_trailing_stops(quotes)

        # ── 候选股检查 ──
        for code in watch_codes:
            quote = quotes.get(code)
            item  = self.watchlist.get(code)
            if not quote or not item:
                continue

            # 只在正式开盘后检查入场
            if not is_market_open():
                continue

            sig = intraday_engine.check_entry(code, item.daily_df, quote)

            if sig.action == Action.BUY:
                price  = quote["price"]
                shares = self._calc_shares(price)
                notify_buy(
                    code, item.name, price, shares,
                    sig.reason,
                    stop_price=round(price * 0.95, 2)
                )
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

        # 更新内部持仓（模拟或实盘均更新）
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
                return
        elif self._broker:
            try:
                result = self._broker.sell(pos.code, price, pos.shares)
                log(f"卖出下单成功: {result}", "INFO")
            except Exception as e:
                log(f"卖出下单失败: {e}", "ERR")
                return
        self.remove_position(pos.code)

    def _calc_shares(self, price: float,
                     total_capital: float = 100_000,
                     risk_pct: float = 0.02,
                     stop_pct: float = 0.05,
                     max_position_pct: float = 0.30) -> int:
        """仓位计算：每笔最大亏损=总资金×2%，止损5%→单笔≤总资金30%"""
        max_loss  = total_capital * risk_pct
        pos_val   = min(max_loss / stop_pct, total_capital * max_position_pct)
        shares    = int(pos_val / price / 100) * 100
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

            # 只在跨越关键档位时才推送微信通知（避免刷屏）
            if crossed_milestone:
                notify_stop_raised(
                    code, pos.name, price,
                    old_stop, candidate_stop,
                    gain_pct, milestone_label
                )

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

    def _loop(self):
        log("监控引擎启动 ✅")
        _summary_sent = False
        _scan_sent    = False
        while self._running:
            now = datetime.now()
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
            if now.hour == 9 and now.minute < 15:
                _summary_sent = False   # 次日重置
                _scan_sent    = False

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
        for code, item in self.watchlist.items():
            price = quotes.get(code, {}).get("price", 0)
            print(f"  ⭕ {item.name}({code})  现价={price:.2f}  "
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

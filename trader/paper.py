"""
模拟交易账户
- 真实行情 + 虚拟资金
- SQLite 持久化（重启不丢数据）
- 自动执行买卖信号
- 实时盈亏 + 绩效统计
"""
from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


BASE_DIR  = Path.home() / ".520quant"
LEGACY_DB = BASE_DIR / "paper_trade.db"        # 多用户化之前的单一账户库（迁移源）
USERS_DIR = BASE_DIR / "users"                 # 各用户独立账户库目录
MARKET_DB = BASE_DIR / "market.db"             # 全市场扫描结果（所有用户共享）

# 向后兼容：旧代码引用的 DB_PATH
DB_PATH = LEGACY_DB


def user_db_path(user: str | None) -> Path:
    """解析某用户的账户库路径；user 为空时回退到 legacy 单库（向后兼容）"""
    if user:
        return USERS_DIR / user / "paper_trade.db"
    return LEGACY_DB


def delete_user_data(user: str) -> bool:
    """删除某用户的隔离数据目录（持仓/记录/自选/账户）。共享的 market.db 不受影响。"""
    if not user:
        return False
    import shutil
    user_dir = USERS_DIR / user
    # 安全校验：确保目标确实在 USERS_DIR 之内，避免越界删除
    try:
        user_dir.resolve().relative_to(USERS_DIR.resolve())
    except Exception:
        return False
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
    return True


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class Order:
    id:         int
    code:       str
    name:       str
    side:       str          # BUY / SELL
    price:      float
    shares:     int
    amount:     float        # 成交金额
    signal:     str          # 触发原因
    timestamp:  str


@dataclass
class PaperPosition:
    code:         str
    name:         str
    cost:         float        # 均价
    shares:       int
    stop_price:   float
    entry_time:   str
    entry_signal: str = ""     # 买入触发信号（信号类型 | 原因）
    scaled:       int = 0      # 是否已分批止盈卖出过半仓（0/1）

    def market_value(self, price: float) -> float:
        return price * self.shares

    def pnl(self, price: float) -> float:
        return (price - self.cost) * self.shares

    def pnl_pct(self, price: float) -> float:
        return (price - self.cost) / self.cost * 100


# ── 数据库初始化 ──────────────────────────────────────────

def _init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS account (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            code       TEXT,
            name       TEXT,
            side       TEXT,
            price      REAL,
            shares     INTEGER,
            amount     REAL,
            signal     TEXT,
            timestamp  TEXT
        );

        CREATE TABLE IF NOT EXISTS positions (
            code         TEXT PRIMARY KEY,
            name         TEXT,
            cost         REAL,
            shares       INTEGER,
            stop_price   REAL,
            entry_time   TEXT,
            entry_signal TEXT DEFAULT '',
            scaled       INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS watchlist (
            code       TEXT PRIMARY KEY,
            name       TEXT,
            signal     TEXT,
            added_time TEXT,
            priority   TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS scan_results (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date  TEXT,
            code       TEXT,
            name       TEXT,
            price      REAL,
            signal     TEXT,
            reason     TEXT,
            score      REAL,
            stop_price REAL
        );
    """)
    conn.commit()
    # 迁移：老数据库可能没有 priority 列
    try:
        conn.execute("ALTER TABLE watchlist ADD COLUMN priority TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 迁移：scan_results 加 rs_score 列（相对强度分）
    try:
        conn.execute("ALTER TABLE scan_results ADD COLUMN rs_score REAL DEFAULT 0")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 迁移：scan_results 加 sector_dir 列（板块ETF方向，仅展示）
    try:
        conn.execute("ALTER TABLE scan_results ADD COLUMN sector_dir TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 迁移：scan_results 加 cross_date 列（金叉形成日期）
    try:
        conn.execute("ALTER TABLE scan_results ADD COLUMN cross_date TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 迁移：positions 加 entry_signal 列（买入触发信号）
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN entry_signal TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 迁移：positions 加 scaled 列（分批止盈是否已卖半仓）
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN scaled INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 迁移：scan_results 加 sector_name 列（申万行业名称，如"白酒Ⅱ"/"半导体"）
    try:
        conn.execute("ALTER TABLE scan_results ADD COLUMN sector_name TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 迁移：scan_results 加 score_detail 列（评分明细 JSON）
    try:
        conn.execute("ALTER TABLE scan_results ADD COLUMN score_detail TEXT DEFAULT '[]'")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 迁移：orders 加 conditions 列（交易条件追踪 JSON）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN conditions TEXT DEFAULT '[]'")
        conn.commit()
    except Exception:
        pass

    # 迁移：orders 加 voided 列（手动失效标记，1=失效不计入统计）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN voided INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass   # 列已存在，忽略

    # 初始化账户资金（首次）
    cur = conn.execute("SELECT value FROM account WHERE key='cash'")
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO account VALUES ('cash', ?)",
            (str(INIT_CAPITAL),)
        )
        conn.execute(
            "INSERT INTO account VALUES ('init_capital', ?)",
            (str(INIT_CAPITAL),)
        )
        conn.commit()


# ── 共享扫描库（全市场扫描结果，所有用户共用 market.db）──────

def _init_scan_db(conn: sqlite3.Connection):
    """初始化共享扫描结果表"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_results (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date    TEXT,
            code         TEXT,
            name         TEXT,
            price        REAL,
            signal       TEXT,
            reason       TEXT,
            score        REAL,
            stop_price   REAL,
            rs_score     REAL DEFAULT 0,
            sector_dir   TEXT DEFAULT '',
            cross_date   TEXT DEFAULT '',
            sector_name  TEXT DEFAULT '',
            score_detail TEXT DEFAULT '[]',
            change_pct   REAL DEFAULT 0,
            ai_score     REAL DEFAULT 0,
            ai_comment   TEXT DEFAULT '',
            main_net_today REAL,
            main_net_5d    REAL,
            main_net_10d   REAL
        );
    """)
    conn.commit()
    # 迁移：老 market.db 补列
    for col, ddl in (
        ("change_pct", "REAL DEFAULT 0"),
        ("ai_score",   "REAL DEFAULT 0"),    # AI 综合评分（技术+资金+消息）
        ("ai_comment", "TEXT DEFAULT ''"),   # AI 评分理由（一句话）
        ("main_net_today", "REAL"),          # 当日主力净额（万元），NULL=未采集
        ("main_net_5d",    "REAL"),          # 近5日主力净额（万元）
        ("main_net_10d",   "REAL"),          # 近10日主力净额（万元）
    ):
        try:
            conn.execute(f"ALTER TABLE scan_results ADD COLUMN {col} {ddl}")
            conn.commit()
        except Exception:
            pass   # 列已存在，忽略


# ── 模拟账户 ──────────────────────────────────────────────

INIT_CAPITAL = 200_000.0    # 初始资金（可在启动时修改）


class PaperAccount:
    """
    模拟交易账户
    所有操作通过 SQLite 持久化，重启后恢复状态
    """

    def __init__(self, init_capital: float = INIT_CAPITAL, user: str | None = None):
        self.user = user

        # ── 每用户独立账户库（account / positions / orders / watchlist）──
        db_path = user_db_path(user)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _init_db(self._conn)

        # ── 共享扫描库（全市场扫描结果，所有用户共用）──
        MARKET_DB.parent.mkdir(parents=True, exist_ok=True)
        self._scan_conn = sqlite3.connect(str(MARKET_DB), check_same_thread=False)
        _init_scan_db(self._scan_conn)

        # 首次运行设置初始资金
        cur = self._conn.execute("SELECT value FROM account WHERE key='cash'")
        row = cur.fetchone()
        if row and float(row[0]) == INIT_CAPITAL and init_capital != INIT_CAPITAL:
            self._set("cash", init_capital)
            self._set("init_capital", init_capital)

    # ── 账户基础读写 ──────────────────────────────────

    def _get(self, key: str, default=0.0) -> float:
        cur = self._conn.execute(
            "SELECT value FROM account WHERE key=?", (key,)
        )
        row = cur.fetchone()
        return float(row[0]) if row else default

    def _set(self, key: str, value: float):
        self._conn.execute(
            "INSERT OR REPLACE INTO account VALUES (?,?)",
            (key, str(value))
        )
        self._conn.commit()

    @property
    def cash(self) -> float:
        return self._get("cash")

    @property
    def init_capital(self) -> float:
        return self._get("init_capital", INIT_CAPITAL)

    def get_pos_cap(self, default: int = 4) -> int:
        """持仓数上限(可配置, 0=弱市不开新仓)。仅限制开仓数量, 不改每仓大小。"""
        return int(self._get("pos_cap", float(default)))

    def set_pos_cap(self, n: int):
        self._set("pos_cap", float(max(0, min(4, int(n)))))

    # ── 持仓管理 ──────────────────────────────────────

    def positions(self) -> dict[str, PaperPosition]:
        rows = self._conn.execute("SELECT * FROM positions").fetchall()
        result = {}
        for r in rows:
            result[r[0]] = PaperPosition(
                code=r[0], name=r[1], cost=r[2],
                shares=r[3], stop_price=r[4], entry_time=r[5],
                entry_signal=r[6] if len(r) > 6 else "",
                scaled=int(r[7]) if len(r) > 7 and r[7] is not None else 0
            )
        return result

    def get_position(self, code: str) -> Optional[PaperPosition]:
        cur = self._conn.execute(
            "SELECT * FROM positions WHERE code=?", (code,)
        )
        r = cur.fetchone()
        if not r:
            return None
        return PaperPosition(
            code=r[0], name=r[1], cost=r[2],
            shares=r[3], stop_price=r[4], entry_time=r[5],
            entry_signal=r[6] if len(r) > 6 else "",
            scaled=int(r[7]) if len(r) > 7 and r[7] is not None else 0
        )

    def update_stop(self, code: str, new_stop: float) -> bool:
        """更新持仓止损价（追踪止损专用，只升不降）"""
        pos = self.get_position(code)
        if not pos:
            return False
        if new_stop <= pos.stop_price:
            return False   # 止损线只升不降
        self._conn.execute(
            "UPDATE positions SET stop_price=? WHERE code=?",
            (round(new_stop, 2), code)
        )
        self._conn.commit()
        return True

    # ── 交易执行 ──────────────────────────────────────

    def buy(self, code: str, name: str, price: float, shares: int,
            signal: str = "", stop_price: float = 0.0,
            entry_signal: str = "",
            conditions: list = None) -> tuple[bool, str]:
        """
        模拟买入
        返回 (成功, 消息)
        """
        shares = int(shares // 100 * 100)   # 取整到100股
        if shares <= 0:
            return False, "买入数量不足100股"

        amount = round(price * shares, 2)
        commission = round(amount * 0.0001, 2)   # 万1佣金
        total_cost = amount + commission

        if total_cost > self.cash:
            max_shares = int(self.cash / price / 100) * 100
            if max_shares <= 0:
                return False, f"现金不足（剩余 {self.cash:.0f} 元）"
            shares = max_shares
            amount = round(price * shares, 2)
            commission = round(amount * 0.0001, 2)
            total_cost = amount + commission

        # 检查是否已有持仓（不加仓）
        existing = self.get_position(code)
        if existing:
            return False, f"已有持仓，520战法不加仓（现有 {existing.shares} 股）"

        # 写入订单
        self._conn.execute(
            "INSERT INTO orders (code,name,side,price,shares,amount,signal,timestamp,conditions) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, name, "BUY", price, shares, amount, signal,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             json.dumps(conditions or [], ensure_ascii=False))
        )

        # 写入持仓
        stop = stop_price or round(price * 0.95, 2)
        self._conn.execute(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?)",
            (code, name, price, shares, stop,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             entry_signal, 0)
        )

        # 扣减现金
        self._set("cash", self.cash - total_cost)
        self._conn.commit()

        msg = (f"模拟买入 {name}({code}) "
               f"{price:.2f}×{shares}股={amount:.0f}元 "
               f"佣金={commission:.1f} 止损={stop:.2f}")
        return True, msg

    def sell(self, code: str, price: float,
             signal: str = "",
             conditions: list = None,
             qty: int = None) -> tuple[bool, str]:
        """
        模拟卖出。qty 为 None 或 ≥持仓时全仓清；qty<持仓时为分批卖出（卖出部分、
        剩余继续持有并标记 scaled=1）。返回 (成功, 消息)。
        """
        pos = self.get_position(code)
        if not pos:
            return False, f"无持仓: {code}"

        # ── T+1 约束：当日买入不可当日卖出（A股规则）──
        today = datetime.now().strftime("%Y-%m-%d")
        if pos.entry_time and pos.entry_time[:10] == today:
            return False, f"T+1限制：{code} 当日买入，不可当日卖出（{pos.entry_time[:10]}）"

        partial     = qty is not None and 0 < int(qty) < pos.shares
        sell_shares = int(qty) if partial else pos.shares
        if sell_shares <= 0:
            return False, "卖出数量无效"

        amount     = round(price * sell_shares, 2)
        commission = round(amount * 0.0001, 2)
        stamp_tax  = round(amount * 0.001, 2)     # 印花税（卖出单边）
        total_fee  = commission + stamp_tax
        net_amount = amount - total_fee
        pnl        = round(net_amount - pos.cost * sell_shares, 2)
        pnl_pct    = round(pnl / (pos.cost * sell_shares) * 100, 2)

        # 写入订单
        self._conn.execute(
            "INSERT INTO orders (code,name,side,price,shares,amount,signal,timestamp,conditions) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, pos.name, "SELL", price, sell_shares, amount, signal,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             json.dumps(conditions or [], ensure_ascii=False))
        )

        if partial:
            # 分批：减仓 + 标记已分批，剩余继续持有
            self._conn.execute(
                "UPDATE positions SET shares = shares - ?, scaled = 1 WHERE code=?",
                (sell_shares, code))
        else:
            self._conn.execute("DELETE FROM positions WHERE code=?", (code,))

        # 增加现金
        self._set("cash", self.cash + net_amount)
        self._conn.commit()

        tag = "分批卖出" if partial else "模拟卖出"
        msg = (f"{tag} {pos.name}({code}) "
               f"{price:.2f}×{sell_shares}股 "
               f"盈亏={pnl:+.0f}元({pnl_pct:+.1f}%) "
               f"费用={total_fee:.1f}")
        return True, msg

    # ── 账户统计 ──────────────────────────────────────

    def summary(self, current_prices: dict[str, float] = None) -> dict:
        """账户概览"""
        positions   = self.positions()
        current_prices = current_prices or {}

        pos_value = sum(
            pos.market_value(current_prices.get(code, pos.cost))
            for code, pos in positions.items()
        )
        total_assets = round(self.cash + pos_value, 2)
        total_return = round(
            (total_assets - self.init_capital) / self.init_capital * 100, 2
        )

        pos_detail = []
        for code, pos in positions.items():
            price   = current_prices.get(code, pos.cost)
            pnl     = pos.pnl(price)
            pnl_pct = pos.pnl_pct(price)
            pos_detail.append({
                "code":       code,
                "name":       pos.name,
                "cost":       pos.cost,
                "price":      price,
                "shares":     pos.shares,
                "mkt_value":  round(pos.market_value(price), 0),
                "pnl":        round(pnl, 0),
                "pnl_pct":    round(pnl_pct, 2),
                "stop_price": pos.stop_price,
            })

        return {
            "init_capital": self.init_capital,
            "cash":         round(self.cash, 2),
            "pos_value":    round(pos_value, 2),
            "total_assets": total_assets,
            "total_return": total_return,
            "positions":    pos_detail,
        }

    def performance(self) -> dict:
        """历史绩效统计"""
        orders = self._conn.execute(
            "SELECT side,price,shares,amount,signal,timestamp "
            "FROM orders ORDER BY id"
        ).fetchall()

        trades = []
        buy_map = {}
        for o in orders:
            side, price, shares, amount, signal, ts = o
            if side == "BUY":
                buy_map[signal] = {"price": price, "shares": shares,
                                   "amount": amount, "ts": ts}
            else:
                # 匹配最近的买单
                cost_per_share = list(buy_map.values())[-1]["price"] \
                    if buy_map else price
                pnl = (price - cost_per_share) * shares
                trades.append({
                    "sell_price": price,
                    "cost":       cost_per_share,
                    "shares":     shares,
                    "pnl":        round(pnl, 2),
                    "pnl_pct":    round(pnl / (cost_per_share * shares) * 100, 2),
                    "signal":     signal,
                    "ts":         ts,
                })

        if not trades:
            return {"message": "暂无已平仓交易"}

        wins    = [t for t in trades if t["pnl"] > 0]
        losses  = [t for t in trades if t["pnl"] <= 0]
        total   = len(trades)

        return {
            "总交易次数":    total,
            "胜率":         f"{len(wins)/total*100:.1f}%",
            "盈利次数":     len(wins),
            "亏损次数":     len(losses),
            "总盈亏":       f"{sum(t['pnl'] for t in trades):+.0f} 元",
            "平均盈利":     f"{sum(t['pnl'] for t in wins)/len(wins):.0f} 元" if wins else "—",
            "平均亏损":     f"{sum(t['pnl'] for t in losses)/len(losses):.0f} 元" if losses else "—",
            "最大单笔盈利": f"{max(t['pnl'] for t in trades):+.0f} 元",
            "最大单笔亏损": f"{min(t['pnl'] for t in trades):+.0f} 元",
            "近5笔交易":    trades[-5:],
        }

    def print_summary(self, current_prices: dict[str, float] = None):
        """打印账户概览"""
        s = self.summary(current_prices)
        pnl_icon = "🟢" if s["total_return"] >= 0 else "🔴"
        print("\n" + "=" * 55)
        print(f"  📋 模拟账户  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 55)
        print(f"  初始资金   {s['init_capital']:>10,.0f} 元")
        print(f"  当前现金   {s['cash']:>10,.0f} 元")
        print(f"  持仓市值   {s['pos_value']:>10,.0f} 元")
        print(f"  总资产     {s['total_assets']:>10,.0f} 元")
        print(f"  {pnl_icon} 累计收益  {s['total_return']:>+9.2f} %")
        print("-" * 55)
        if s["positions"]:
            print(f"  {'代码':<8} {'名称':<8} {'成本':>6} {'现价':>6} "
                  f"{'盈亏%':>7} {'市值':>8} {'止损':>7}")
            for p in s["positions"]:
                icon = "🟢" if p["pnl_pct"] >= 0 else "🔴"
                print(f"  {icon}{p['code']:<7} {p['name']:<8} "
                      f"{p['cost']:>6.2f} {p['price']:>6.2f} "
                      f"{p['pnl_pct']:>+6.1f}% "
                      f"{p['mkt_value']:>8,.0f} "
                      f"{p['stop_price']:>7.2f}")
        else:
            print("  （空仓）")
        print("=" * 55 + "\n")

    def print_performance(self):
        """打印绩效报告"""
        p = self.performance()
        if "message" in p:
            print(f"\n  {p['message']}\n")
            return
        print("\n" + "=" * 45)
        print("  📊 历史绩效报告")
        print("=" * 45)
        for k, v in p.items():
            if k == "近5笔交易":
                continue
            print(f"  {k:<12}  {v}")
        print("\n  近5笔交易：")
        for t in p.get("近5笔交易", []):
            icon = "🟢" if t["pnl"] > 0 else "🔴"
            print(f"  {icon} {t['ts'][:10]}  "
                  f"成本={t['cost']:.2f} 卖={t['sell_price']:.2f} "
                  f"盈亏={t['pnl']:+.0f}元({t['pnl_pct']:+.1f}%)")
        print("=" * 45 + "\n")


    # ── 交易失效管理 ─────────────────────────────────

    def void_trade(self, sell_order_id: int, voided: bool = True) -> tuple[bool, str]:
        """
        将一笔已平仓交易标记为失效，同时回退现金影响。
        sell_order_id: SELL 订单的 ID（api_trades 接口返回）
        voided=True  → 失效：cash -= pnl（抹掉该笔盈亏）
        voided=False → 恢复：cash += pnl（重新计入盈亏）
        """
        sell_row = self._conn.execute(
            "SELECT code, side, amount, COALESCE(voided,0) FROM orders WHERE id=?",
            (sell_order_id,)
        ).fetchone()
        if not sell_row:
            return False, f"订单 {sell_order_id} 不存在"
        code, side, sell_amount, already_voided = sell_row
        if side != "SELL":
            return False, "只能对卖出订单操作"
        if bool(already_voided) == voided:
            return True, "状态未变化，跳过"

        # ── 找配对的买入订单（FIFO 匹配，与 api_trades 逻辑一致）──
        all_orders = self._conn.execute(
            "SELECT id, side, amount FROM orders WHERE code=? ORDER BY id",
            (code,)
        ).fetchall()

        buy_queue   = []
        paired_buy  = None
        for oid, o_side, o_amount in all_orders:
            if o_side == "BUY":
                buy_queue.append((oid, o_amount))
            elif o_side == "SELL" and buy_queue:
                buy = buy_queue.pop(0)
                if oid == sell_order_id:
                    paired_buy = buy
                    break

        if not paired_buy:
            return False, "找不到对应买入订单，无法调整现金"

        _, buy_amount = paired_buy

        # ── 计算该笔交易净盈亏（与 api_trades 公式一致）──
        commission = round((buy_amount + sell_amount) * 0.0001, 2)
        stamp_tax  = round(sell_amount * 0.001, 2)
        pnl        = round(sell_amount - buy_amount - commission - stamp_tax, 2)

        # ── 更新失效状态 ──
        self._conn.execute(
            "UPDATE orders SET voided=? WHERE id=?",
            (1 if voided else 0, sell_order_id)
        )

        # ── 回退现金：失效→扣回盈亏；恢复→补回盈亏 ──
        cash_delta = -pnl if voided else pnl
        self._set("cash", self.cash + cash_delta)
        self._conn.commit()

        action = "失效" if voided else "恢复有效"
        return True, (
            f"{code} 已{action} | 现金调整 {cash_delta:+.2f} 元"
            f"（该笔盈亏={pnl:+.2f} 元）"
        )

    # ── Watchlist 管理 ────────────────────────────────

    def get_watchlist(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT code,name,signal,added_time,priority FROM watchlist "
            "ORDER BY added_time DESC"
        ).fetchall()
        return [
            {"code": r[0], "name": r[1], "signal": r[2],
             "added_time": r[3], "priority": r[4] or ""}
            for r in rows
        ]

    def add_to_watchlist(self, code: str, name: str,
                         signal: str = "", priority: str = "") -> bool:
        """添加自选股；priority 为空时保留已有优先级"""
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._conn.execute("""
                INSERT INTO watchlist(code, name, signal, added_time, priority)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name       = excluded.name,
                    signal     = excluded.signal,
                    added_time = excluded.added_time,
                    priority   = CASE WHEN excluded.priority != ''
                                      THEN excluded.priority
                                      ELSE priority END
            """, (code, name, signal, now, priority))
            self._conn.commit()
            return True
        except Exception:
            return False

    def update_watchlist_priority(self, code: str, priority: str) -> bool:
        """单独更新某只自选股的优先级（P1/P2/P3 或空字符串清除）"""
        self._conn.execute(
            "UPDATE watchlist SET priority=? WHERE code=?", (priority, code)
        )
        self._conn.commit()
        return True

    def remove_from_watchlist(self, code: str) -> bool:
        self._conn.execute("DELETE FROM watchlist WHERE code=?", (code,))
        self._conn.commit()
        return True

    # ── 扫描结果 ──────────────────────────────────────

    def save_scan_results(self, scan_date: str, results: list[dict]):
        """保存当日扫描结果到共享库（先清除当日所有旧记录，支持秒级时间戳）"""
        date_prefix = scan_date[:10]   # 取 YYYY-MM-DD 前缀，清除同一天的历次扫描
        self._scan_conn.execute("DELETE FROM scan_results WHERE scan_date LIKE ?", (date_prefix + "%",))
        for r in results:
            self._scan_conn.execute(
                "INSERT INTO scan_results "
                "(scan_date,code,name,price,signal,reason,score,stop_price,"
                "rs_score,sector_dir,cross_date,sector_name,score_detail,change_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scan_date, r["code"], r["name"], r["price"],
                 r["signal"], r["reason"],
                 r.get("score", 0), r.get("stop_price", 0),
                 r.get("rs_score", 0), r.get("sector_dir", ""),
                 r.get("cross_date", ""), r.get("sector_name", ""),
                 json.dumps(r.get("score_detail", []), ensure_ascii=False),
                 r.get("change_pct", 0))
            )
        self._scan_conn.commit()

    def get_scan_results(self, scan_date: str = None) -> dict:
        """获取共享扫描结果，默认取最新一天"""
        if not scan_date:
            row = self._scan_conn.execute(
                "SELECT scan_date FROM scan_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {"date": "", "results": []}
            scan_date = row[0]

        rows = self._scan_conn.execute(
            "SELECT code,name,price,signal,reason,score,stop_price,"
            "rs_score,sector_dir,cross_date,sector_name,score_detail,"
            "COALESCE(change_pct,0),COALESCE(ai_score,0),COALESCE(ai_comment,''),"
            "main_net_today,main_net_5d,main_net_10d "
            "FROM scan_results WHERE scan_date=? ORDER BY score DESC",
            (scan_date,)
        ).fetchall()
        return {
            "date": scan_date,
            "results": [
                {
                    "code":         r[0],
                    "name":         r[1],
                    "price":        r[2],
                    "signal":       r[3],
                    "reason":       r[4],
                    "score":        r[5],
                    "stop_price":   r[6],
                    "rs_score":     r[7] if r[7] is not None else 0.0,
                    "sector_dir":   r[8] or "",
                    "cross_date":   r[9] or "",
                    "sector_name":  r[10] or "",
                    "score_detail": json.loads(r[11]) if r[11] else [],
                    "change_pct":   r[12] if r[12] is not None else 0.0,
                    "ai_score":     r[13] if r[13] is not None else 0.0,
                    "ai_comment":   r[14] or "",
                    "main_net_today": r[15],   # 万元，None=未采集
                    "main_net_5d":    r[16],
                    "main_net_10d":   r[17],
                }
                for r in rows
            ]
        }

    def update_fund_flow(self, fund: dict, scan_date: str = None) -> int:
        """
        把主力净额写回扫描结果（每日扫描后由监控采集，仅展示/供AI评分参考）。
        fund: {code: (今日万, 5日万, 10日万)}；值为 None 的项跳过。
        scan_date 为空时取最新一天。返回更新条数。
        """
        if not scan_date:
            row = self._scan_conn.execute(
                "SELECT scan_date FROM scan_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return 0
            scan_date = row[0]
        n = 0
        for code, v in (fund or {}).items():
            if not v:
                continue
            try:
                cur = self._scan_conn.execute(
                    "UPDATE scan_results SET main_net_today=?, main_net_5d=?, main_net_10d=? "
                    "WHERE scan_date=? AND code=?",
                    (float(v[0]), float(v[1]), float(v[2]), scan_date, code))
                n += cur.rowcount
            except Exception:
                pass
        self._scan_conn.commit()
        return n

    def update_ai_scores(self, scores: dict, scan_date: str = None) -> int:
        """
        把 AI 评分写回扫描结果（仅展示参考，不碰交易）。
        scores: {code: {"ai_score": 0-100, "ai_comment": "理由"}}
        scan_date 为空时取最新一天。返回更新条数。
        """
        if not scan_date:
            row = self._scan_conn.execute(
                "SELECT scan_date FROM scan_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return 0
            scan_date = row[0]
        n = 0
        for code, v in (scores or {}).items():
            try:
                cur = self._scan_conn.execute(
                    "UPDATE scan_results SET ai_score=?, ai_comment=? "
                    "WHERE scan_date=? AND code=?",
                    (float(v.get("ai_score", 0)), str(v.get("ai_comment", "")),
                     scan_date, code))
                n += cur.rowcount
            except Exception:
                pass
        self._scan_conn.commit()
        return n


# 全局单例（懒加载）
# 仅 scanner / 回测等批处理通过 `from trader.paper import paper` 使用，
# 且只调用共享扫描方法（写 market.db）。改为懒加载后，单纯 import 本模块
# （如 Web 服务）不会再创建遗留的 ~/.520quant/paper_trade.db。
_paper_singleton: PaperAccount | None = None


def __getattr__(name):
    # PEP 562：首次访问 trader.paper.paper 时才实例化
    global _paper_singleton
    if name == "paper":
        if _paper_singleton is None:
            _paper_singleton = PaperAccount(init_capital=200_000)
        return _paper_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

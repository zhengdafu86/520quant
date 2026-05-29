"""
520量化系统主入口
用法：
  python run.py                     # 启动监控（含模拟交易）
  python run.py --test 002156       # 测试单只股票分析
  python run.py --status            # 查看当前状态 + 模拟账户余额
  python run.py --report            # 打印模拟账户绩效报告
  python run.py --paper-buy 002156 10.50 100   # 手动模拟买入
  python run.py --paper-sell 002156 11.20      # 手动模拟卖出
  python run.py --reset-paper                  # 重置模拟账户（清空数据）
"""
from __future__ import annotations

import sys
import time

# ── 模式开关 ────────────────────────────────────────────────
PAPER_MODE = True        # True=模拟交易  False=实盘（需接券商API）

# ── 当前持仓配置（每次启动前更新） ─────────────────────────
POSITIONS = [
    {"code": "002156", "name": "通富微电", "cost": 70.8,  "shares": 600},
    {"code": "002625", "name": "光启技术", "cost": 39.4,  "shares": 1300},
    {"code": "000426", "name": "兴业银锡", "cost": 39.9,  "shares": 1000},
    # 特变电工 2026-05-28 止损出局 25.91，亏损 -2990 元(-10.3%)
]

# ── 日线候选股（收盘后扫描填入） ─────────────────────────────
WATCHLIST = [
    {"code": "603002", "name": "宏昌电子", "signal": "候选观察"},
    {"code": "600487", "name": "亨通光电", "signal": "候选观察"},
]


def _build_monitor():
    """构建并注入持仓的监控引擎"""
    from monitor.engine import MonitorEngine
    monitor = MonitorEngine(interval=30, paper_mode=PAPER_MODE)
    for p in POSITIONS:
        monitor.add_position(
            code=p["code"], name=p["name"],
            cost=p["cost"], shares=p["shares"]
        )
    for w in WATCHLIST:
        monitor.add_watch(
            code=w["code"], name=w["name"], signal=w["signal"]
        )
    return monitor


def run_monitor():
    """启动实时监控"""
    monitor = _build_monitor()

    mode_label = "【模拟交易模式】" if PAPER_MODE else "【实盘模式⚠️】"
    print("=" * 60)
    print(f"  520量化监控系统  启动中...  {mode_label}")
    print("=" * 60)

    monitor.status()

    try:
        monitor.start(background=False)
    except KeyboardInterrupt:
        monitor.stop()
        print("\n系统已退出")


def run_test(code: str):
    """测试单只股票的520信号"""
    from data.fetcher import db
    from strategy.signal_520 import strategy
    import urllib.request

    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    try:
        url  = f"https://qt.gtimg.cn/q={prefix}{code}"
        req  = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        raw  = urllib.request.urlopen(req, timeout=5).read().decode("gbk")
        vals = raw.split('"')[1].split("~")
        name  = vals[1] if len(vals) > 1 else code
        price = float(vals[3]) if len(vals) > 3 and vals[3] else 0
    except Exception:
        name  = code
        price = 0

    print(f"\n{'='*50}")
    print(f"  测试分析: {name}({code})  现价={price:.2f}")
    print(f"{'='*50}")

    daily_df = db.get(code, freq="day", bars=60)
    if daily_df.empty:
        print("❌ 日线数据获取失败")
        return

    last = daily_df.iloc[-1]
    print(f"\n【日线数据】")
    print(f"  MA5={last['ma5']:.3f}  MA20={last['ma20']:.3f}  "
          f"斜率={last['ma20_slope']:+.4f}  量比={last['vol_ratio']:.2f}")

    result = strategy.analyze(daily_df)
    print(f"\n【日线520信号】")
    print(f"  信号: {result.signal.value}")
    print(f"  原因: {result.reason}")
    if result.stop_price:
        print(f"  止损: {result.stop_price}")

    print(f"\n【近5根5分钟K】")
    min_df = db.get(code, freq="5m", bars=20)
    if not min_df.empty:
        for _, row in min_df.tail(5).iterrows():
            print(f"  {str(row['datetime'])[11:16]}  "
                  f"收={row['close']:.2f}  量={int(row['vol'])}")
    else:
        print("  分钟数据获取失败（非交易时段正常）")

    print(f"\n{'='*50}\n")


def run_status():
    """打印当前状态（含模拟账户余额），不启动监控"""
    monitor = _build_monitor()
    monitor.status()


def run_report():
    """打印模拟账户绩效报告"""
    from trader.paper import PaperAccount
    paper = PaperAccount()
    paper.print_performance()
    paper.print_summary()


def run_paper_buy(code: str, price: float, shares: int):
    """手动模拟买入（用于初始化仓位 / 调试）"""
    from trader.paper import PaperAccount
    # 查询股票名称
    import urllib.request
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    try:
        url  = f"https://qt.gtimg.cn/q={prefix}{code}"
        req  = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        raw  = urllib.request.urlopen(req, timeout=5).read().decode("gbk")
        vals = raw.split('"')[1].split("~")
        name = vals[1] if len(vals) > 1 else code
    except Exception:
        name = code

    paper = PaperAccount()
    ok, msg = paper.buy(code, name, price, shares, signal="手动买入")
    print(f"{'✅' if ok else '❌'} {msg}")
    if ok:
        paper.print_summary()


def run_paper_sell(code: str, price: float):
    """手动模拟卖出"""
    from trader.paper import PaperAccount
    paper = PaperAccount()
    ok, msg = paper.sell(code, price, signal="手动卖出")
    print(f"{'✅' if ok else '❌'} {msg}")
    if ok:
        paper.print_summary()
        paper.print_performance()


def run_reset_paper():
    """重置模拟账户（危险操作，需二次确认）"""
    from pathlib import Path
    db_path = Path.home() / ".520quant" / "paper_trade.db"
    confirm = input(f"确认删除模拟账户数据？({db_path}) [y/N]: ").strip().lower()
    if confirm == "y":
        if db_path.exists():
            db_path.unlink()
            print("✅ 模拟账户已重置")
        else:
            print("账户文件不存在，无需重置")
    else:
        print("已取消")


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--test" in args:
        idx  = args.index("--test")
        code = args[idx + 1] if idx + 1 < len(args) else "002156"
        run_test(code)

    elif "--status" in args:
        run_status()

    elif "--report" in args:
        run_report()

    elif "--paper-buy" in args:
        idx = args.index("--paper-buy")
        try:
            code   = args[idx + 1]
            price  = float(args[idx + 2])
            shares = int(args[idx + 3])
            run_paper_buy(code, price, shares)
        except (IndexError, ValueError):
            print("用法: python run.py --paper-buy <代码> <价格> <股数>")
            print("示例: python run.py --paper-buy 002156 72.00 600")

    elif "--paper-sell" in args:
        idx = args.index("--paper-sell")
        try:
            code  = args[idx + 1]
            price = float(args[idx + 2])
            run_paper_sell(code, price)
        except (IndexError, ValueError):
            print("用法: python run.py --paper-sell <代码> <价格>")
            print("示例: python run.py --paper-sell 002156 78.50")

    elif "--reset-paper" in args:
        run_reset_paper()

    else:
        run_monitor()

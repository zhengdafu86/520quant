"""
520量化系统主入口
用法：
  python run.py                     # 启动监控（含模拟交易）
  python run.py --test 002156       # 测试单只股票分析
  python run.py --status            # 查看当前状态 + 模拟账户余额
  python run.py --report            # 打印模拟账户绩效报告
  python run.py --backtest          # 回测自选股（近1年，最多5仓）
  python run.py --paper-buy 002156 10.50 100   # 手动模拟买入
  python run.py --paper-sell 002156 11.20      # 手动模拟卖出
  python run.py --reset-paper                  # 重置模拟账户（清空数据）
"""
from __future__ import annotations

import sys
import time

# ── 模式开关 ────────────────────────────────────────────────
PAPER_MODE = True        # True=模拟交易  False=实盘（需接券商API）

# ── 持仓从 paper 账户自动读取，无需在此硬编码 ─────────────────
# 使用 run.py --paper-buy <code> <price> <shares> 手动买入后会自动同步
POSITIONS = []   # 保留字段以备手动临时注入，正常运行请留空

# ── 日线候选股（收盘后扫描填入） ─────────────────────────────
WATCHLIST = [
    {"code": "603002", "name": "宏昌电子", "signal": "候选观察", "priority": "P1"},
    {"code": "600487", "name": "亨通光电", "signal": "候选观察", "priority": "P2"},
    # priority: "P1"=最优先 / "P2"=次优先 / "P3"=普通
    # 仓位有限时，P1先占坑，P2次之，P3再次，自动扫描的股票排最后
]


def _build_monitor():
    """构建监控引擎，持仓从 paper 账户同步"""
    from monitor.engine import MonitorEngine
    from trader.paper import PaperAccount

    monitor = MonitorEngine(interval=30, paper_mode=PAPER_MODE)
    monitor.load_from_paper()

    for p in POSITIONS:   # 通常为空，手动指定时才生效
        monitor.add_position(
            code=p["code"], name=p["name"],
            cost=p["cost"], shares=p["shares"]
        )

    paper        = PaperAccount()
    db_watchlist = paper.get_watchlist()   # 网页自选股（含优先级）
    db_pri_map   = {w["code"]: w.get("priority", "") for w in db_watchlist}

    # ① 先加载 run.py 里硬编码的 WATCHLIST（优先级以此为准）
    loaded_codes: set[str] = set()
    for w in WATCHLIST:
        priority = w.get("priority") or db_pri_map.get(w["code"], "")
        monitor.add_watch(
            code=w["code"], name=w["name"], signal=w["signal"],
            priority=priority
        )
        loaded_codes.add(w["code"])

    # ② 加载网页 DB 自选股（尚未在 ① 里的 + 尚未持仓的）
    # 这是自动买入的主要来源：用户在页面扫描后点「+ 自选」的股票
    for w in db_watchlist:
        code = w["code"]
        if code in loaded_codes:
            continue                           # 已在 ① 里
        if code in monitor.positions:
            continue                           # 已在持仓，不重复加
        monitor.add_watch(
            code=code,
            name=w["name"],
            signal=w.get("signal", "候选"),
            priority=w.get("priority", ""),
        )
        loaded_codes.add(code)

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


def run_backtest(start_date: str = "", end_date: str = ""):
    """
    回测自选股
    start_date: "2025-06-01"（不传则默认近250个交易日）
    end_date:   "2026-05-29"（不传则到最新数据）
    """
    from trader.paper import PaperAccount
    from backtest.engine import Backtester

    # 股票池：paper DB 自选 + run.py WATCHLIST 合并去重
    paper    = PaperAccount()
    db_watch = paper.get_watchlist()
    extra    = [{"code": w["code"], "name": w["name"]} for w in WATCHLIST]
    seen     = set()
    stocks   = []
    for s in db_watch + extra:
        if s["code"] not in seen:
            seen.add(s["code"])
            stocks.append({"code": s["code"], "name": s["name"]})

    if not stocks:
        print("自选股为空，请先添加股票再回测")
        return

    # 优先级映射：DB 自选股优先级（网页设置）+ run.py WATCHLIST（后者覆盖前者）
    priority_map: dict[str, str] = {}
    for w in db_watch:
        if w.get("priority"):
            priority_map[w["code"]] = w["priority"]
    for w in WATCHLIST:
        if w.get("priority"):
            priority_map[w["code"]] = w["priority"]

    bt = Backtester(init_capital=200_000, max_positions=4)
    bt.run(stocks, bars=250, start_date=start_date, end_date=end_date,
           priority_map=priority_map)


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

    elif "--backtest" in args:
        idx        = args.index("--backtest")
        start_date = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith("--") else ""
        end_date   = args[idx + 2] if idx + 2 < len(args) and not args[idx + 2].startswith("--") else ""
        run_backtest(start_date=start_date, end_date=end_date)

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

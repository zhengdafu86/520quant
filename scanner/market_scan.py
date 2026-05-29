"""
市场扫描器 - 每日收盘后自动扫描全A股
筛选符合520战法买点的候选股（Top 20）
"""
from __future__ import annotations

import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Optional

from data.fetcher import db
from strategy.signal_520 import strategy, Signal
from alert.notifier import log, _push, _date


# ── 过滤参数 ──────────────────────────────────────────
SCAN_MIN_PRICE    = 5.0      # 最低股价（元）
SCAN_MAX_PRICE    = 200.0    # 最高股价（元）
SCAN_MIN_TURNOVER = 5000.0   # 日成交额下限（万元），过滤小盘/低流动性
SCAN_MIN_DAYS     = 60       # 上市至少 60 个交易日（过滤次新股）
SCAN_MAX_RESULTS  = 20       # 最多推送候选数
SCAN_WORKERS      = 10       # 并发分析线程数


# ── 批量报价 ──────────────────────────────────────────

def _batch_quotes(codes: list[str]) -> dict[str, dict]:
    """
    腾讯 API 批量实时报价
    返回 {code: {name, price, amount_wan}}
    """
    BATCH = 80
    result: dict[str, dict] = {}

    for i in range(0, len(codes), BATCH):
        batch = codes[i: i + BATCH]
        items = [
            ("sh" if c.startswith(("6", "9")) else "sz") + c
            for c in batch
        ]
        url = "https://qt.gtimg.cn/q=" + ",".join(items)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")

            for line in raw.strip().split("\n"):
                if '="' not in line:
                    continue
                try:
                    inner = line.split('="')[1].rstrip('";')
                    vals  = inner.split("~")
                    if len(vals) < 38:
                        continue
                    code   = vals[2][2:]          # 去掉 sh/sz 前缀
                    name   = vals[1]
                    price  = float(vals[3])  if vals[3]  else 0.0
                    amount = float(vals[37]) if vals[37] else 0.0  # 成交额（万）
                    if code:
                        result[code] = {"name": name, "price": price, "amount_wan": amount}
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            log(f"批量报价请求失败 ({batch[0]}...): {e}", "WARN")

        time.sleep(0.08)   # 限速，避免被封

    return result


# ── 扫描器 ────────────────────────────────────────────

class MarketScanner:

    # ── 获取全市场代码 ──────────────────────────────

    def _get_all_codes(self) -> list[str]:
        """优先用 mootdx 获取，失败则用内置范围"""
        try:
            from mootdx.quotes import Quotes
            client = Quotes.factory(market='std')
            codes: list[str] = []
            for market in (0, 1):     # 0=深圳  1=上海
                offset = 0
                while True:
                    batch = client.security_list(market=market, start=offset)
                    if batch is None or len(batch) == 0:
                        break
                    rows = batch if isinstance(batch, list) else batch.to_dict("records")
                    for s in rows:
                        code = str(s.get("code", "")).strip().zfill(6)
                        if code and code != "000000":
                            codes.append(code)
                    offset += len(rows)
                    if len(rows) < 1000:
                        break
            log(f"mootdx 获取 {len(codes)} 只股票", "INFO")
            return list(set(codes))
        except Exception as e:
            log(f"mootdx 获取列表失败: {e}，切换内置范围", "WARN")
            return self._builtin_codes()

    @staticmethod
    def _builtin_codes() -> list[str]:
        """内置 A 股代码范围（备用）"""
        codes = []
        for i in range(1, 3800):          # 深圳主板 000001~003799
            codes.append(str(i).zfill(6))
        for i in range(300001, 301800):   # 创业板
            codes.append(str(i))
        for i in range(600000, 605000):   # 上海主板
            codes.append(str(i))
        for i in range(688001, 688800):   # 科创板
            codes.append(str(i))
        return codes

    # ── 预过滤 ──────────────────────────────────────

    def _pre_filter(self, codes: list[str]) -> list[tuple[str, str, float]]:
        """
        批量报价 → 排除 ST / 次新 / 小盘 / 停牌 / 价格越界
        返回 [(code, name, price), ...]
        """
        log(f"预过滤: {len(codes)} 只 → 批量报价中...", "INFO")
        quotes = _batch_quotes(codes)

        passed = []
        for code, q in quotes.items():
            name   = q.get("name", "")
            price  = q.get("price", 0.0)
            amount = q.get("amount_wan", 0.0)

            if not name or price <= 0:
                continue                                       # 无效/停牌
            if any(kw in name for kw in ("ST", "退", "N ", "C ")):
                continue                                       # ST / 退市 / 次新
            if not (SCAN_MIN_PRICE <= price <= SCAN_MAX_PRICE):
                continue                                       # 价格越界
            if amount < SCAN_MIN_TURNOVER:
                continue                                       # 成交额过小
            passed.append((code, name, price))

        log(f"预过滤后剩 {len(passed)} 只", "INFO")
        return passed

    # ── 单股分析 ────────────────────────────────────

    def _analyze_one(self, code: str, name: str,
                     price: float) -> Optional[dict]:
        """对单只股票运行 520 信号，无买点返回 None"""
        try:
            df = db.get(code, freq="day", bars=65)
            if df.empty or len(df) < SCAN_MIN_DAYS:
                return None      # 数据不足 → 次新股
            result = strategy.analyze(df)
            if result.signal not in (
                Signal.BUY_GOLDEN_CROSS,
                Signal.BUY_PULLBACK,
                Signal.BUY_SQUEEZE,
            ):
                return None
            return {
                "code":       code,
                "name":       name,
                "price":      price,
                "signal":     result.signal.value,
                "reason":     result.reason,
                "score":      result.score or 0,
                "stop_price": result.stop_price or round(price * 0.95, 2),
            }
        except Exception:
            return None

    # ── 主扫描 ──────────────────────────────────────

    def scan(self) -> list[dict]:
        """完整扫描流程，返回 Top 候选列表"""
        log("🔍 市场扫描开始...", "INFO")
        t0 = time.time()

        codes    = self._get_all_codes()
        filtered = self._pre_filter(codes)

        # 多线程并发分析
        results: list[dict] = []
        done = 0
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            futures = {
                pool.submit(self._analyze_one, c, n, p): c
                for c, n, p in filtered
            }
            for fut in as_completed(futures):
                done += 1
                if done % 100 == 0:
                    log(f"  分析进度 {done}/{len(filtered)}...", "INFO")
                r = fut.result()
                if r:
                    results.append(r)

        results.sort(key=lambda x: x["score"], reverse=True)
        top = results[:SCAN_MAX_RESULTS]

        elapsed = time.time() - t0
        log(f"🔍 扫描完成: 有信号 {len(results)} 只，"
            f"取 Top{len(top)}，耗时 {elapsed:.0f}s", "INFO")
        return top

    # ── 推送 + 存库 ─────────────────────────────────

    def notify_and_save(self, results: list[dict]):
        """推送企业微信 + 写入 scan_results 表"""
        from trader.paper import paper
        scan_date = date.today().isoformat()
        paper.save_scan_results(scan_date, results)

        ICONS = {"金叉": "✅", "回踩": "🔄", "压缩": "🔀"}
        if results:
            lines = []
            for r in results:
                icon = ICONS.get(r["signal"], "⭕")
                lines.append(
                    f"{icon} **{r['name']}**({r['code']})  "
                    f"{r['price']:.2f}  止损{r['stop_price']:.2f}\n"
                    f"　{r['signal']} | {r['reason'][:35]}"
                )
            body = "\n\n".join(lines)
        else:
            body = "_今日暂无符合520买点的候选股_"

        title = f"🔍 每日扫描  {_date()}  共{len(results)}只"
        _push(title, body, level="INFO")
        log(f"扫描推送完成，{len(results)} 只候选", "INFO")

    def run(self) -> list[dict]:
        """一键扫描 + 推送，返回结果"""
        results = self.scan()
        self.notify_and_save(results)
        return results


# 全局单例
scanner = MarketScanner()

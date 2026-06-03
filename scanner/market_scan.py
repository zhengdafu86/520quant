"""
市场扫描器 - 每日收盘后自动扫描全A股
筛选符合520战法买点的候选股（Top 20）

三重过滤 + 两项参考指标：
  ① 基础预过滤（价格 / ST / 成交额）
  ② 基本面粗筛（流通市值 30-800亿 / 非亏损股 / PE≤200）
  ③ 技术信号（520战法：金叉 / 回踩 / 粘合发散）
  ── 参考展示（不过滤）──
  ④ 行业ETF MA20 方向（⬆/⬇/➡，辅助人工判断是否顺势）
  ⑤ 相对强度 RS（个股20日收益 - 沪深300，RS>0优先排序）

待扩展（需引入 akshare）：
  - 业绩增长：近两季度净利润同比增长
  - 北向资金：外资连续净买入
"""
from __future__ import annotations

import time
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from data.fetcher import db
from data.sector_map import get_sector_etf, get_sector_etf_by_industry
from strategy.signal_520 import strategy, Signal
from alert.notifier import log, _push, _date


# ── 行业名持久化缓存（兜底：API 取不到行业时复用历史值）──────────────
# 申万行业分类基本静态，缓存一次几乎不会过时；把"偶发 API 失败"变为非问题。
_SECTOR_CACHE_PATH = Path.home() / ".520quant" / "sector_cache.json"


def _load_industry_cache() -> dict[str, str]:
    """加载 code→行业名 持久缓存"""
    try:
        if _SECTOR_CACHE_PATH.exists():
            data = json.loads(_SECTOR_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v}
    except Exception:
        pass
    return {}


def _save_industry_cache(cache: dict[str, str]):
    """写回 code→行业名 持久缓存"""
    try:
        _SECTOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SECTOR_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log(f"行业缓存写入失败（不影响扫描）: {e}", "WARN")


# ── 不可交易板块过滤（创业板 300/301、科创板 688/689）────────────
# 账户无对应权限，直接从扫描源头剔除，连分析都不做。
def _is_excluded_board(code: str) -> bool:
    return str(code).startswith(("300", "301", "688", "689"))


# ── 基础过滤参数 ──────────────────────────────────────
SCAN_MIN_PRICE    = 5.0      # 最低股价（元）
SCAN_MAX_PRICE    = 200.0    # 最高股价（元）
SCAN_MIN_TURNOVER = 5000.0   # 日成交额下限（万元），过滤小盘/低流动性
SCAN_MIN_DAYS     = 60       # 上市至少 60 个交易日（过滤次新股）
SCAN_MAX_RESULTS  = 20       # 最多推送候选数
SCAN_WORKERS      = 10       # 并发分析线程数

# ── 基本面粗筛参数 ────────────────────────────────────
# 流通市值估算 = 成交额(万) / 换手率(%) / 100（亿元）
SCAN_MIN_CAP      = 30.0     # 最小流通市值（亿）：<30亿流动性极差，主力不进
SCAN_MAX_CAP      = 800.0    # 最大流通市值（亿）：>800亿大盘股波动慢

# PE 过滤（腾讯 API vals[39] = TTM 动态市盈率）
# pe < 0  → 亏损股         → 排除（核心红线，不买亏损公司）
# pe == 0 → 数据缺失       → 放行（保守，不误杀）
# pe > 200 → 纯投机泡沫    → 排除
# 注意：A 股成长股（半导体/医疗器械/消费科技）PE 100-150 是正常区间
#       把上限设 100 会错杀大量有真实业绩的成长股，200 更合理
SCAN_MAX_PE       = 200.0    # PE 上限；>200 视为纯投机，无安全边际


# ── 批量报价 ──────────────────────────────────────────

def _batch_quotes(codes: list[str]) -> dict[str, dict]:
    """
    腾讯 API 批量实时报价（含重试）
    返回 {code: {name, price, amount_wan, turnover_pct, pe}}

    pe = TTM动态市盈率（vals[39]）：< 0 亏损 / 0 缺失 / >0 正常
    注：腾讯 qt.gtimg.cn 不含行业分类字段，行业由东财 API 单独获取

    可靠性保障：
      - 每批失败最多重试 2 次（间隔 0.5s / 1.5s），避免单次网络抖动漏批
      - 非交易时段 amount_wan 可能为 0（实时累计量），
        此时取 vals[36]（前收成交额）兜底，保证过滤一致性
    """
    BATCH    = 80
    MAX_RETRY = 2          # 每批最多重试次数
    result: dict[str, dict] = {}

    for i in range(0, len(codes), BATCH):
        batch = codes[i: i + BATCH]
        items = [
            ("sh" if c.startswith(("6", "9", "5")) else "sz") + c
            for c in batch
        ]
        url = "https://qt.gtimg.cn/q=" + ",".join(items)

        raw = None
        for attempt in range(MAX_RETRY + 1):
            try:
                req = urllib.request.Request(url)
                req.add_header("User-Agent", "Mozilla/5.0")
                raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
                break   # 成功
            except Exception as e:
                if attempt < MAX_RETRY:
                    wait = 0.5 * (attempt + 1)   # 0.5s / 1.0s
                    log(
                        f"批量报价第{attempt+1}次失败 ({batch[0]}...)，"
                        f"{wait:.1f}s 后重试: {e}",
                        "WARN"
                    )
                    time.sleep(wait)
                else:
                    log(f"批量报价彻底失败 ({batch[0]}...)，跳过此批: {e}", "WARN")

        if raw is None:
            time.sleep(0.08)
            continue

        for line in raw.strip().split("\n"):
            if '="' not in line:
                continue
            try:
                inner = line.split('="')[1].rstrip('";')
                vals  = inner.split("~")
                if len(vals) < 38:
                    continue
                code         = vals[2]
                name         = vals[1]
                price        = float(vals[3])  if vals[3]  else 0.0
                # vals[37] = 当日成交额（万）
                # 非交易时段/未开市时该值为 0，用 -1 标记"未知"
                # _pre_filter 中遇到 amount=-1 会跳过成交额过滤，交由 DB 分析决定
                raw_amount   = vals[37] if vals[37] else ""
                amount       = float(raw_amount) if raw_amount else -1.0
                turnover_pct = float(vals[38]) if len(vals) > 38 and vals[38] else 0.0
                # vals[39] = TTM动态市盈率
                pe_raw = vals[39] if len(vals) > 39 else ""
                pe     = float(pe_raw) if pe_raw.strip() else 0.0
                if code:
                    result[code] = {
                        "name":         name,
                        "price":        price,
                        "amount_wan":   amount,
                        "turnover_pct": turnover_pct,
                        "pe":           pe,
                    }
            except (ValueError, IndexError):
                continue

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
        """内置 A 股代码范围（备用）——仅主板，不含创业板/科创板（不可交易）"""
        codes = []
        for i in range(1, 3800):          # 深圳主板 000001~003799
            codes.append(str(i).zfill(6))
        for i in range(600000, 605000):   # 上海主板
            codes.append(str(i))
        return codes

    # ── 预过滤 ──────────────────────────────────────

    def _pre_filter(self, codes: list[str]) -> list[tuple[str, str, float]]:
        """
        批量报价 → 三层排除（按顺序，先快后慢）：
          ① 基础：无效/停牌 / ST/退市/次新 / 价格越界 / 成交额过小
          ② 基本面-PE：亏损（pe<0）/ 纯投机泡沫（pe>200）/ pe=0放行
          ③ 基本面-市值：流通市值 < 30亿 或 > 800亿
        返回 [(code, name, price), ...]
        """
        # 板块过滤：剔除不可交易的创业板/科创板（账户无权限）
        _before = len(codes)
        codes = [c for c in codes if not _is_excluded_board(c)]
        if _before != len(codes):
            log(f"板块过滤: 排除创业板/科创板 {_before - len(codes)} 只（不可交易）", "INFO")

        log(f"预过滤: {len(codes)} 只 → 批量报价中...", "INFO")
        quotes = _batch_quotes(codes)

        passed     = []
        skip_basic = 0   # 基础过滤
        skip_pe    = 0   # PE 过滤
        skip_cap   = 0   # 市值过滤

        for code, q in quotes.items():
            name         = q.get("name", "")
            price        = q.get("price", 0.0)
            amount       = q.get("amount_wan", 0.0)
            turnover_pct = q.get("turnover_pct", 0.0)
            pe           = q.get("pe", 0.0)

            # ① 基础过滤 ─────────────────────────────────
            if not name or price <= 0:
                skip_basic += 1
                continue                                       # 无效/停牌
            if any(kw in name for kw in ("ST", "退", "N ", "C ")):
                skip_basic += 1
                continue                                       # ST / 退市 / 次新
            if not (SCAN_MIN_PRICE <= price <= SCAN_MAX_PRICE):
                skip_basic += 1
                continue                                       # 价格越界
            # amount == -1 表示非交易时段无成交额数据，跳过该过滤保证稳定性
            if amount >= 0 and amount < SCAN_MIN_TURNOVER:
                skip_basic += 1
                continue                                       # 成交额过小

            # ② 基本面-PE 粗筛 ───────────────────────────
            # pe == 0：数据缺失，放行（保守，不误杀有效股）
            if pe < 0:
                skip_pe += 1
                continue                                       # 亏损股，无安全边际
            if 0 < pe > SCAN_MAX_PE:                          # pe > 100 且非缺失
                skip_pe += 1
                continue                                       # 投机泡沫，PE 过高

            # ③ 基本面-流通市值估算 ──────────────────────
            # float_cap_亿 ≈ amount_万 / (turnover_pct × 100)
            # amount == -1（非交易时段无数据）→ 跳过市值估算，保证稳定性
            if turnover_pct > 0.01 and amount > 0:
                float_cap = amount / (turnover_pct * 100)
                if not (SCAN_MIN_CAP <= float_cap <= SCAN_MAX_CAP):
                    skip_cap += 1
                    continue

            passed.append((code, name, price))

        log(
            f"预过滤后剩 {len(passed)} 只 | "
            f"基础 -{skip_basic} / PE -{skip_pe} / 市值 -{skip_cap}",
            "INFO"
        )
        return passed

    # ── 单股分析 ────────────────────────────────────

    def _analyze_one(
        self,
        code: str,
        name: str,
        price: float,
        sector_direction: str = "unknown",
        market_20d_ret: float = 0.0,
    ) -> Optional[dict]:
        """
        对单只股票运行 520 信号，无买点或不满足过滤条件返回 None。

        sector_direction : 该股对应行业ETF的MA20方向（仅展示，不过滤）
                           'up' / 'down' / 'flat' / 'unknown'
        market_20d_ret   : 大盘（沪深300 ETF）20日涨幅%，用于计算相对强度
        """
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

            # 当日涨跌幅（收盘 vs 昨收）
            change_pct = 0.0
            if len(df) >= 2:
                prev_close = float(df.iloc[-2]["close"])
                cur_close  = float(df.iloc[-1]["close"])
                if prev_close > 0:
                    change_pct = round((cur_close - prev_close) / prev_close * 100, 2)

            # 相对强度（RS）计算：个股20日涨幅 - 大盘20日涨幅
            rs_score = 0.0
            if len(df) >= 21:
                c_now = float(df.iloc[-1]["close"])
                c_20d = float(df.iloc[-21]["close"])
                if c_20d > 0:
                    stock_20d_ret = (c_now - c_20d) / c_20d * 100
                    rs_score = round(stock_20d_ret - market_20d_ret, 2)

            # RS 纳入主评分：强势股加分，弱势股扣分（不影响 rs_score 展示字段）
            base_score = result.score or 0
            rs_bonus = (
                10 if rs_score >= 10 else
                 5 if rs_score >=  3 else
                -5 if rs_score <  -5 else
                 0
            )
            final_score = min(100, max(0, base_score + rs_bonus))

            # score_detail 追加 RS 加减分项（复制列表，不修改原 SignalResult）
            score_detail = list(result.score_detail or [])
            if rs_bonus != 0:
                rs_label = (
                    f"RS+{rs_score:.1f}%跑赢大盘" if rs_bonus > 0 else
                    f"RS{rs_score:.1f}%跑输大盘"
                )
                score_detail.append((rs_bonus, rs_label))

            return {
                "code":         code,
                "name":         name,
                "price":        price,
                "change_pct":   change_pct,         # 当日涨跌幅%
                "signal":       result.signal.value,
                "reason":       result.reason,
                "score":        final_score,
                "score_detail": score_detail,       # 评分明细（含 RS）
                "stop_price":   result.stop_price or round(price * 0.95, 2),
                "rs_score":     rs_score,           # 相对强度分（正=跑赢大盘，仅展示）
                "sector_dir":   sector_direction,   # 板块方向（仅展示参考）
                "sector_name":  "",                 # 申万行业名（由 _enrich_sector_dir 填充）
                "cross_date":   result.cross_date,  # 金叉形成日期
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

        # ── 预取大盘20日收益率（RS基准）────────────────
        log("预取大盘20日收益率（沪深300 ETF 510300）...", "INFO")
        market_20d_ret = db.get_20d_return("510300")
        log(f"  大盘20日收益: {market_20d_ret:+.2f}%", "INFO")

        # ── 预取行业ETF MA20方向（名称关键词粗匹配，纯本地，无网络）──
        # 目的是建好 etf_dir_cache，分析时先填一个方向；
        # 扫描结束后再用东财单股 API 精确覆盖（仅对有信号的股票）
        needed_etfs: set[str] = set()
        for _, name, _ in filtered:
            etf = get_sector_etf(name)
            if etf:
                needed_etfs.add(etf)

        etf_dir_cache: dict[str, str] = {}
        if needed_etfs:
            log(f"预取行业ETF MA20方向（名称匹配）: {len(needed_etfs)} 个...", "INFO")
            for etf_code in sorted(needed_etfs):
                direction = db.get_ma20_direction(etf_code)
                etf_dir_cache[etf_code] = direction
                icon = {"up": "⬆", "down": "⬇", "flat": "➡", "unknown": "❓"}.get(
                    direction, "?"
                )
                log(f"  ETF {etf_code}: {direction} {icon}", "INFO")

        # ── 多线程并发分析 ──────────────────────────────
        results: list[dict] = []
        done = 0
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            futures: dict = {}
            for c, n, p in filtered:
                etf     = get_sector_etf(n)
                sec_dir = etf_dir_cache.get(etf, "unknown") if etf else "unknown"
                fut = pool.submit(self._analyze_one, c, n, p, sec_dir, market_20d_ret)
                futures[fut] = c

            for fut in as_completed(futures):
                done += 1
                if done % 100 == 0:
                    log(f"  分析进度 {done}/{len(filtered)}...", "INFO")
                r = fut.result()
                if r:
                    results.append(r)

        # ── 东财单股 API 精确补充行业方向（仅对有信号的 ~20-50 只）──
        # 并发扫描结束后稍作等待，避免请求高峰期东财 API 被截断
        if results:
            time.sleep(2)
            log(f"东财 API 精确补充行业方向: {len(results)} 只...", "INFO")
            self._enrich_sector_dir(results, etf_dir_cache)

        # 双维排序：信号强度（score）→ 相对强度（rs_score），均降序
        results.sort(key=lambda x: (x["score"], x.get("rs_score", 0)), reverse=True)

        elapsed = time.time() - t0
        log(
            f"🔍 扫描完成: 有信号 {len(results)} 只，耗时 {elapsed:.0f}s",
            "INFO"
        )
        return results

    # ── 东财批量行业数据 ─────────────────────────────

    @staticmethod
    def _fetch_industry_batch() -> dict[str, str]:
        """
        东方财富批量接口，一次扫全A股申万行业分类。
        每页 200 只，约 25-30 次请求覆盖全市场（~5000只），耗时 ~5s。
        返回 {code(6位): industry_name}，如 {"600519": "白酒Ⅱ", ...}
        """
        import json as _json

        result: dict[str, str] = {}
        page = 1
        # fs 参数：沪深主板 + 创业板 + 科创板
        # f12=代码  f100=申万行业（L2）  每页上限100条
        url_tpl = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            "?fltt=2&invt=2&np=1&po=1&pz=100&fid=f3"
            "&fs=m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23"
            "&fields=f12,f100&pn={page}"
        )

        while True:
            try:
                req = urllib.request.Request(url_tpl.format(page=page))
                req.add_header("User-Agent", "Mozilla/5.0")
                raw   = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
                data  = _json.loads(raw).get("data") or {}
                items = data.get("diff") or []

                if not items:
                    break  # 已取完

                for item in items:
                    code = str(item.get("f12") or "").zfill(6)
                    ind  = str(item.get("f100") or "").strip()
                    if code and code != "000000" and ind and ind not in ("0", "-"):
                        result[code] = ind

                if len(items) < 100:
                    break  # 最后一页（不足100条）
                page += 1
                time.sleep(0.12)   # 限速

            except Exception as e:
                log(f"批量行业数据 page={page} 失败: {e}", "WARN")
                break

        log(f"东财批量行业: 共获取 {len(result)} 只股票行业信息（{page} 页）", "INFO")
        return result

    # ── 东财 API 行业补充 ────────────────────────────

    @staticmethod
    def _fetch_industries_for_codes(codes: list[str]) -> dict[str, str]:
        """
        东财 ulist.np API，一次请求获取指定股票的申万行业（f100 字段）。
        失败时最多重试 3 次（间隔 2s / 4s / 8s），应对扫描高峰期被截断的情况。
        返回 {code(6位): industry_name}，如 {"600519": "白酒Ⅱ", ...}
        """
        import json as _json
        if not codes:
            return {}
        secids = ",".join(
            ("1" if c.startswith(("6", "9", "5")) else "0") + "." + c
            for c in codes
        )
        url = (
            "https://push2.eastmoney.com/api/qt/ulist.np/get"
            f"?fltt=2&invt=2&secids={secids}&fields=f12,f100"
        )
        MAX_RETRY = 3
        for attempt in range(MAX_RETRY + 1):
            try:
                req = urllib.request.Request(url)
                req.add_header("User-Agent", "Mozilla/5.0")
                raw   = urllib.request.urlopen(req, timeout=12).read().decode("utf-8")
                items = (_json.loads(raw).get("data") or {}).get("diff") or []
                result: dict[str, str] = {}
                for item in items:
                    code = str(item.get("f12") or "").zfill(6)
                    ind  = str(item.get("f100") or "").strip()
                    if code and ind and ind not in ("0", "-"):
                        result[code] = ind
                log(f"ulist行业查询: {len(result)}/{len(codes)} 只有行业数据", "INFO")
                return result
            except Exception as e:
                if attempt < MAX_RETRY:
                    wait = 2 ** (attempt + 1)   # 2s / 4s / 8s
                    log(f"ulist行业查询第{attempt+1}次失败，{wait}s 后重试: {e}", "WARN")
                    time.sleep(wait)
                else:
                    log(f"ulist行业查询彻底失败（{MAX_RETRY+1}次）: {e}", "WARN")
        return {}

    @staticmethod
    def _fetch_em_industry(code: str) -> str:
        """
        单只股票申万行业名称（f127 字段）——仅作 ulist 批量失败时的兜底。
        """
        import json as _json
        prefix = "1" if code.startswith(("6", "9", "5")) else "0"
        url    = (
            f"https://push2.eastmoney.com/api/qt/stock/get"
            f"?fltt=2&invt=2&secid={prefix}.{code}&fields=f127"
        )
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            raw  = urllib.request.urlopen(req, timeout=5).read().decode("utf-8")
            data = _json.loads(raw).get("data") or {}
            return (data.get("f127") or "").strip()
        except Exception:
            return ""

    def _enrich_sector_dir(
        self,
        results: list[dict],
        etf_dir_cache: dict[str, str],
    ):
        """
        对有技术信号的股票，获取申万行业名称，再查对应 ETF MA20 方向。
        策略：优先用 ulist 批量 API（f100，一次请求）；失败则逐只兜底。
        """
        # 1. 批量获取行业（一次请求，比逐只 f127 稳定得多）
        codes = [r["code"] for r in results]
        code_industry: dict[str, str] = self._fetch_industries_for_codes(codes)

        # 逐只兜底：补充批量请求未返回的股票（理论上极少触发）
        missing = [r for r in results if not code_industry.get(r["code"])]
        if missing:
            log(f"逐只兜底补充行业: {len(missing)} 只...", "INFO")

            def _fetch_one(r: dict) -> tuple[str, str]:
                time.sleep(0.05)
                return r["code"], self._fetch_em_industry(r["code"])

            with ThreadPoolExecutor(max_workers=5) as pool:
                for code, ind in pool.map(_fetch_one, missing):
                    if ind:
                        code_industry[code] = ind

        # 1.5 持久化缓存兜底：API（批量+逐只）仍取不到的，复用历史成功值
        persist_cache = _load_industry_cache()
        from_cache = 0
        for r in results:
            code = r["code"]
            if not code_industry.get(code) and persist_cache.get(code):
                code_industry[code] = persist_cache[code]
                from_cache += 1
        if from_cache:
            log(f"行业缓存兜底: {from_cache} 只复用历史行业名（本次 API 未返回）", "INFO")

        # 把本次新取到的行业名并入缓存并持久化（供下次 API 失败时兜底）
        updated = False
        for code, ind in code_industry.items():
            if ind and persist_cache.get(code) != ind:
                persist_cache[code] = ind
                updated = True
        if updated:
            _save_industry_cache(persist_cache)

        # 统计匹配情况
        matched   = 0
        unmatched = []
        for r in results:
            ind = code_industry.get(r["code"], "")
            if ind:
                etf = get_sector_etf_by_industry(ind)
                if etf:
                    matched += 1
                    log(f"  {r['name']}({r['code']}) 行业={ind} → ETF {etf}", "INFO")
                else:
                    unmatched.append(f"{r['name']}({r['code']})={ind}")
            else:
                unmatched.append(f"{r['name']}({r['code']})=API无数据")

        if unmatched:
            log(f"⚠️ 以下股票行业未匹配ETF，板块方向将显示unknown：", "WARN")
            for s in unmatched:
                log(f"    {s}", "WARN")
        log(f"行业匹配: {matched}/{len(results)} 只有板块数据", "INFO")

        # 2. 找出尚未缓存的 ETF
        new_etfs: set[str] = set()
        for code, ind in code_industry.items():
            etf = get_sector_etf_by_industry(ind)
            if etf and etf not in etf_dir_cache:
                new_etfs.add(etf)

        if new_etfs:
            log(f"补充 ETF MA20方向: {sorted(new_etfs)}", "INFO")
            for etf_code in sorted(new_etfs):
                direction = db.get_ma20_direction(etf_code)
                etf_dir_cache[etf_code] = direction
                icon = {"up": "⬆", "down": "⬇", "flat": "➡"}.get(direction, "❓")
                log(f"  ETF {etf_code}: {direction} {icon}", "INFO")

        # 3. 更新每只股票的 sector_dir + sector_name（东财行业 > 名称关键词）
        for r in results:
            code = r["code"]
            ind  = code_industry.get(code, "")
            etf  = get_sector_etf_by_industry(ind) or get_sector_etf(r["name"])
            if etf:
                r["sector_dir"] = etf_dir_cache.get(etf, "unknown")
            # 无论是否有对应 ETF，只要东财返回了行业名就存下来
            if ind:
                r["sector_name"] = ind

    # ── 推送 + 存库 ─────────────────────────────────

    def notify_and_save(self, results: list[dict]):
        """推送企业微信 + 写入 scan_results 表（全部存库，只推送 Top N）"""
        from trader.paper import paper
        scan_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")   # 秒级时间戳，方便前端检测完成
        paper.save_scan_results(scan_date, results)   # 全量存库

        # 微信推送只取 Top N（避免消息过长）
        notify_top = results[:SCAN_MAX_RESULTS]

        SIGNAL_ICONS = {"金叉买点": "✅", "回踩买点": "🔄", "粘合发散买点": "🔀"}

        if notify_top:
            blocks = []
            for i, r in enumerate(notify_top, 1):
                icon         = SIGNAL_ICONS.get(r["signal"], "⭕")
                signal_short = r["signal"].replace("买点", "")
                rs_tag       = (
                    f"  📊RS={r['rs_score']:+.1f}%"
                    if r.get("rs_score") is not None else ""
                )
                blocks.append(
                    f"**{i}. {r['name']}（{r['code']}）{r['price']:.2f}元**"
                    f" {icon}{signal_short}{rs_tag}\n"
                    f"{r['reason']}"
                )
            body = "\n\n".join(blocks)
        else:
            body = "_今日暂无符合520买点的候选股_"

        total_cnt  = len(results)
        notify_cnt = len(notify_top)
        title = (
            f"🔍 每日扫描 {_date()} | 共{total_cnt}只"
            + (f"，推送Top{notify_cnt}" if total_cnt > notify_cnt else "")
        )
        _push(title, body, level="INFO")
        log(f"扫描推送完成，共 {total_cnt} 只候选（推送 {notify_cnt} 只）", "INFO")

    def run(self) -> list[dict]:
        """一键扫描 + 推送，返回结果"""
        results = self.scan()
        self.notify_and_save(results)
        return results


# 全局单例
scanner = MarketScanner()

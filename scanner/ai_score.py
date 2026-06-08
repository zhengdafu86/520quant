"""
AI 评分写入工具（手动按需阶段）
================================================
工作流：
  1. 收盘扫描后，导出当日扫描结果（含技术评分/涨跌幅/板块/信号）：
       python -m scanner.ai_score --export > today.json
  2. 把 today.json 交给 AI（Claude），结合 a-stock-data 的资金面/北向/龙虎榜
     + 利好利空新闻，给每只 0-100 的 AI 评分 + 一句话理由，生成 scores.json：
       {"600036": {"ai_score": 85, "ai_comment": "北向连买3日+龙虎榜机构净买,题材正面"}, ...}
  3. 写回扫描结果（仅展示，不碰交易）：
       python -m scanner.ai_score --apply scores.json [--date YYYY-MM-DD]

注：AI 评分无法回测、消息面有噪音 → 仅作人工决策参考。
"""
from __future__ import annotations

import sys
import re
import json
import argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trader.paper import PaperAccount

import time
import random

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# ── 东财统一节流入口（防封）──────────────────────────────
# 所有东财端点（datacenter/push2his/push2/search/np-weblist/reportapi）统一走 em_get：
#   · 会话复用（keep-alive）  · 串行限流（每次间隔≥1s + 随机抖动）  · 失败退避重试
# 批量抓取直接调用即自带防封，无需各处再手写 sleep。
_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": _UA})
_EM_LAST = [0.0]
_EM_MIN_INTERVAL = 1.0   # 最小请求间隔（秒）


def em_get(url, params=None, referer="https://quote.eastmoney.com/",
           timeout=15, tries=3):
    """节流 + 重试的东财 GET，返回 requests.Response 或 None。"""
    for i in range(tries):
        gap = _EM_MIN_INTERVAL + random.uniform(0.2, 0.9) - (time.time() - _EM_LAST[0])
        if gap > 0:
            time.sleep(gap)
        try:
            r = _EM_SESSION.get(url, params=params,
                                headers={"Referer": referer}, timeout=timeout)
            _EM_LAST[0] = time.time()
            if r.status_code == 200:
                return r
        except Exception:
            _EM_LAST[0] = time.time()
        time.sleep(1.5 * (i + 1) + random.uniform(0, 1.0))   # 退避
    return None


def _dc(report, filt="", ps=50, sc="", st="-1"):
    p = {"reportName": report, "columns": "ALL", "filter": filt, "pageNumber": "1",
         "pageSize": str(ps), "sortColumns": sc, "sortTypes": st, "source": "WEB", "client": "WEB"}
    r = em_get(_DC, p, referer="https://data.eastmoney.com/")
    if not r:
        return []
    try:
        d = r.json()
        return d.get("result", {}).get("data", []) if d.get("result") else []
    except Exception:
        return []


def _fund_net(code):
    """近5日 / 近20日 主力净流入（万元）"""
    mc = 1 if code.startswith(("6", "9", "5")) else 0
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    p = {"secid": f"{mc}.{code}", "fields1": "f1,f2,f3,f7",
         "fields2": "f51,f52,f53,f54,f55,f56,f57", "lmt": "120"}
    r = em_get(url, p)
    if not r:
        return None
    try:
        kl = r.json().get("data", {}).get("klines", [])
        vals = [float(ln.split(",")[1]) for ln in kl if ln.split(",")[1] != "-"]
        if not vals:
            return None
        return (round(sum(vals[-5:]) / 1e4), round(sum(vals[-20:]) / 1e4))
    except Exception:
        return None


def _lhb(code, ref_date):
    start = (datetime.strptime(ref_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    data = _dc("RPT_DAILYBILLBOARD_DETAILSNEW",
               f"(TRADE_DATE>='{start}')(TRADE_DATE<='{ref_date}')(SECURITY_CODE=\"{code}\")",
               50, "TRADE_DATE", "-1")
    return [{"date": str(r.get("TRADE_DATE", ""))[:10], "reason": r.get("EXPLANATION", ""),
             "net_wan": round((r.get("BILLBOARD_NET_AMT") or 0) / 1e4)} for r in data]


def _news(name, n=5):
    inner = json.dumps({"uid": "", "keyword": name, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
        "pageIndex": 1, "pageSize": n, "preTag": "", "postTag": ""}}}, separators=(',', ':'))
    r = em_get("https://search-api-web.eastmoney.com/search/jsonp",
               {"cb": "jq", "param": inner}, referer="https://so.eastmoney.com/")
    if not r:
        return []
    try:
        t = r.text
        d = json.loads(t[t.index("(") + 1:t.rindex(")")])
        arts = d.get("result", {}).get("cmsArticleWebOld", [])   # 直接是 list
        return [{"date": a.get("date", "")[:10],
                 "title": re.sub(r"<[^>]+>", "", a.get("title", ""))} for a in arts[:n]]
    except Exception:
        return []


def _excluded(code):
    return code.startswith(("300", "301", "688", "689"))


_SINA_FUND = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "MoneyFlow.ssl_qsfx_zjlrqs")


def _fund_one_sina(code: str):
    """新浪个股资金流（日级历史）→ (今日万, 5日万, 10日万)。失败返回 None。
    口径：r0_net=主力(超大单)净额，单位元 → 万元；按最近N日累加。
    （东财 push2 对云服务器IP封禁，改用新浪——不同服务商、服务器可达。）"""
    daima = ("sh" if code.startswith(("6", "9", "5")) else "sz") + code
    p = {"page": "1", "num": "20", "sort": "opendate", "asc": "0", "daima": daima}
    r = em_get(_SINA_FUND, p, referer="https://finance.sina.com.cn/")
    if not r:
        return None
    try:
        rows = r.json()
        if not rows:
            return None
        net = [float(x.get("r0_net") or 0) for x in rows]   # 最近在前(asc=0)
        today = net[0]
        d5  = sum(net[:5])
        d10 = sum(net[:10])
        return (round(today / 1e4), round(d5 / 1e4), round(d10 / 1e4))
    except Exception:
        return None


def _fund_batch(codes) -> dict:
    """逐只取主力净额（新浪源，em_get 节流），返回 {code:(今日万,5日万,10日万)}，缺失None。"""
    return {c: _fund_one_sina(c) for c in codes}


def gather(date: str = None, top: int = 12) -> dict:
    """为当日扫描的 Top-N 主板候选，汇集资金面/龙虎榜/新闻，供 AI 打分。"""
    pa = PaperAccount()
    data = pa.get_scan_results(date)
    d = data.get("date", "")
    ref = d[:10] or datetime.now().strftime("%Y-%m-%d")
    cands = [r for r in data.get("results", []) if not _excluded(r["code"])][:top]

    # 资金流优先读库存（监控扫描后已采集）；缺失才现场批量补抓一次
    stored = {r["code"]: (r.get("main_net_today"), r.get("main_net_5d"), r.get("main_net_10d"))
              for r in cands}
    missing = [c for c, v in stored.items() if v[1] is None]
    fund_live = _fund_batch(missing) if missing else {}

    # 当日热门题材：① 个股代码精确命中(回踩股少见) ② 整体热点榜作上下文，
    # 供 AI 结合个股行业/新闻判断"是否踩中热点"（主力机制）
    try:
        from scanner.hot_sectors import stock_hot_themes, get_hot
        hot_map = stock_hot_themes([r["code"] for r in cands], ref)
        hot_today = [{"theme": t["theme"], "stock_count": t["stock_count"]}
                     for t in get_hot(ref, top=20)["themes"]]
    except Exception:
        hot_map, hot_today = {}, []

    out = []
    for r in cands:
        c = r["code"]
        sv = stored[c]
        fb = sv if sv[1] is not None else fund_live.get(c)
        lhb = _lhb(c, ref)
        news = _news(r["name"])
        out.append({
            "code": c, "name": r["name"], "signal": r["signal"],
            "score": r["score"], "change_pct": r["change_pct"], "sector": r["sector_name"],
            "main_net_today_wan": fb[0] if fb else None,
            "main_net_5d_wan": fb[1] if fb else None,
            "main_net_10d_wan": fb[2] if fb else None,
            "hot_themes": hot_map.get(c, []),
            "lhb_30d": lhb, "news": news,
        })
    return {"date": d, "hot_themes_today": hot_today, "stocks": out}


def collect_fund_to_db(date: str = None, top: int = 0) -> int:
    """监控每日扫描后调用：批量采集当日候选主力净额(clist一波)写入 scan_results。
    top=0 表示全部候选。返回写入条数。"""
    pa = PaperAccount()
    data = pa.get_scan_results(date)
    d = data.get("date", "")
    cands = [r for r in data.get("results", []) if not _excluded(r["code"])]
    if top and top > 0:
        cands = cands[:top]
    codes = [r["code"] for r in cands]
    if not codes:
        return 0
    fund = {c: v for c, v in _fund_batch(codes).items() if v}
    return pa.update_fund_flow(fund, d) if fund else 0


def export_scan(date: str = None) -> dict:
    pa = PaperAccount()
    data = pa.get_scan_results(date)
    slim = [{"code": r["code"], "name": r["name"], "signal": r["signal"],
             "price": r["price"], "change_pct": r["change_pct"],
             "score": r["score"], "sector_name": r["sector_name"],
             "rs_score": r["rs_score"]}
            for r in data.get("results", [])]
    return {"date": data.get("date", ""), "results": slim}


def apply_scores(path: str, date: str = None) -> int:
    scores = json.loads(Path(path).read_text(encoding="utf-8"))
    pa = PaperAccount()
    n = pa.update_ai_scores(scores, date)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="导出当日扫描结果(JSON)")
    ap.add_argument("--gather", action="store_true", help="汇集Top-N候选的资金面/龙虎榜/新闻(JSON)")
    ap.add_argument("--top", type=int, default=12, help="--gather 取前N只(默认12)")
    ap.add_argument("--apply", metavar="JSON", help="写回 AI 评分(JSON 文件)")
    ap.add_argument("--collect", action="store_true", help="采集当日候选主力净额写入扫描结果")
    ap.add_argument("--date", default=None, help="指定扫描日期，默认最新")
    a = ap.parse_args()

    if a.collect:
        n = collect_fund_to_db(a.date, a.top if a.top != 12 else 0)
        print(f"✅ 资金流已采集入库 {n} 条")
    elif a.gather:
        print(json.dumps(gather(a.date, a.top), ensure_ascii=False, indent=1))
    elif a.export:
        print(json.dumps(export_scan(a.date), ensure_ascii=False, indent=2))
    elif a.apply:
        n = apply_scores(a.apply, a.date)
        print(f"✅ 已写回 AI 评分 {n} 条")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

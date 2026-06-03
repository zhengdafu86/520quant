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

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _dc(report, filt="", ps=50, sc="", st="-1"):
    p = {"reportName": report, "columns": "ALL", "filter": filt, "pageNumber": "1",
         "pageSize": str(ps), "sortColumns": sc, "sortTypes": st, "source": "WEB", "client": "WEB"}
    try:
        r = requests.get(_DC, params=p, headers={"User-Agent": _UA}, timeout=15).json()
        return r.get("result", {}).get("data", []) if r.get("result") else []
    except Exception:
        return []


def _fund_net(code):
    """近5日 / 近20日 主力净流入（万元）"""
    mc = 1 if code.startswith("6") else 0
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    p = {"secid": f"{mc}.{code}", "fields1": "f1,f2,f3,f7",
         "fields2": "f51,f52,f53,f54,f55,f56,f57", "lmt": "120"}
    try:
        d = requests.get(url, params=p, headers={"User-Agent": _UA,
            "Referer": "https://quote.eastmoney.com/"}, timeout=15).json()
        vals = [float(ln.split(",")[1]) for ln in d.get("data", {}).get("klines", [])
                if ln.split(",")[1] != "-"]
        return (round(sum(vals[-5:]) / 1e4), round(sum(vals[-20:]) / 1e4))
    except Exception:
        return (0, 0)


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
    try:
        t = requests.get("https://search-api-web.eastmoney.com/search/jsonp",
            params={"cb": "jq", "param": inner},
            headers={"User-Agent": _UA, "Referer": "https://so.eastmoney.com/"}, timeout=12).text
        d = json.loads(t[t.index("(") + 1:t.rindex(")")])
        arts = d.get("result", {}).get("cmsArticleWebOld", [])   # 直接是 list
        return [{"date": a.get("date", "")[:10],
                 "title": re.sub(r"<[^>]+>", "", a.get("title", ""))} for a in arts[:n]]
    except Exception:
        return []


def _excluded(code):
    return code.startswith(("300", "301", "688", "689"))


def gather(date: str = None, top: int = 12) -> dict:
    """为当日扫描的 Top-N 主板候选，汇集资金面/龙虎榜/新闻，供 AI 打分。"""
    pa = PaperAccount()
    data = pa.get_scan_results(date)
    d = data.get("date", "")
    ref = d[:10] or datetime.now().strftime("%Y-%m-%d")
    cands = [r for r in data.get("results", []) if not _excluded(r["code"])][:top]
    out = []
    for r in cands:
        n5, n20 = _fund_net(r["code"])
        lhb = _lhb(r["code"], ref)
        news = _news(r["name"])
        out.append({
            "code": r["code"], "name": r["name"], "signal": r["signal"],
            "score": r["score"], "change_pct": r["change_pct"], "sector": r["sector_name"],
            "main_net_5d_wan": n5, "main_net_20d_wan": n20,
            "lhb_30d": lhb, "news": news,
        })
    return {"date": d, "stocks": out}


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
    ap.add_argument("--date", default=None, help="指定扫描日期，默认最新")
    a = ap.parse_args()

    if a.gather:
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

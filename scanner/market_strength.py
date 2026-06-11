"""
市场强弱仪表盘 — 给"人来判弱市"提供据可依的日度读数(非机械交易信号)。
================================================
指标(均为可靠数据源):
  ① 沪深300指数: 现价 vs MA20/MA60(金叉?) + MA20斜率方向 + 近10日涨跌 + 量能
  ② 全市场涨跌家数(东财 ulist.np, 沪+深) → 涨跌比(最实用的宽度温度计)
  ③ 策略近10笔实盘胜率(由调用方传入, 内生信号)
综合 → 强 / 中性 / 弱。
"""
from __future__ import annotations

import requests

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117"


def _index_state() -> dict:
    """沪深300(510300)趋势+量能。"""
    from data.fetcher import db
    mk = db.get_market(bars=60)
    if mk is None or mk.empty:
        return {}
    last = mk.iloc[-1]
    close = float(last["close"]); ma20 = float(last["ma20"]); ma60 = float(last["ma60"])
    slope = float(last.get("ma20_slope", 0))
    c = mk["close"].astype(float)
    ma5 = float(c.tail(5).mean()); ma10 = float(c.tail(10).mean())
    ret10 = (float(c.iloc[-1]) / float(c.iloc[-11]) - 1) * 100 if len(c) > 11 else 0.0
    vr = float(last.get("vol_ratio", 1.0) or 1.0)
    up_day = float(c.iloc[-1]) > float(c.iloc[-2]) if len(c) > 1 else False
    return {"close": round(close, 3), "ma5": round(ma5, 3), "ma10": round(ma10, 3),
            "ma20": round(ma20, 3), "ma60": round(ma60, 3),
            "cross": ma20 > ma60, "slope": round(slope, 4),
            "slope_dir": "上行" if slope > 0.001 else ("下行" if slope < -0.001 else "走平"),
            "ret10": round(ret10, 1), "vol_ratio": round(vr, 2),
            "above_ma5": close > ma5, "above_ma10": close > ma10, "above_ma20": close > ma20,
            "up_day": bool(up_day)}


def _breadth() -> dict:
    """全市场涨跌家数(沪+深) — 东财 ulist.np(f104涨/f105跌/f106平)。"""
    try:
        u = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        p = {"fltt": "2", "fields": "f104,f105,f106", "secids": "1.000001,0.399001"}
        r = requests.get(u, params=p, headers={"User-Agent": _UA}, timeout=10).json()
        diff = (r.get("data") or {}).get("diff") or []
        up = sum(int(x.get("f104", 0) or 0) for x in diff)
        down = sum(int(x.get("f105", 0) or 0) for x in diff)
        flat = sum(int(x.get("f106", 0) or 0) for x in diff)
        ratio = round(up / down, 2) if down else 9.99
        return {"up": up, "down": down, "flat": flat, "ratio": ratio}
    except Exception as e:
        return {"err": str(e)[:40]}


def _margin() -> dict:
    """全市场融资余额近5日变化(东财 RPTA_RZRQ_LSHJ) — 杠杆资金风向。"""
    try:
        u = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        p = {"reportName": "RPTA_RZRQ_LSHJ", "columns": "DIM_DATE,RZYE,RZRQYE",
             "sortColumns": "DIM_DATE", "sortTypes": "-1", "pageSize": "6",
             "source": "WEB", "client": "WEB"}
        r = requests.get(u, params=p, headers={"User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=10).json()
        d = (r.get("result") or {}).get("data") or []
        if len(d) >= 6:
            now = float(d[0]["RZYE"]); ago = float(d[5]["RZYE"])
            chg5 = (now / ago - 1) * 100 if ago else 0.0
            return {"date": d[0]["DIM_DATE"][:10], "rzye_yi": round(now / 1e4, 0),
                    "chg5": round(chg5, 2)}
    except Exception as e:
        return {"err": str(e)[:40]}
    return {}


def _v_progress(idx: dict, br: dict) -> dict:
    """V反弹确认进度条 — 机器可算的5项右侧确认信号。"""
    ratio = br.get("ratio", 0) if "err" not in br else 0
    checks = [
        ("涨跌家数转正", bool(ratio >= 1.0)),
        ("放量上涨", bool(idx.get("up_day") and idx.get("vol_ratio", 0) >= 1.0)),
        ("站上MA5", bool(idx.get("above_ma5"))),
        ("站上MA10", bool(idx.get("above_ma10"))),
        ("站上MA20", bool(idx.get("above_ma20"))),
    ]
    n = sum(1 for _, ok in checks if ok)
    stage = "确认(可恢复进攻)" if n >= 4 else ("试探(小仓/最强龙头)" if n >= 2 else "观察(未启动)")
    return {"n": n, "total": len(checks), "stage": stage,
            "checks": [{"k": k, "ok": ok} for k, ok in checks]}


def compute(recent_winrate: float = None, recent_n: int = 0) -> dict:
    """recent_winrate: 调用方传入的策略近N笔胜率(%); None=不计入。"""
    idx = _index_state()
    br = _breadth()
    mg = _margin()
    score = 0
    notes = []
    # 指数趋势
    if idx:
        if idx["cross"]:
            score += 1
        else:
            score -= 1; notes.append("指数跌破MA60(空头)")
        if idx["slope_dir"] == "上行":
            score += 1
        elif idx["slope_dir"] == "下行":
            score -= 1; notes.append("MA20向下")
        if idx["ret10"] < -2:
            notes.append(f"近10日{idx['ret10']:+.1f}%")
    # 宽度
    if br.get("ratio") is not None and "err" not in br:
        rt = br["ratio"]
        if rt >= 1.5:
            score += 2
        elif rt >= 1.0:
            score += 1
        elif rt < 0.5:
            score -= 2; notes.append(f"普跌(涨跌{br['up']}:{br['down']})")
        elif rt < 1.0:
            score -= 1; notes.append(f"跌多于涨({br['up']}:{br['down']})")
    # 两融余额(杠杆资金风向)
    if mg.get("chg5") is not None and "err" not in mg:
        c5 = mg["chg5"]
        if c5 >= 1.0:
            score += 1
        elif c5 <= -1.5:
            score -= 1; notes.append(f"融资余额近5日{c5:+.1f}%(杠杆撤离)")
    # 策略反馈
    if recent_winrate is not None and recent_n > 0:
        if recent_winrate >= 50:
            score += 1
        elif recent_winrate < 35:
            score -= 1; notes.append(f"策略近{recent_n}笔胜率{recent_winrate:.0f}%偏低")
    verdict = "强" if score >= 2 else ("弱" if score <= -2 else "中性")
    advice = {"强": "顺势满仓、积极进攻", "中性": "正常操作、按信号来",
              "弱": "降低仓位/持币观望、只打最强信号"}[verdict]
    sug_cap = {"强": 4, "中性": 4, "弱": 1}[verdict]   # 建议持仓数上限(弱市留1,极弱可手动设0)
    return {"index": idx, "breadth": br, "margin": mg, "score": score, "verdict": verdict,
            "advice": advice, "sug_cap": sug_cap, "notes": notes, "vreb": _v_progress(idx, br),
            "winrate": recent_winrate, "winrate_n": recent_n}


if __name__ == "__main__":
    import json
    print(json.dumps(compute(), ensure_ascii=False, indent=2))

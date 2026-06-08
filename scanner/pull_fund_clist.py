"""
全市场资金流拉取（push2 clist）→ 映射当日候选
================================================
绕开被限流的 push2his 逐只接口：clist 一次返回全市场（~5500只），
分页几十次即覆盖，含 今日(f62)/5日(f164)/10日(f174) 主力净额。
配合 em_get 节流 + 退避，对 IP 更友好。

用法:
  python -m scanner.pull_fund_clist [sig_json] [out_json]
默认 sig_json=/tmp/sig_today.json  out_json=/tmp/fund2.json
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.ai_score import em_get

URL = "https://push2.eastmoney.com/api/qt/clist/get"
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"   # 沪深主板/中小/创业(主板范围)
FIELDS = "f12,f14,f62,f164,f174"            # 代码,名,今日,5日,10日 主力净额


def _scan(want_codes: set, page_size: int):
    found, pn, total, fails, got_any = {}, 1, None, 0, False
    while True:
        p = {"fid": "f62", "po": "1", "pz": str(page_size), "pn": str(pn), "np": "1",
             "fltt": "2", "invt": "2", "fs": FS, "fields": FIELDS}
        r = em_get(URL, p, referer="https://data.eastmoney.com/")
        if not r:
            fails += 1
            if fails >= 5:
                break
            continue
        fails = 0
        d = r.json().get("data") or {}
        diff = d.get("diff") or []
        total = d.get("total", total)
        if not diff:
            break
        got_any = True
        for it in diff:
            c = it.get("f12")
            if c in want_codes:
                found[c] = (it.get("f62"), it.get("f164"), it.get("f174"))
        if len(found) >= len(want_codes) or pn * page_size >= (total or 0):
            break
        pn += 1
    return found, got_any


def pull(want_codes: set, pz: int = 1000) -> dict:
    """返回 {code: (今日万, 5日万, 10日万)}，缺失为 None。
    大页 pz=1000 全市场约 6 页覆盖；被拒则回退 pz=200。"""
    found, got_any = _scan(want_codes, pz)
    if not got_any and pz > 200:
        found, _ = _scan(want_codes, 200)
    res = {}
    for c in want_codes:
        f = found.get(c)
        if f and f[1] is not None:
            res[c] = (round(f[0] / 1e4), round(f[1] / 1e4), round(f[2] / 1e4))
        else:
            res[c] = None
    return res


def main():
    sig_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sig_today.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/fund2.json"
    sig = json.load(open(sig_path))
    want = {s["code"]: s for s in sig["stocks"]}
    res = pull(set(want))
    ok = sum(1 for v in res.values() if v)
    for c, s in want.items():
        v = res.get(c)
        if v:
            print(f"  {s['name']}({c}) [{s.get('sector','')}] 今{v[0]:+.0f}/5日{v[1]:+.0f}/10日{v[2]:+.0f}万")
        else:
            print(f"  {s['name']}({c}) 缺")
    json.dump(res, open(out_path, "w"))
    print(f"\n命中 {ok}/{len(want)}  已写 {out_path}")


if __name__ == "__main__":
    main()

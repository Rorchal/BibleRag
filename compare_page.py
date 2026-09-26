# -*- coding: utf-8 -*-
"""生成「切段结果 vs gold」对照页面，多个版本可在页面上切换。

对齐口径：以 gold 与该版本共有的边界为分组点，一组里 gold 与模型各一段且起止行相同 = 一致；
其余按差异类型标出（±3 行内错位 / 模型多切 / 模型少切 / 边界交错）。
每段模型标题附上其他版本在同一位置的标题，便于看标题的变化。

用法：
  python compare_page.py --run v1=output/paras_v1_p1 --run v2=output/paras_v2_p1 \
      --out output/切段对照_v1_v2.html
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import os
import re

import goldeval as g
import seg_paras as sp

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "templates", "切段对照.html")


def flags(title: str) -> dict:
    return {"ban": sorted(set(sp.BANNED.findall(title))),
            "per": sp.person_hits(title),
            "dem": sorted(set(sp.DEMONS.findall(title)))}


def build_run(G: list[dict], run_dir: str, n_lines: int) -> dict:
    pj = json.load(io.open(os.path.join(run_dir, "parsed.json"), encoding="utf-8"))
    P = []
    for s in pj["sections"]:
        for p in s["paragraphs"]:
            t = p.get("title") or ""
            P.append(dict(s=p["start"], e=p["end"], t=t, en=p.get("ending", ""),
                          sec=s["no"], f=flags(t)))
    unc = {str(u["line"]): u["reason"] for s in pj["sections"] for u in s.get("uncertain", [])}

    gb = {p["s"] for p in G} - {1}
    pb = {p["s"] for p in P} - {1}
    exact = gb & pb
    near, used = {}, set()
    for b in sorted(pb - exact):
        cand = [x for x in gb - exact if abs(x - b) <= 3 and x not in used]
        if cand:
            x = min(cand, key=lambda x: abs(x - b))
            used.add(x)
            near[b] = x

    cuts = sorted(exact | {1})
    groups = []
    for i, a in enumerate(cuts):
        b = cuts[i + 1] - 1 if i + 1 < len(cuts) else n_lines
        gg = [p for p in G if a <= p["s"] <= b]
        pp = [p for p in P if a <= p["s"] <= b]
        ig = {p["s"] for p in gg} - {a}
        ip = {p["s"] for p in pp} - {a}
        if len(gg) == 1 and len(pp) == 1:
            st = "same"
        elif len(gg) == len(pp) and all(x in near for x in ip) and {near[x] for x in ip} == ig:
            st = "near"
        elif len(gg) == 1:
            st = "over"
        elif len(pp) == 1:
            st = "under"
        else:
            st = "mixed"
        groups.append(dict(s=a, e=b, st=st, g=[x["i"] for x in gg], p=pp))

    m0, m3 = g.prf(pb, gb, 0), g.prf(pb, gb, 3)
    meta = pj.get("meta", {})
    usage = [c.get("usage") or {} for c in meta.get("calls", [])]
    stats = dict(
        pred_n=len(P), gb=len(gb), pb=len(pb), exact=len(exact), near=len(near),
        f0=[m0["P"], m0["R"], m0["F1"]], f3=[m3["P"], m3["R"], m3["F1"]],
        wd=g.window_diff(gb, pb, n_lines),
        groups=dict(collections.Counter(x["st"] for x in groups)),
        ban=sum(bool(p["f"]["ban"]) for p in P), per=sum(bool(p["f"]["per"]) for p in P),
        dem=sum(bool(p["f"]["dem"]) for p in P),
        long=sum(len(p["t"]) > 110 for p in P),
        tok_in=sum(u.get("prompt_tokens", 0) for u in usage),
        tok_out=sum(u.get("completion_tokens", 0) for u in usage),
        prompt=meta.get("prompt", ""), model=meta.get("model", ""), effort=meta.get("effort", ""))
    return dict(stats=stats, groups=groups, near={str(k): v for k, v in near.items()},
                unc=unc, paras=[{k: p[k] for k in ("s", "e", "t")} for p in P])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="output/gold_v3.md")
    ap.add_argument("--txt", default=sp.TXT)
    ap.add_argument("--run", action="append", required=True, help="标签=结果目录")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    lines = sp.read_lines(a.txt)
    gd = g.parse_gold(a.gold)
    G = [dict(i=i, s=p["start"], e=p["end"], t=p["title"], sec=p["section"], f=flags(p["title"]))
         for i, p in enumerate(gd["paragraphs"])]
    runs = {}
    for spec in a.run:
        label, _, path = spec.partition("=")
        runs[label] = build_run(G, path, len(lines))
    data = dict(
        gold_name=os.path.basename(a.gold), gold_n=len(G), gold=G, lines=lines,
        chapters=[dict(no=c["no"], t=c["title"], s=c["start"], e=c["end"]) for c in gd["chapters"]],
        runs=runs, order=list(runs))
    html = io.open(TEMPLATE, encoding="utf-8").read()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    io.open(a.out, "w", encoding="utf-8").write(html.replace("__DATA__", payload))
    for k, r in runs.items():
        s = r["stats"]
        print(f"{k}: {s['pred_n']} 段  F1(0) {s['f0'][2]}  F1(±3) {s['f3'][2]}  "
              f"禁用词 {s['ban']}  人称 {s['per']}  指示词 {s['dem']}")
    print(f"页面：{a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""以豆包产出的章/节/段结构为 gold，评测各版提示词的切分结果。

边界口径：一个「边界」= 某一块的起始行号（不含全文第一行，因为它恒定存在）。
指标：precision / recall / F1，分别在容差 0 行和 ±3 行下计算。
  容差 ±3 的理由：讲道口语里过渡句归属前后块都说得通，差 1~3 行不算真错。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

import ds_client as ds

DASH = r"[–\-—~〜]"


LP = r"[（(]"          # 全角/半角左括号都要认
RP = r"[）)]"


def parse_gold(path: str) -> dict:
    """解析豆包的 markdown 结构文件。

    两版 gold 格式不同，都要兼容：
      旧版 `## 第1章 标题（行1–25）` + `### 1.1 标题（行1–4）` 节带范围
      新版 `## 第1章 标题(行1–3)`   + `### 1.1 标题`          节不带范围
    节缺范围时，从它下辖的段反推。
    """
    text = io.open(path, encoding="utf-8-sig").read()
    chapters, sections, paragraphs = [], [], []
    rng = LP + r"行(\d+)" + DASH + r"(\d+)" + RP
    re_ch = re.compile(r"^##\s*第(\d+)章\s*(.*?)\s*(?:" + rng + r")?\s*$")
    re_se = re.compile(r"^###\s*([\d.]+)\s*(.*?)\s*(?:" + rng + r")?\s*$")
    re_pa = re.compile(r"^-\s*\*\*行(\d+)(?:" + DASH + r"(\d+))?\*\*[　\s]*(.*)$")
    cur_ch = cur_se = None
    for ln in text.splitlines():
        ln = ln.strip()
        m = re_ch.match(ln)
        if m:
            cur_ch = {"no": int(m.group(1)), "title": m.group(2).strip(),
                      "start": int(m.group(3)) if m.group(3) else None,
                      "end": int(m.group(4)) if m.group(4) else None}
            chapters.append(cur_ch)
            cur_se = None
            continue
        m = re_se.match(ln)
        if m:
            cur_se = {"no": m.group(1), "title": m.group(2).strip(),
                      "start": int(m.group(3)) if m.group(3) else None,
                      "end": int(m.group(4)) if m.group(4) else None,
                      "chapter": cur_ch["no"] if cur_ch else None}
            sections.append(cur_se)
            continue
        m = re_pa.match(ln)
        if m:
            s = int(m.group(1))
            e = int(m.group(2)) if m.group(2) else s
            paragraphs.append({"start": s, "end": e, "title": m.group(3).strip(),
                               "section": cur_se["no"] if cur_se else None,
                               "chapter": cur_ch["no"] if cur_ch else None})
            for blk in (cur_se, cur_ch):
                if blk is None:
                    continue
                blk["start"] = s if blk["start"] is None else min(blk["start"], s)
                blk["end"] = e if blk["end"] is None else max(blk["end"], e)
            continue
    return {"chapters": chapters, "sections": sections, "paragraphs": paragraphs}


def bounds(items: list[dict], lo: int = 1) -> set[int]:
    """块的起始行号集合，去掉恒定的全文首行。"""
    return {x["start"] for x in items} - {lo}


def prf(pred: set[int], gold: set[int], tol: int = 0) -> dict:
    """容差 tol 下的 precision/recall/F1。"""
    if tol == 0:
        tp = len(pred & gold)
        matched_gold = len(pred & gold)
    else:
        used, tp = set(), 0
        for p in sorted(pred):
            cand = [g for g in gold if abs(g - p) <= tol and g not in used]
            if cand:
                g = min(cand, key=lambda x: abs(x - p))
                used.add(g)
                tp += 1
        matched_gold = len(used)
    P = tp / len(pred) if pred else 0.0
    R = matched_gold / len(gold) if gold else 0.0
    F = 2 * P * R / (P + R) if (P + R) else 0.0
    return {"tp": tp, "pred": len(pred), "gold": len(gold),
            "P": round(P, 3), "R": round(R, 3), "F1": round(F, 3)}


def _win_k(n_lines: int, n_ref_segments: int) -> int:
    """Beeferman 的惯例：k = 参考切分平均段长的一半。"""
    return max(2, int(round(n_lines / (2 * max(1, n_ref_segments)))))


def pk(ref: set[int], hyp: set[int], n: int, k: int | None = None) -> float:
    """Pk：窗口两端「是否同段」判断不一致的比例。越低越好，0 为完美。

    只看窗口内「有没有边界」，所以对边界数量差不敏感，但对错位敏感。
    """
    if k is None:
        k = _win_k(n, len(ref) + 1)
    r, h = sorted(ref), sorted(hyp)
    err = tot = 0
    for i in range(1, n - k + 1):
        a = any(i < b <= i + k for b in r)
        c = any(i < b <= i + k for b in h)
        err += (a != c)
        tot += 1
    return round(err / tot, 4) if tot else 0.0


def window_diff(ref: set[int], hyp: set[int], n: int, k: int | None = None) -> float:
    """WindowDiff：窗口内边界「个数」不等的比例。越低越好，0 为完美。

    比 Pk 严格 —— 窗口里参考有 2 个边界而假设只有 1 个，Pk 判它通过，WindowDiff 判它失败。
    因此它同时惩罚漏切和过切，是这两个指标里更该看的那个。
    """
    if k is None:
        k = _win_k(n, len(ref) + 1)
    r, h = sorted(ref), sorted(hyp)
    err = tot = 0
    for i in range(1, n - k + 1):
        a = sum(1 for b in r if i < b <= i + k)
        c = sum(1 for b in h if i < b <= i + k)
        err += (a != c)
        tot += 1
    return round(err / tot, 4) if tot else 0.0


def load_pred(path: str) -> dict:
    """支持 v3 的 parsed.json，以及 v1/v2 的 raw.json（只有段）。"""
    raw = io.open(path, encoding="utf-8").read()
    if path.endswith(".parsed.json"):
        d = json.loads(raw)
        return {"chapters": d.get("chapters", []), "sections": d.get("sections", []),
                "paragraphs": d.get("paragraphs", [])}
    data = ds.parse_json(raw)
    if data is None:
        return {"chapters": [], "sections": [], "paragraphs": []}
    paras = ds.flatten(data)
    # v2 的章/节没有显式行号，用其下辖首段的 start 反推
    chs, ses, seen_c, seen_s = [], [], {}, {}
    for p in paras:
        if p["chapter"] not in seen_c:
            seen_c[p["chapter"]] = {"no": p["chapter"], "title": p["chapter_title"],
                                    "start": p["start"], "end": p["end"]}
        seen_c[p["chapter"]]["end"] = max(seen_c[p["chapter"]]["end"], p["end"])
        k = (p["chapter"], p["section"])
        if k not in seen_s:
            seen_s[k] = {"no": f"{p['chapter']}.{p['section']}",
                         "title": p["section_title"], "start": p["start"], "end": p["end"]}
        seen_s[k]["end"] = max(seen_s[k]["end"], p["end"])
    chs = list(seen_c.values())
    ses = list(seen_s.values())
    return {"chapters": chs, "sections": ses, "paragraphs": paras}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--pred", nargs="+", required=True,
                    help="形如 标签=路径")
    ap.add_argument("--txt", default=None, help="源 txt，用于打印边界附近的原文")
    a = ap.parse_args()

    gold = parse_gold(a.gold)
    print(f"Gold：{os.path.basename(a.gold)}")
    print(f"  章 {len(gold['chapters'])}  节 {len(gold['sections'])}  "
          f"段 {len(gold['paragraphs'])}")
    gb = {k: bounds(gold[k]) for k in ("chapters", "sections", "paragraphs")}

    preds = {}
    for spec in a.pred:
        label, _, path = spec.partition("=")
        preds[label] = load_pred(path)

    name = {"chapters": "章", "sections": "节", "paragraphs": "段"}
    nlines = None
    if a.txt:
        nlines = len(ds.read_lines(a.txt))
    else:
        nlines = max([x["end"] for lv in gold.values() for x in lv] or [969])

    for level in ("chapters", "sections", "paragraphs"):
        k = _win_k(nlines, len(gold[level]))
        print(f"\n{'=' * 92}")
        print(f"【{name[level]}】gold {len(gold[level])} 块 / {len(gb[level])} 个边界"
              f"   共 {nlines} 行   窗口 k={k}")
        print(f"{'方案':<18}{'块数':>5}{'边界':>5}"
              f"{'F1(0)':>8}{'F1(±3)':>9}{'P(±3)':>8}{'R(±3)':>8}"
              f"{'Pk':>8}{'WinDiff':>9}")
        print("-" * 92)
        for label, pr in preds.items():
            pb = bounds(pr[level])
            m0 = prf(pb, gb[level], 0)
            m3 = prf(pb, gb[level], 3)
            print(f"{label:<18}{len(pr[level]):>5}{len(pb):>5}"
                  f"{m0['F1']:>8.3f}{m3['F1']:>9.3f}{m3['P']:>8.3f}{m3['R']:>8.3f}"
                  f"{pk(gb[level], pb, nlines, k):>8.3f}"
                  f"{window_diff(gb[level], pb, nlines, k):>9.3f}")

    # 章级逐块对照
    print(f"\n{'=' * 72}")
    print("【章】边界逐个对照")
    print(f"gold 章边界: {sorted(gb['chapters'])}")
    for label, pr in preds.items():
        pb = sorted(bounds(pr["chapters"]))
        hits = [b for b in pb if any(abs(b - g) <= 3 for g in gb["chapters"])]
        print(f"{label:<16}{pb}")
        print(f"{'':<16}命中(±3): {hits}  漏掉: "
              f"{sorted(g for g in gb['chapters'] if not any(abs(g - b) <= 3 for b in pb))}")

    print(f"\ngold 的章：")
    for c in gold["chapters"]:
        print(f"  第{c['no']}章 [{c['start']}-{c['end']}] {c['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

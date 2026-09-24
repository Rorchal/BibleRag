# -*- coding: utf-8 -*-
"""复核节 v3：只复核边界，不管标题。每章调用一次复核者。

提示词逐字取自 output/prompts_复核节_v3.md 的「一、SYSTEM」「二、FEWSHOT」两节。
复核者只输出 changes（move / merge / split），由 verify_boundary 逐条校验后应用到原边界上；
不通过就重跑一次，再不过就退回切节原结果。

用法：
    export DS_BASE=https://api.deepseek.com/chat/completions
    export DS_KEY=<你的密钥>
    export DS_MODEL=deepseek-flash      # 只用 flash，见 CLAUDE.md
    python review_boundary.py <原文 txt> <切节 parsed.json> --tag rb1
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import ds_client as ds

ROOT = os.path.dirname(os.path.abspath(__file__))
PROMPT_MD = os.path.join(ROOT, "output", "prompts_复核节_v3.md")
EVID = re.compile(r"行(\d+)「([^」]{2,40})」")

USER_RB = """{fewshot}

━━━ 待复核的章 ━━━
这是第 {no} 章，占行 {lo}..{hi}，共 {n} 行。格式为「行号 | 内容」。

{numbered}

━━━ 切分者给出的节（只有区间）━━━
{sections_json}

━━━ 切分者自报的不确定点 ━━━
{uncertain_json}

━━━ 机器预检 ━━━
{precheck_hints}

逐条检查每一条边界和每一节的内部，找出切错的地方。只输出 changes 和 uncertain 的 json。"""


def load_prompts() -> tuple[str, str]:
    md = io.open(PROMPT_MD, encoding="utf-8").read()
    system = re.search(r"## 一、SYSTEM\n(.*?)\n---\n", md, re.S).group(1).strip()
    fewshot = re.search(r"## 二、FEWSHOT（放在 user 消息开头）\n(.*?)\n---\n", md, re.S).group(1).strip()
    return system, fewshot


def precheck(secs, lo, hi):
    """fatal：断裂/重叠/没覆盖整章；hints：超过 60 行的节。"""
    fatal, hints = [], []
    s = sorted(secs, key=lambda x: x["start"])
    if not s or s[0]["start"] != lo or s[-1]["end"] != hi:
        fatal.append(f"没有覆盖整章 {lo}..{hi}")
    for a, b in zip(s, s[1:]):
        if a["end"] + 1 != b["start"]:
            fatal.append(f"{a['start']}-{a['end']} 与 {b['start']}-{b['end']} 断裂或重叠")
    for x in s:
        n = x["end"] - x["start"] + 1
        if n > 60 and hi - lo + 1 >= 10:
            hints.append(f"{x['start']}-{x['end']} 共 {n} 行，超过 60 行，必须拆分")
    return fatal, hints


def verify_boundary(orig, changes, lines, lo, hi, min_conf=0.75):
    """逐条校验 changes 并应用。返回 (是否通过, 问题列表, 新边界集合)。边界 = 各节 start，不含 lo。"""
    errs = []
    bo = {x["start"] for x in orig} - {lo}
    srt = sorted(bo)
    long_secs = [(x["start"], x["end"]) for x in orig
                 if x["end"] - x["start"] + 1 > 60 and hi - lo + 1 >= 10]
    removed, added, touched, free = set(), set(), set(), 0

    if not isinstance(changes, list):
        return False, ["changes 不是数组"], bo
    for ch in changes:
        if not isinstance(ch, dict):
            errs.append(f"改动不是对象: {ch}")
            continue
        t = ch.get("type")
        try:
            if t == "move":
                key, old, new = "new_line", int(ch["line"]), int(ch["new_line"])
            elif t == "merge":
                key, old, new = "line", int(ch["line"]), None
            elif t == "split":
                key, old, new = "at", None, int(ch["at"])
            else:
                errs.append(f"未知的改动类型: {t}")
                continue
        except (KeyError, TypeError, ValueError):
            errs.append(f"{t} 缺少行号或行号不是整数: {ch}")
            continue
        keyline = new if t != "merge" else old

        m = EVID.search(str(ch.get("evidence") or ""))
        if not m:
            errs.append(f"{t} {keyline} 的 evidence 不是「行N「片段」」格式")
        else:
            n, frag = int(m.group(1)), m.group(2)
            if n != keyline:
                errs.append(f"{t} 的 evidence 引的是行{n}，应该引关键行 {keyline}")
            elif not (1 <= n <= len(lines) and frag in lines[n - 1]):
                errs.append(f"{t} 的 evidence 片段「{frag}」不在第 {n} 行原文里")
        try:
            conf = float(ch.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            errs.append(f"{t} {keyline} 置信度 {ch.get('confidence')} 低于 {min_conf}")

        for x in (old, new):
            if x is not None:
                if x in touched:
                    errs.append(f"行 {x} 被多条改动同时引用")
                touched.add(x)

        if t == "move":
            if old not in bo:
                errs.append(f"move 的 line {old} 不是原来的边界")
            elif new in bo or not lo < new <= hi:
                errs.append(f"move 的 new_line {new} 已是边界或越出本章")
            else:
                i = srt.index(old)
                prev = srt[i - 1] if i > 0 else lo
                nxt = srt[i + 1] if i + 1 < len(srt) else hi + 1
                if not prev < new < nxt:
                    errs.append(f"move {old}→{new} 越过了相邻边界（只能在 {prev}..{nxt} 之间）")
            removed.add(old)
            added.add(new)
            free += 1
        elif t == "merge":
            if old not in bo:
                errs.append(f"merge 的 line {old} 不是原来的边界")
            removed.add(old)
            free += 1
        else:
            if new in bo or not lo < new <= hi:
                errs.append(f"split 的 at {new} 已是边界或越出本章")
            added.add(new)
            if not any(s < new <= e for s, e in long_secs):  # 拆超长节是预检要求的，不计入
                free += 1

    nb = (bo - removed) | added
    cuts = [lo] + sorted(nb) + [hi + 1]
    if hi - lo + 1 >= 10:
        for a, b in zip(cuts, cuts[1:]):
            if b - a > 60:
                errs.append(f"应用后 {a}-{b - 1} 共 {b - a} 行，仍超过 60 行")
    if free > max(1, len(bo)) / 2:
        errs.append(f"非必要的改动 {free} 处，超过边界数 {len(bo)} 的一半，像是在重切")
    return (not errs), errs, nb


def rebuild(orig, bounds, lo, hi, chapter):
    """按新边界重建节。区间没变沿用原标题，变了的标 title_stale。"""
    by_rng = {(x["start"], x["end"]): x.get("title", "") for x in orig}
    cuts = [lo] + sorted(bounds) + [hi + 1]
    out = []
    for i, (a, b) in enumerate(zip(cuts, cuts[1:]), 1):
        rng = (a, b - 1)
        out.append({"no": f"{chapter}.{i}", "start": a, "end": b - 1,
                    "title": by_rng.get(rng, ""), "title_stale": rng not in by_rng,
                    "chapter": chapter})
    return out


def review_chapter(c, secs, unc, lines, system, fewshot, args, ctx):
    lo, hi = c["start"], c["end"]
    fatal, hints = precheck(secs, lo, hi)
    if fatal:
        return {"chapter": c["no"], "status": "预检致命错误，未复核：" + "；".join(fatal),
                "final": secs, "applied": [], "attempts": []}
    user = USER_RB.format(
        fewshot=fewshot, no=c["no"], lo=lo, hi=hi, n=hi - lo + 1,
        numbered="\n".join(f"{i} | {lines[i - 1]}" for i in range(lo, hi + 1)),
        sections_json=json.dumps([{"start": x["start"], "end": x["end"],
                                   "lines": x["end"] - x["start"] + 1} for x in secs],
                                 ensure_ascii=False),
        uncertain_json=json.dumps(unc, ensure_ascii=False, indent=1) if unc else "（无）",
        precheck_hints="\n".join(hints) if hints else "（无）")
    attempts = []
    for attempt in (1, 2):
        content, meta = ds.chat(system, user, max_tokens=args.max_tokens,
                                temperature=args.temperature, timeout=1200,
                                ctx={**ctx, "chapter": c["no"], "attempt": attempt})
        data = ds.parse_json(content) if content else None
        rec = {"usage": meta.get("usage"), "secs": meta.get("secs"),
               "finish_reason": meta.get("finish_reason"), "error": meta.get("error"),
               "content": content}
        if not isinstance(data, dict) or "changes" not in data:
            rec.update(parse_ok=False, verify_ok=False, verify_errs=["返回不是含 changes 的 json"])
            attempts.append(rec)
            continue
        ok, errs, nb = verify_boundary(secs, data["changes"], lines, lo, hi)
        rec.update(parse_ok=True, verify_ok=ok, verify_errs=errs, parsed=data)
        attempts.append(rec)
        if ok:
            return {"chapter": c["no"], "lo": lo, "hi": hi, "precheck_hints": hints,
                    "status": f"采用修正(第 {attempt} 次)", "applied": data["changes"],
                    "final": rebuild(secs, nb, lo, hi, c["no"]), "attempts": attempts}
    return {"chapter": c["no"], "lo": lo, "hi": hi, "precheck_hints": hints,
            "status": "两次都未通过校验，退回切节原结果", "applied": [],
            "final": secs, "attempts": attempts}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("txt")
    ap.add_argument("parsed")
    ap.add_argument("--tag", default="rb")
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    lines = ds.read_lines(a.txt)
    d = json.load(io.open(a.parsed, encoding="utf-8-sig"))
    top = max(s["end"] for s in d["sections"])
    if top != len(lines):
        print(f"原文 {len(lines)} 行，切节结果却覆盖到第 {top} 行 —— 两者不是同一篇，停止")
        return 2
    system, fewshot = load_prompts()
    ctx = {"file": os.path.basename(a.txt), "tag": a.tag, "stage": "review_boundary"}
    print(f"模型 {ds.MODEL}  temperature {a.temperature}  原文 {len(lines)} 行  {len(d['chapters'])} 章")

    jobs = []
    for c in d["chapters"]:
        secs = sorted((s for s in d["sections"] if s["chapter"] == c["no"]), key=lambda x: x["start"])
        unc = [u for u in d.get("uncertain", []) if c["start"] <= int(u.get("line", 0)) <= c["end"]]
        jobs.append((c, secs, unc))
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(lambda j: review_chapter(*j, lines, system, fewshot, a, ctx), jobs))

    tot = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    for r in res:
        for t in r["attempts"]:
            u = t.get("usage") or {}
            tot["calls"] += 1
            tot["prompt_tokens"] += u.get("prompt_tokens", 0)
            tot["completion_tokens"] += u.get("completion_tokens", 0)
            tot["reasoning_tokens"] += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        print(f"第{r['chapter']}章  {r['status']}  改动 {len(r['applied'])} 处  调用 {len(r['attempts'])} 次")
        for t in r["attempts"]:
            for e in t.get("verify_errs", []):
                print("    ✗", e)
        for ch in r["applied"]:
            print("    ✓", json.dumps(ch, ensure_ascii=False))
    print("tokens", tot)

    out = os.path.join(ROOT, "output", f"{a.tag}_复核边界_{os.path.splitext(os.path.basename(a.parsed))[0]}.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {"meta": {"model": ds.MODEL, "temperature": a.temperature, "prompt": "prompts_复核节_v3.md",
                  "source": os.path.basename(a.parsed), "tokens": tot},
         "chapters": d["chapters"], "sections": [x for r in res for x in r["final"]], "review": res},
        ensure_ascii=False, indent=2))
    print("输出：", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

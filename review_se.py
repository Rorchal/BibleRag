# -*- coding: utf-8 -*-
"""复核节 v2：每章调用一次复核者，纠正版经 verify_correction 校验后为准。

提示词逐字取自 output/prompts_复核节_v2.md 的「一、SYSTEM」「二、FEWSHOT」两节，
user 消息按其「三、user 消息模板」拼。流程见该文件「零、流程」。

用法：
    export DS_BASE=https://api.deepseek.com/chat/completions
    export DS_KEY=<你的密钥>
    export DS_MODEL=deepseek-flash      # 只用 flash，见 CLAUDE.md
    python review_se.py <原文 txt> <切节 parsed.json> --tag rv1
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
from verify_correction import pick_final, verify_correction

ROOT = os.path.dirname(os.path.abspath(__file__))
PROMPT_MD = os.path.join(ROOT, "output", "prompts_复核节_v2.md")

USER_RV = """{fewshot}

━━━ 待复核的章 ━━━
这是第 {no} 章，占行 {lo}..{hi}，共 {n} 行。格式为「行号 | 内容」。

{numbered}

━━━ 切分者给出的节 ━━━
{sections_json}

━━━ 切分者自报的不确定点 ━━━
{uncertain_json}

━━━ 机器预检 ━━━
{precheck_hints}

逐条复核后，输出纠正后的完整节列表。没改的节原样照抄；每一处改动都写进 changes。只输出 json。"""


def load_prompts() -> tuple[str, str]:
    md = io.open(PROMPT_MD, encoding="utf-8").read()
    system = re.search(r"## 一、SYSTEM\n(.*?)\n---\n", md, re.S).group(1).strip()
    fewshot = re.search(r"## 二、FEWSHOT（放在 user 消息开头）\n(.*?)\n---\n", md, re.S).group(1).strip()
    return system, fewshot


def precheck(secs: list[dict], lo: int, hi: int) -> tuple[list[str], list[str]]:
    """机械预检。fatal：断裂/重叠/没覆盖整章；hints：超 60 行、标题不在 25-50 字。

    后两项正是 verify_correction 会卡的硬条件，提前告诉复核者。
    """
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
            hints.append(f"{x['no']}（{x['start']}-{x['end']}）共 {n} 行，超过 60 行，必须拆分")
        t = len(x.get("title", ""))
        if not 25 <= t <= 50:
            hints.append(f"{x['no']}（{x['start']}-{x['end']}）标题 {t} 字，不在 25-50 字之间")
    return fatal, hints


def review_chapter(c, secs, unc, lines, system, fewshot, args, ctx):
    lo, hi = c["start"], c["end"]
    fatal, hints = precheck(secs, lo, hi)
    if fatal:  # 本脚本不重跑切节，直接报出来
        return {"chapter": c["no"], "status": "预检致命错误，未复核：" + "；".join(fatal),
                "final": secs, "changes": [], "attempts": []}
    user = USER_RV.format(
        fewshot=fewshot, no=c["no"], lo=lo, hi=hi, n=hi - lo + 1,
        numbered="\n".join(f"{i} | {lines[i - 1]}" for i in range(lo, hi + 1)),
        sections_json=json.dumps({"sections": [{k: x[k] for k in ("no", "start", "end", "title")}
                                               for x in secs]}, ensure_ascii=False, indent=1),
        uncertain_json=json.dumps(unc, ensure_ascii=False, indent=1) if unc else "（无）",
        precheck_hints="\n".join(hints) if hints else "（无）")
    attempts = []

    def run_reviewer():
        content, meta = ds.chat(system, user, max_tokens=args.max_tokens,
                                temperature=args.temperature, timeout=1200,
                                ctx={**ctx, "chapter": c["no"], "attempt": len(attempts) + 1})
        data = ds.parse_json(content) if content else None
        rec = {"usage": meta.get("usage"), "secs": meta.get("secs"),
               "finish_reason": meta.get("finish_reason"), "error": meta.get("error"),
               "content": content}
        if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
            rec["parse_ok"] = False
            data = {"sections": [], "changes": []}
        else:
            try:
                for x in data["sections"]:  # 行号统一成 int，便于校验
                    x["start"], x["end"] = int(x["start"]), int(x["end"])
                rec["parse_ok"] = True
            except (KeyError, TypeError, ValueError):
                rec["parse_ok"] = False
                data = {"sections": [], "changes": []}
        ok, errs = verify_correction(secs, data["sections"], data.get("changes", []),
                                     lines, lo, hi)
        rec.update(verify_ok=ok, verify_errs=errs, parsed=data)
        attempts.append(rec)
        return data

    final, status, changes = pick_final(secs, run_reviewer, lines, lo, hi)
    return {"chapter": c["no"], "lo": lo, "hi": hi, "precheck_hints": hints,
            "status": status, "changes": changes, "final": final, "attempts": attempts}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("txt")
    ap.add_argument("parsed")
    ap.add_argument("--tag", default="rv")
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
    ctx = {"file": os.path.basename(a.txt), "tag": a.tag, "stage": "review"}
    print(f"模型 {ds.MODEL}  temperature {a.temperature}  原文 {len(lines)} 行  {len(d['chapters'])} 章")

    jobs = []
    for c in d["chapters"]:
        secs = [s for s in d["sections"] if s["chapter"] == c["no"]]
        unc = [u for u in d.get("uncertain", []) if c["start"] <= int(u.get("line", 0)) <= c["end"]]
        jobs.append((c, secs, unc))
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(lambda j: review_chapter(*j, lines, system, fewshot, a, ctx), jobs))

    tot = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "calls": 0}
    for r in res:
        for t in r["attempts"]:
            u = t.get("usage") or {}
            tot["calls"] += 1
            tot["prompt_tokens"] += u.get("prompt_tokens", 0)
            tot["completion_tokens"] += u.get("completion_tokens", 0)
            tot["reasoning_tokens"] += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        print(f"第{r['chapter']}章  {r['status']}  改动 {len(r['changes'])} 处  调用 {len(r['attempts'])} 次")
        for t in r["attempts"]:
            if not t["verify_ok"]:
                for e in t["verify_errs"]:
                    print("    -", e)
    print("tokens", tot)

    final = [dict(x, chapter=r["chapter"]) for r in res for x in r["final"]]
    out = os.path.join(ROOT, "output", f"{a.tag}_复核_{os.path.splitext(os.path.basename(a.parsed))[0]}.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {"meta": {"model": ds.MODEL, "temperature": a.temperature, "source": os.path.basename(a.parsed),
                  "tokens": tot},
         "chapters": d["chapters"], "sections": final, "review": res},
        ensure_ascii=False, indent=2))
    print("输出：", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

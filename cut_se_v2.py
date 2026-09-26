# -*- coding: utf-8 -*-
"""切节 v2：两种方式，都用 prompts/切节_v2.md 的提示词。

  --mode chapter  逐章切：章区间取自 gold（只取行号，不给标题），每章调用一次，按 start 合并
  --mode whole    整篇切：整篇原文一次交给模型，直接切成节

整篇模式下 SYSTEM 需要把「某一章」改成「整篇」，改动见 WHOLE_EDITS，每处都断言恰好命中一次。
不重试（与 ds_client 的约定一致）：逐章模式某章失败则整章降级为 1 节并标记；整篇模式失败则该轮作废。

用法：
    export DS_BASE=https://api.deepseek.com/chat/completions DS_KEY=<密钥> DS_MODEL=deepseek-flash
    python cut_se_v2.py <原文 txt> --mode chapter --chapters output/gold_v3.md --tag c2r1
    python cut_se_v2.py <原文 txt> --mode whole --tag w2r1
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
PROMPT_MD = os.path.join(ROOT, "prompts", "切节_v2.md")

USER_CH = """{fewshot}

━━━ 待处理的章 ━━━
这是第 {no} 章,占行 {lo}..{hi},共 {n} 行。格式为「行号 | 内容」。

{numbered}

把这一章切成「节」,no 用 "{no}.1"、"{no}.2" 这样的编号。只输出「节」的 json。"""

USER_WHOLE = """{fewshot}

━━━ 待处理的转写稿 ━━━
共 {n} 行,行号 1..{n}。格式为「行号 | 内容」。

{numbered}

把全文切成「节」,no 用 "1"、"2"、"3" 这样的顺序编号。只输出「节」的 json。"""

# 整篇模式对 SYSTEM 的改动：只改「输入是某一章」相关的措辞，外加一句功能转换
# （逐章模式里章边界由调用方给出，整篇模式没有章，问候/诵读/祷告的边界要模型自己找）
WHOLE_EDITS = [
    ("输入是**某一章的全文**(行号沿用原稿的绝对行号)。", "输入是**整篇转写稿的全文**。"),
    ("你这一次**只做一件事:把这一章切成「节」**。不要输出章,不要输出段。",
     "你这一次**只做一件事:把全文直接切成「节」**。不要输出章,不要输出段。"),
    ("2. 无缝覆盖:所有「节」必须连续覆盖本章区间 [start..end],不重叠不留空,\n"
     "   前一节 end+1 严格等于后一节 start;首节 start = 本章 start,末节 end = 本章 end。",
     "2. 无缝覆盖:所有「节」必须连续覆盖全文 1..N,不重叠不留空,\n"
     "   前一节 end+1 严格等于后一节 start;首节 start = 1,末节 end = N。"),
    ("### 节 —— 章内的论题单元\n", "### 节 —— 论题单元\n"),
    ("数量:**按本章的长度定,不设固定上限。**", "数量:**按长度定,不设固定上限。**"),
    ("**本章不足 10 行时,允许整章就是 1 节。**",
     "**讲者功能的转换(开场问候 / 诵读经文 / 祷告 / 讲道 / 结束祷告)一定是节的边界,"
     "这类块不足 10 行也单独成节。**"),
    ("no 用「章号.序号」,章号由调用方在 user 消息里给出。", "no 用顺序编号 \"1\"、\"2\"、\"3\"。"),
]


def load_prompts():
    md = io.open(PROMPT_MD, encoding="utf-8").read()
    system = re.search(r"## 一、SYSTEM\n(.*?)\n## 二、FEWSHOT", md, re.S).group(1).strip()
    fewshot = re.search(r"## 二、FEWSHOT（放在 user 消息开头）\n(.*?)\n## 三、user 消息模板", md, re.S).group(1).strip()
    return system, fewshot


def whole_system(system: str) -> str:
    for a, b in WHOLE_EDITS:
        assert system.count(a) == 1, f"整篇改动没有恰好命中一次：{a[:30]}"
        system = system.replace(a, b)
    return system


def numbered(lines, lo, hi):
    return "\n".join(f"{i} | {lines[i - 1]}" for i in range(lo, hi + 1))


def call(system, user, ctx, a):
    content, meta = ds.chat(system, user, max_tokens=a.max_tokens, temperature=a.temperature,
                            timeout=1800, ctx=ctx, reasoning_effort=a.effort)
    data = ds.parse_json(content) if content else None
    rec = {"usage": meta.get("usage") or {}, "secs": meta.get("secs"),
           "finish_reason": meta.get("finish_reason"), "error": meta.get("error")}
    secs = []
    if isinstance(data, dict):
        for s in data.get("sections") or []:
            try:
                secs.append({"no": str(s.get("no")), "start": int(s["start"]), "end": int(s["end"]),
                             "title": str(s.get("title", "")).strip()})
            except (KeyError, TypeError, ValueError):
                continue
    if content and not secs:
        ds.log_failure("no_sections", content, ctx)
    return sorted(secs, key=lambda x: x["start"]), (data or {}).get("uncertain") or [], rec


def seam_issues(secs, lo, hi, label):
    out = []
    if not secs or secs[0]["start"] != lo or secs[-1]["end"] != hi:
        out.append(f"{label} 没有覆盖 {lo}..{hi}")
    for x, y in zip(secs, secs[1:]):
        if x["end"] + 1 != y["start"]:
            out.append(f"{label} {x['start']}-{x['end']} 与 {y['start']}-{y['end']} 断裂或重叠")
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--mode", required=True, choices=["chapter", "whole"])
    ap.add_argument("--chapters", default=None, help="逐章模式：章区间取自这个 gold markdown")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--effort", default=None, choices=["low", "high", "max"],
                    help="思考强度；不传则用模型默认（deepseek-flash 默认 high）")
    a = ap.parse_args()

    lines = ds.read_lines(a.path)
    N = len(lines)
    system, fewshot = load_prompts()
    ctx = {"file": os.path.basename(a.path), "tag": a.tag, "stage": f"se_v2_{a.mode}"}
    calls, unc, issues = [], [], []

    if a.mode == "chapter":
        import goldeval
        chapters = [{"no": c["no"], "start": c["start"], "end": c["end"]}
                    for c in goldeval.parse_gold(a.chapters)["chapters"]]

        def one(c):
            lo, hi = c["start"], c["end"]
            secs, u, rec = call(system, USER_CH.format(fewshot=fewshot, no=c["no"], lo=lo, hi=hi,
                                                       n=hi - lo + 1, numbered=numbered(lines, lo, hi)),
                                {**ctx, "chapter": c["no"]}, a)
            rec["chapter"] = c["no"]
            if not secs:
                secs = [{"no": f"{c['no']}.1", "start": lo, "end": hi, "title": "", "synthesized": True}]
                rec["degraded"] = True
            for s in secs:
                s["chapter"] = c["no"]
            return secs, u, rec, seam_issues(secs, lo, hi, f"章{c['no']}内的节")

        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(one, chapters))
        sections = [s for r in res for s in r[0]]
        for r in res:
            unc += r[1]
            calls.append(r[2])
            issues += r[3]
    else:
        chapters = []
        sections, unc, rec = call(whole_system(system),
                                  USER_WHOLE.format(fewshot=fewshot, n=N, numbered=numbered(lines, 1, N)),
                                  ctx, a)
        calls.append(rec)
        if not sections:
            print("整篇调用失败：", rec.get("error") or rec.get("finish_reason"))
            issues.append("整篇调用失败")
        else:
            issues += seam_issues(sections, 1, N, "全文的节")

    tok = {"prompt": sum(c["usage"].get("prompt_tokens", 0) for c in calls),
           "completion": sum(c["usage"].get("completion_tokens", 0) for c in calls)}
    print(f"[{a.tag}] {a.mode}  调用 {len(calls)}  节 {len(sections)}  问题 {len(issues)}  tokens {tok}")
    for m in issues[:10]:
        print("   -", m)
    out = os.path.join(ROOT, "output", f"{a.tag}_se_v2_{a.mode}_{os.path.splitext(os.path.basename(a.path))[0]}.parsed.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {"meta": {"pipeline": f"se_v2_{a.mode}", "tag": a.tag, "model": ds.MODEL,
                  "temperature": a.temperature, "effort": a.effort, "tokens": tok, "calls": calls},
         "issues": issues, "chapters": chapters, "sections": sections, "uncertain": unc},
        ensure_ascii=False, indent=2))
    return 0 if sections else 2


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""切章：用 prompts/切章.md，全文一次调用，只切「章」。不重试。

输出 output/<tag>_ch_<原文名>.parsed.json，含 chapters（no/start/end/title/trigger/evidence/confidence）、
uncertain、issues（覆盖 / 断裂检查）。可直接交给 cut_se_v2.py --chapters-json 切节。

用法：
    export DS_BASE=https://api.deepseek.com/chat/completions DS_KEY=<密钥> DS_MODEL=deepseek-flash
    python cut_chapters.py <原文 txt> --tag ch1 --effort low
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

import ds_client as ds

ROOT = os.path.dirname(os.path.abspath(__file__))
PROMPT_MD = os.path.join(ROOT, "prompts", "切章.md")   # --prompt 可换成 切章_v2（带 typos）

USER = """{fewshot}

━━━ 待处理的转写稿 ━━━
共 {n} 行,行号 1..{n}。格式为「行号 | 内容」。

{numbered}

只输出「章」的 json{tail}。"""


def load_prompts():
    md = io.open(PROMPT_MD, encoding="utf-8").read()
    system = re.search(r"## 一、SYSTEM\n(.*?)\n## 二、FEWSHOT", md, re.S).group(1).strip()
    fewshot = re.search(r"## 二、FEWSHOT（放在 user 消息开头）\n(.*?)\n## 三、user 消息模板", md, re.S).group(1).strip()
    return system, fewshot


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--prompt", default="切章", help="prompts/ 下的文件名（不含 .md），如 切章_v2")
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--effort", default=None, choices=["low", "high", "max"],
                    help="思考强度；不传则用模型默认")
    a = ap.parse_args()
    global PROMPT_MD
    PROMPT_MD = os.path.join(ROOT, "prompts", f"{a.prompt}.md")
    with_typos = "typos" in io.open(PROMPT_MD, encoding="utf-8").read()

    lines = ds.read_lines(a.path)
    N = len(lines)
    system, fewshot = load_prompts()
    user = USER.format(fewshot=fewshot, n=N, tail=",顺带把识别错字写进 typos" if with_typos else "", numbered="\n".join(f"{i} | {l}" for i, l in enumerate(lines, 1)))
    ctx = {"file": os.path.basename(a.path), "tag": a.tag, "stage": "chapter"}
    content, meta = ds.chat(system, user, max_tokens=a.max_tokens, temperature=a.temperature,
                            timeout=1800, ctx=ctx, reasoning_effort=a.effort)
    data = ds.parse_json(content) if content else None

    chapters, issues = [], []
    if isinstance(data, dict):
        for c in data.get("chapters") or []:
            try:
                chapters.append({**c, "no": int(c["no"]), "start": int(c["start"]), "end": int(c["end"]),
                                 "title": str(c.get("title", "")).strip()})
            except (KeyError, TypeError, ValueError):
                continue
    chapters.sort(key=lambda c: c["start"])
    for i, c in enumerate(chapters, 1):   # 按顺序重新编号，避免模型编号跳号
        c["no"] = i
    if not chapters:
        issues.append(f"没有切出章：{meta.get('error') or meta.get('finish_reason') or '返回内容为空'}")
    else:
        if chapters[0]["start"] != 1 or chapters[-1]["end"] != N:
            issues.append(f"章没有覆盖 1..{N}")
        for x, y in zip(chapters, chapters[1:]):
            if x["end"] + 1 != y["start"]:
                issues.append(f"章 {x['start']}-{x['end']} 与 {y['start']}-{y['end']} 断裂或重叠")

    usage = meta.get("usage") or {}
    print(f"[{a.tag}] 切章  {N} 行  章 {len(chapters)}  问题 {len(issues)}  {meta.get('secs')}s  "
          f"入 {usage.get('prompt_tokens')} 出 {usage.get('completion_tokens')}")
    for c in chapters:
        print(f"   第{c['no']}章 {c['start']}-{c['end']}  {c['title']}")
    for m in issues:
        print("   -", m)
    if with_typos:
        print(f"   上报错字 {len((data or {}).get('typos') or [])} 条")
    out = os.path.join(ROOT, "output", f"{a.tag}_ch_{os.path.splitext(os.path.basename(a.path))[0]}.parsed.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {"meta": {"pipeline": "chapter", "prompt": a.prompt, "tag": a.tag, "model": ds.MODEL, "temperature": a.temperature,
                  "effort": a.effort, "usage": usage, "secs": meta.get("secs"),
                  "finish_reason": meta.get("finish_reason")},
         "issues": issues, "chapters": chapters, "uncertain": (data or {}).get("uncertain") or [],
         "typos": (data or {}).get("typos") or []},
        ensure_ascii=False, indent=2))
    print(f"   → {os.path.relpath(out, ROOT)}")
    return 0 if chapters and not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())

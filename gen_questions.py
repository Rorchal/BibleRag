# -*- coding: utf-8 -*-
"""按段生成检索用问题：读 切分结果/<名字>/结构.json，每章调一次模型（prompts/生成问题_v1.md），不重试。

输出：
  output/questions_<tag>/ch<N>.raw.json     每章的原始返回与 usage
  切分结果/<名字>/问题.json                    章→节→段，每段附 questions
  切分结果/<名字>/问题.md                      可读版：段标题 + 问题
校验（只记录，不重跑）：每段都要在输出里出现、start/end 一致；问题是非空字符串；
  问题里不许出现 讲道人/讲者/牧师/作者/本段/这段 和「」外的 我/你/咱。

用法：
  export DS_KEY=<密钥>
  python gen_questions.py --name GH_约伯记1章1到8节_新热词 --effort low
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

import ds_client as ds
import seg_paras
import typo_fix

HERE = os.path.dirname(os.path.abspath(__file__))
BANNED = re.compile(r"讲道人|讲者|讲员|牧师|作者|本段|这段|这一段|上面|刚才")
PERSON = re.compile(r"[我你咱]")


def load_prompts(name: str) -> tuple[str, str]:
    md = io.open(os.path.join(HERE, "prompts", f"{name}.md"), encoding="utf-8").read()
    system = re.search(r"## 一、SYSTEM\n(.*?)\n---\n", md, re.S).group(1).strip()
    fewshot = re.search(r"## 二、FEWSHOT[^\n]*\n(.*?)\n---\n", md, re.S).group(1).strip()
    return system, fewshot


def para_block(lines: list[str], p: dict) -> str:
    text = "\n".join(lines[p["start"] - 1:p["end"]])
    return f"【段 行 {p['start']}–{p['end']}】{p.get('title', '')}\n{text}"


def build_user(fewshot: str, names: str, ch: dict, lines: list[str]) -> str:
    paras = [p for s in ch["sections"] for p in s["paragraphs"]]
    return (f"{fewshot}\n\n━━━ 待处理的章 ━━━\n专名表:{names}\n"
            f"第{ch['no']}章「{ch.get('title', '')}」,共 {len(paras)} 段。"
            f"每段先是「【段 行 起–止】段标题」,下面是原文。\n\n"
            + "\n\n".join(para_block(lines, p) for p in paras)
            + '\n\n只输出 {"paragraphs": [...]}。')


def check_questions(qs: list, protected_ok: bool = True) -> list[str]:
    issues = []
    for q in qs:
        if not isinstance(q, str) or not q.strip():
            issues.append("空问题")
            continue
        if BANNED.search(q):
            issues.append(f"禁用词: {q}")
        outside = re.sub(r"「[^」]*」", "", q)
        if PERSON.search(outside):
            issues.append(f"人称: {q}")
        if len(q) > 40:
            issues.append(f"过长: {q}")
    return issues


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="切分结果/ 下的目录名")
    ap.add_argument("--prompt", default="生成问题_v1")
    ap.add_argument("--effort", default=None, choices=["low", "high", "max"])
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--chapters", default=None, help="只跑这些章，如 1,2")
    a = ap.parse_args()

    rdir = os.path.join(HERE, "切分结果", a.name)
    struct = json.load(io.open(os.path.join(rdir, "结构.json"), encoding="utf-8"))
    txt = os.path.join(rdir, "校对后原文.txt")
    if not os.path.exists(txt):
        txt = os.path.join(HERE, struct["source"]["原文"])
    lines = typo_fix.read_lines(txt)
    system, fewshot = load_prompts(a.prompt)
    names = seg_paras.lexicon_names()
    tag = a.name.replace("GH_", "")
    odir = os.path.join(HERE, "output", f"questions_{tag}")
    os.makedirs(odir, exist_ok=True)
    only = {int(x) for x in a.chapters.split(",")} if a.chapters else None

    usage_sum = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}
    n_paras = n_q = 0
    problems = []
    for ch in struct["chapters"]:
        if only and ch["no"] not in only:
            continue
        user = build_user(fewshot, names, ch, lines)
        content, meta = ds.chat(system, user, max_tokens=a.max_tokens, temperature=a.temperature,
                                timeout=1800, ctx={"name": a.name, "stage": "questions", "chapter": ch["no"]},
                                reasoning_effort=a.effort)
        io.open(os.path.join(odir, f"ch{ch['no']}.raw.json"), "w", encoding="utf-8").write(
            json.dumps({"content": content, "meta": meta}, ensure_ascii=False, indent=1))
        u = meta.get("usage") or {}
        for k in usage_sum:
            usage_sum[k] += u.get(k) or 0
        data = ds.parse_json(content) if content else None
        got = {}
        for p in (data or {}).get("paragraphs") or [] if isinstance(data, dict) else []:
            try:
                got[(int(p["start"]), int(p["end"]))] = [str(q).strip() for q in (p.get("questions") or [])]
            except (KeyError, TypeError, ValueError):
                problems.append(f"第{ch['no']}章：输出里有一段格式不对 {p}")
        ch_q = 0
        for s in ch["sections"]:
            for p in s["paragraphs"]:
                key = (p["start"], p["end"])
                qs = got.pop(key, None)
                if qs is None:
                    problems.append(f"第{ch['no']}章 行{p['start']}–{p['end']}：输出里没有这一段")
                    qs = []
                for why in check_questions(qs):
                    problems.append(f"第{ch['no']}章 行{p['start']}–{p['end']}：{why}")
                p["questions"] = qs
                n_paras += 1
                ch_q += len(qs)
        for key in got:
            problems.append(f"第{ch['no']}章：输出多出一段 {key}")
        n_q += ch_q
        print(f"第{ch['no']}章  {len([p for s in ch['sections'] for p in s['paragraphs']])} 段 → {ch_q} 问  "
              f"{meta.get('secs')}s  入 {u.get('prompt_tokens')}（缓存命中 {u.get('prompt_cache_hit_tokens')}）"
              f" 出 {u.get('completion_tokens')}" + ("" if meta.get("ok") else f"  失败: {meta.get('error')}"),
              flush=True)

    struct["questions"] = {"prompt": a.prompt, "model": ds.MODEL, "effort": a.effort,
                           "temperature": a.temperature, "usage": usage_sum, "problems": problems}
    io.open(os.path.join(rdir, "问题.json"), "w", encoding="utf-8").write(
        json.dumps(struct, ensure_ascii=False, indent=1))

    md = [f"# {a.name} · 按段生成的检索问题", "",
          f"- {n_paras} 段 / {n_q} 问；{a.prompt} · {ds.MODEL} · effort {a.effort}",
          f"- 校验问题 {len(problems)} 条（见 问题.json 的 problems）", "", "---", ""]
    for ch in struct["chapters"]:
        if only and ch["no"] not in only:
            continue
        md.append(f"## 第{ch['no']}章　{ch.get('title', '')}（行 {ch['start']}–{ch['end']}）\n")
        for s in ch["sections"]:
            md.append(f"### {s['no']}　{s.get('title', '')}（行 {s['start']}–{s['end']}）\n")
            for p in s["paragraphs"]:
                md.append(f"**行 {p['start']}–{p['end']}**　{p.get('title', '')}\n")
                md += [f"- {q}" for q in p.get("questions", [])] or ["- （无）"]
                md.append("")
    io.open(os.path.join(rdir, "问题.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print(f"\n{n_paras} 段 → {n_q} 问；校验问题 {len(problems)} 条；"
          f"tokens 入 {usage_sum['prompt_tokens']}（缓存命中 {usage_sum['prompt_cache_hit_tokens']}）"
          f" 出 {usage_sum['completion_tokens']} → {os.path.relpath(rdir, HERE)}/问题.md")
    for p in problems[:30]:
        print("  ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

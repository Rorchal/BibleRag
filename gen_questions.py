# -*- coding: utf-8 -*-
"""按段生成检索用问题：读 切分结果/<名字>/结构.json，每一章调一次模型（这一章的所有段放进同一批），不重试；最后把各章结果合并。

提示词 prompts/段问题_v4.md（<<<SYSTEM>>> / <<<USER>>> 两段，USER 里的 {payload} 换成这一节的 JSON）：
  {"讲道": "约伯记1章1到8节", "主经文": "约伯记1章1到8节",
   "paragraphs": [{"id": "L19-23", "title": "段标题", "text": "原文（按行拼接）"}]}
模型返回 {"paragraphs": [{"id", "kind", "questions": [2 条]}]}。

输出：
  output/questions_<tag>/ch<章号>.raw.json        每章的原始返回与 usage
  切分结果/<名字>/问题.json                      结构.json + 每段 kind、questions
  切分结果/<名字>/问题.md                        可读版
校验（只记录，不重跑）：每段 id 都要出现；讲道/经文诵读段 2 条问题，其余 1~2 条；8~35 字、以问号结尾；
  不含 这段/本段/上文/这一节；与原文连续相同的字不超过 6 个（引号内除外）。

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
import typo_fix

HERE = os.path.dirname(os.path.abspath(__file__))
DEIXIS = re.compile(r"这段|本段|上文|这一节|这节|本节")
KINDS = {"讲道", "经文诵读", "祷告", "开场过渡"}


def load_prompt(name: str) -> tuple[str, str]:
    md = io.open(os.path.join(HERE, "prompts", f"{name}.md"), encoding="utf-8").read()
    m = re.search(r"<<<SYSTEM>>>\n(.*?)\n<<<USER>>>\n(.*)", md, re.S)
    return m.group(1).strip(), m.group(2).strip()


def sermon_title(name: str) -> str:
    t = name.replace("GH_", "")
    t = re.sub(r"_.*$", "", t)          # 去掉 _新热词 之类的后缀
    return t.replace("伯", "约伯记") if t.startswith("伯") else t


def payload(sermon: str, paras: list[dict], lines: list[str]) -> dict:
    return {"讲道": sermon, "主经文": sermon,
            "paragraphs": [{"id": f"L{p['start']}-{p['end']}", "title": p.get("title", ""),
                            "text": "".join(lines[p["start"] - 1:p["end"]])}
                           for p in paras]}


def longest_overlap(q: str, text: str) -> int:
    q = re.sub(r"[“「][^”」]*[”」]", "", q)
    best = 0
    for n in range(len(q), 6, -1):
        for i in range(len(q) - n + 1):
            if q[i:i + n] in text:
                return n
    return best


def check(p: dict, text: str) -> list[str]:
    out = []
    qs = p.get("questions") or []
    need = 2 if p.get("kind") in ("讲道", "经文诵读") and len(text) >= 60 else 1
    if not need <= len(qs) <= 2:
        out.append(f"问题数 {len(qs)}（kind {p.get('kind')} 应为 {need}~2）")
    if p.get("kind") not in KINDS:
        out.append(f"kind 不在四选一: {p.get('kind')}")
    for q in qs:
        if not isinstance(q, str):
            out.append("问题不是字符串")
            continue
        if not q.rstrip().endswith(("?", "？")):
            out.append(f"不以问号结尾: {q}")
        if not 8 <= len(q.rstrip("?？")) <= 35:
            out.append(f"长度 {len(q)}: {q}")
        if DEIXIS.search(q):
            out.append(f"指代词: {q}")
        n = longest_overlap(q, text)
        if n > 6:
            out.append(f"与原文连续相同 {n} 字: {q}")
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="切分结果/ 下的目录名")
    ap.add_argument("--prompt", default="段问题_v6")
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
    system, user_tpl = load_prompt(a.prompt)
    sermon = sermon_title(a.name)
    tag = a.name.replace("GH_", "")
    odir = os.path.join(HERE, "output", f"questions_{tag}")
    os.makedirs(odir, exist_ok=True)
    only = {int(x) for x in a.chapters.split(",")} if a.chapters else None

    usage_sum = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}
    n_paras = n_q = n_calls = 0
    problems = []
    for ch in struct["chapters"]:
        if only and ch["no"] not in only:
            continue
        paras = [p for sec in ch["sections"] for p in sec["paragraphs"]]
        pl = payload(sermon, paras, lines)
        user = user_tpl.replace("{payload}", json.dumps(pl, ensure_ascii=False))
        content, meta = ds.chat(system, user, max_tokens=a.max_tokens, temperature=a.temperature,
                                timeout=1800, ctx={"name": a.name, "stage": "questions", "chapter": ch["no"]},
                                reasoning_effort=a.effort)
        n_calls += 1
        io.open(os.path.join(odir, f"ch{ch['no']}.raw.json"), "w", encoding="utf-8").write(
            json.dumps({"payload": pl, "content": content, "meta": meta}, ensure_ascii=False, indent=1))
        u = meta.get("usage") or {}
        for k in usage_sum:
            usage_sum[k] += u.get(k) or 0
        data = ds.parse_json(content) if content else None
        got = {}
        for p in ((data or {}).get("paragraphs") or []) if isinstance(data, dict) else []:
            if isinstance(p, dict) and p.get("id"):
                got[str(p["id"])] = p
        ch_q = 0
        for p, pin in zip(paras, pl["paragraphs"]):
            r = got.pop(pin["id"], None)
            if r is None:
                problems.append(f"第{ch['no']}章 {pin['id']}：输出里没有这一段")
                r = {"kind": None, "questions": []}
            for why in check(r, pin["text"]):
                problems.append(f"第{ch['no']}章 {pin['id']}：{why}")
            p["kind"] = r.get("kind")
            p["questions"] = [str(q).strip() for q in (r.get("questions") or [])]
            n_paras += 1
            ch_q += len(p["questions"])
        for k in got:
            problems.append(f"第{ch['no']}章：输出多出 {k}")
        n_q += ch_q
        print(f"第{ch['no']}章  {len(paras)} 段 → {ch_q} 问  {meta.get('secs')}s  "
              f"入 {u.get('prompt_tokens')}（缓存命中 {u.get('prompt_cache_hit_tokens')}） 出 {u.get('completion_tokens')}"
              f"  finish={meta.get('finish_reason')}" + ("" if meta.get("ok") else f"  失败: {meta.get('error')}"), flush=True)

    struct["questions"] = {"prompt": a.prompt, "model": ds.MODEL, "effort": a.effort,
                           "temperature": a.temperature, "calls": n_calls, "usage": usage_sum, "problems": problems}
    io.open(os.path.join(rdir, "问题.json"), "w", encoding="utf-8").write(
        json.dumps(struct, ensure_ascii=False, indent=1))

    md = [f"# {a.name} · 按段生成的检索问题", "",
          f"- {n_paras} 段 / {n_q} 问；{a.prompt} · {ds.MODEL} · effort {a.effort} · 每章一次调用，共 {n_calls} 次",
          f"- 校验问题 {len(problems)} 条（见 问题.json 的 problems）", "", "---", ""]
    for ch in struct["chapters"]:
        if only and ch["no"] not in only:
            continue
        md.append(f"## 第{ch['no']}章　{ch.get('title', '')}（行 {ch['start']}–{ch['end']}）\n")
        for s in ch["sections"]:
            md.append(f"### {s['no']}　{s.get('title', '')}（行 {s['start']}–{s['end']}）\n")
            for p in s["paragraphs"]:
                md.append(f"**行 {p['start']}–{p['end']}**　[{p.get('kind') or '?'}]　{p.get('title', '')}\n")
                md += [f"- {q}" for q in p.get("questions", [])] or ["- （无）"]
                md.append("")
    io.open(os.path.join(rdir, "问题.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print(f"\n{n_calls} 次调用，{n_paras} 段 → {n_q} 问；校验问题 {len(problems)} 条；"
          f"tokens 入 {usage_sum['prompt_tokens']}（缓存命中 {usage_sum['prompt_cache_hit_tokens']}）"
          f" 出 {usage_sum['completion_tokens']} → {os.path.relpath(rdir, HERE)}/问题.md")
    for p in problems[:40]:
        print("  ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

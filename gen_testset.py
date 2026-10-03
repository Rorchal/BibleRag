# -*- coding: utf-8 -*-
"""给每一段生成 1 句「模拟用户搜索话」作为检索测试集（不进索引）。按章调用，提示词 prompts/测试提问_v1.md。

输出：切分结果/<名字>/测试提问.json
  {"prompt":..., "items":[{"id":"GH_xxx#L19-23","query":"..."}], "usage":..., "problems":[...]}
用法：
  export DS_KEY=<密钥>
  python gen_testset.py --name GH_约伯记1章1到8节_新热词 --effort low
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import ds_client as ds
import typo_fix
from gen_questions import load_prompt, payload, sermon_title

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--prompt", default="测试提问_v1")
    ap.add_argument("--effort", default=None, choices=["low", "high", "max"])
    ap.add_argument("--temperature", type=float, default=0.7, help="测试集要多样，温度比生成问题高")
    ap.add_argument("--max-tokens", type=int, default=64000)
    a = ap.parse_args()

    rdir = os.path.join(HERE, "切分结果", a.name)
    struct = json.load(io.open(os.path.join(rdir, "结构.json"), encoding="utf-8"))
    lines = typo_fix.read_lines(os.path.join(rdir, "校对后原文.txt"))
    system, user_tpl = load_prompt(a.prompt)
    sermon = sermon_title(a.name)
    items, problems = [], []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}
    for ch in struct["chapters"]:
        paras = [p for s in ch["sections"] for p in s["paragraphs"]]
        pl = payload(sermon, paras, lines)
        user = user_tpl.replace("{payload}", json.dumps(pl, ensure_ascii=False))
        content, meta = ds.chat(system, user, max_tokens=a.max_tokens, temperature=a.temperature, timeout=1800,
                                ctx={"name": a.name, "stage": "testset", "chapter": ch["no"]},
                                reasoning_effort=a.effort)
        u = meta.get("usage") or {}
        for k in usage:
            usage[k] += u.get(k) or 0
        data = ds.parse_json(content) if content else None
        got = {str(p.get("id")): str(p.get("query") or "").strip()
               for p in ((data or {}).get("paragraphs") or []) if isinstance(p, dict)}
        n = 0
        for pin in pl["paragraphs"]:
            q = got.get(pin["id"])
            if not q:
                problems.append(f"第{ch['no']}章 {pin['id']}：没有输出")
                continue
            items.append({"id": f"{a.name}#{pin['id']}", "query": q})
            n += 1
        print(f"第{ch['no']}章  {len(paras)} 段 → {n} 句  {meta.get('secs')}s  入 {u.get('prompt_tokens')}"
              f"（缓存命中 {u.get('prompt_cache_hit_tokens')}） 出 {u.get('completion_tokens')}"
              + ("" if meta.get("ok") else f"  失败: {meta.get('error')}"), flush=True)
    out = os.path.join(rdir, "测试提问.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {"prompt": a.prompt, "model": ds.MODEL, "effort": a.effort, "temperature": a.temperature,
         "usage": usage, "problems": problems, "items": items}, ensure_ascii=False, indent=1))
    print(f"{len(items)} 句，问题 {len(problems)} 条 → {os.path.relpath(out, HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

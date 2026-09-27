# -*- coding: utf-8 -*-
"""校对：把热词表和全文交给 DeepSeek，只让它报「错词→正词」和行号，不重写原文。

提示词 prompts/校对_v1.md；热词表 lexicon/热词表.txt 追加在 SYSTEM 末尾（每篇相同，吃前缀缓存）。
输出 output/<tag>_typos_<原文名>.json：{"meta": ..., "typos": [...]}，交给 typo_fix.py apply 按行替换。
全文一次调用，不重试。

用法：
    export DS_BASE=https://api.deepseek.com/chat/completions DS_KEY=<密钥> DS_MODEL=deepseek-flash
    python correct_typos.py <原文 txt> --tag x --effort low
    python typo_fix.py apply <原文 txt> output/x_typos_<原文名>.json --out <校对后.txt> --log <日志.json> --add-hotwords
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

ROOT = os.path.dirname(os.path.abspath(__file__))

USER = """{fewshot}

━━━ 待校对的转写稿 ━━━
共 {n} 行,行号 1..{n}。格式为「行号 | 内容」。

{numbered}

只输出 {{"typos": [...]}}。"""


def load_prompts(name: str = "校对_v1") -> tuple[str, str]:
    md = io.open(os.path.join(ROOT, "prompts", f"{name}.md"), encoding="utf-8").read()
    system = re.search(r"## 一、SYSTEM\n(.*?)\n---\n", md, re.S).group(1).strip()
    fewshot = re.search(r"## 二、FEWSHOT[^\n]*\n(.*?)\n---\n", md, re.S).group(1).strip()
    return system, fewshot


def lexicon_block() -> str:
    groups = typo_fix.load_lexicon()
    rows = [f"【{g}】" + "、".join(ws) for g, ws in groups.items() if ws]
    return "## 热词表(正确写法)\n" + "\n".join(rows)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--prompt", default="校对_v1")
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--effort", default=None, choices=["low", "high", "max"])
    a = ap.parse_args()

    lines = typo_fix.read_lines(a.path)          # 保留原行号（不丢空行）
    system, fewshot = load_prompts(a.prompt)
    system = system + "\n\n" + lexicon_block()
    user = USER.format(fewshot=fewshot, n=len(lines),
                       numbered="\n".join(f"{i} | {l}" for i, l in enumerate(lines, 1)))
    content, meta = ds.chat(system, user, max_tokens=a.max_tokens, temperature=a.temperature,
                            timeout=1800, ctx={"file": os.path.basename(a.path), "tag": a.tag,
                                               "stage": "proofread"},
                            reasoning_effort=a.effort)
    data = ds.parse_json(content) if content else None
    typos = (data or {}).get("typos") or [] if isinstance(data, dict) else []

    usage = meta.get("usage") or {}
    print(f"[{a.tag}] 校对  {len(lines)} 行  上报 {len(typos)} 条  {meta.get('secs')}s  "
          f"入 {usage.get('prompt_tokens')}（缓存命中 {usage.get('prompt_cache_hit_tokens')}） "
          f"出 {usage.get('completion_tokens')}"
          + ("" if data is not None else f"  【没有解析出结果：{meta.get('error') or '返回内容为空或不是 json'}】"))
    for t in typos:
        print(f"   {t.get('wrong')} → {t.get('right')}  行{t.get('lines')}  {t.get('why', '')}")
    out = os.path.join(ROOT, "output", f"{a.tag}_typos_{os.path.splitext(os.path.basename(a.path))[0]}.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(
        {"meta": {"stage": "proofread", "prompt": a.prompt, "tag": a.tag, "model": ds.MODEL,
                  "temperature": a.temperature, "effort": a.effort, "usage": usage,
                  "secs": meta.get("secs"), "finish_reason": meta.get("finish_reason")},
         "typos": typos}, ensure_ascii=False, indent=2))
    print(f"   → {os.path.relpath(out, ROOT)}")
    return 0 if data is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())

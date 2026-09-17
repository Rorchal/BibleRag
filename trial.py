# -*- coding: utf-8 -*-
"""调 DeepSeek 把一篇讲道稿拆成「章/节/段 + 每段概要」，并核验结果。

用法：
  python trial.py <txt> --lines 0 --max-tokens 16000 --tag full2
  --lines 0 表示整篇；非 0 表示只取前 N 行试跑
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import ds_client as ds


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--lines", type=int, default=0, help="取前 N 行，0 表示整篇")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--tag", default="trial")
    a = ap.parse_args()

    all_lines = ds.read_lines(a.path)
    lines = all_lines if a.lines == 0 else all_lines[:a.lines]
    lo, hi = 1, len(lines)
    numbered = "\n".join(f"{i} | {ln}" for i, ln in enumerate(lines, 1))

    print(f"文件 {os.path.basename(a.path)}：全文 {len(all_lines)} 行，"
          f"本次取 {len(lines)} 行（{len(''.join(lines))} 字）")
    print("调用 DeepSeek …（单次，不重试）")

    content, meta = ds.chat(
        ds.SYSTEM,
        ds.USER_TMPL.format(n=len(lines), lo=lo, hi=hi, numbered=numbered),
        max_tokens=a.max_tokens,
        ctx={"file": os.path.basename(a.path), "lines": len(lines), "tag": a.tag},
    )

    print("\n--- 调用结果 ---")
    print(f"耗时 {meta.get('secs')}s  usage={meta.get('usage')}  "
          f"finish_reason={meta.get('finish_reason')}")
    if content is None:
        print(f"【失败】{meta.get('error')}")
        print("已记录到 logs/api_failures.jsonl，不重试。")
        return 2
    if meta.get("truncated"):
        print("【警告】finish_reason=length，输出被 max_tokens 截断")

    data = ds.parse_json(content)
    if data is None:
        ds.log_failure("json_parse_failed", content, {"file": os.path.basename(a.path)})
        print("【失败】返回不是合法 JSON，原文前 500 字：")
        print(content[:500])
        return 3

    paras = ds.flatten(data)
    v = ds.verify(paras, lo, hi, lines)
    sq = ds.check_summaries(paras, lines, lo)
    doc_summary = str(data.get("doc_summary", "")).strip()

    n_ch = len({p["chapter"] for p in paras})
    n_sec = len({(p["chapter"], p["section"]) for p in paras})
    print("\n--- 结构 ---")
    print(f"章 {n_ch} 个，节 {n_sec} 个，段 {len(paras)} 个")

    print("\n--- 正文核验（硬性，必须全绿）---")
    print(f"行覆盖 {v['covered']}/{hi - lo + 1}（{v['coverage_rate']:.1%}）")
    print(f"拼回正文与原文逐字一致: {'是' if v['text_identical'] else '否'}"
          f"（原文 {v['orig_chars']} 字 / 拼回 {v['rebuilt_chars']} 字）")
    if v["issues"]:
        print("问题：")
        for msg in v["issues"]:
            print("  -", msg)
    else:
        print("无问题")

    print("\n--- 概要质量（新生成内容，只做可自动判定的检查）---")
    if doc_summary:
        print(f"全篇主旨：{doc_summary}")
    print(f"有概要 {sq['with_summary']}/{sq['paragraphs']}，"
          f"有关键词 {sq['with_keywords']}/{sq['paragraphs']}")
    print(f"概要平均长度 {sq['summary_len_avg']} 字")
    print(f"关键词命中原文 {sq['keyword_in_text_rate']:.1%}（共 {sq['keyword_total']} 个）")
    for label, key in (("缺概要", "missing_summary"), ("缺关键词", "missing_keywords"),
                       ("概要过短", "too_short"), ("概要过长", "too_long"),
                       ("空话套话", "vague_wording"), ("残留口语", "oral_wording")):
        if sq[key]:
            print(f"  {label}: {sq[key]}")
    if sq["keyword_not_in_text_samples"]:
        print(f"  关键词未见于原文（抽样）: {sq['keyword_not_in_text_samples']}")

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output",
                        f"{a.tag}_{os.path.splitext(os.path.basename(a.path))[0]}")
    os.makedirs(os.path.dirname(base), exist_ok=True)
    io.open(base + ".raw.json", "w", encoding="utf-8").write(content)
    io.open(base + ".verify.json", "w", encoding="utf-8").write(
        json.dumps({"meta": meta, "verify": v, "summary_quality": sq,
                    "doc_summary": doc_summary, "paragraphs": paras},
                   ensure_ascii=False, indent=2))
    chunks = ds.build_chunks(paras, lines, lo, os.path.basename(a.path), doc_summary)
    with io.open(base + ".chunks.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    io.open(base + ".md", "w", encoding="utf-8").write(
        ds.build_markdown(paras, lines, lo, doc_summary))

    print(f"\n输出：{base}.md / .raw.json / .verify.json / "
          f".chunks.jsonl（{len(chunks)} 个检索单元）")
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""噪声底线测量：同一提示词、同一文件、同一参数，连跑 N 次，量化重跑方差。

没有这条基线，任何"A 提示词比 B 好"的说法都无法与采样噪声区分。
顺便产出边界投票结果（出现在多数次运行中的边界 = 真实结构信号）。

用法：python noise.py <txt> [--runs 5] [--variant mine] [--workers 5]
"""
from __future__ import annotations

import argparse
import collections
import io
import itertools
import json
import math
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import compare as cmp
import ds_client as ds


def one_run(idx: int, system: str, user: str, path: str, max_tokens: int,
            temperature: float = 0.2) -> dict:
    content, meta = ds.chat(system, user, max_tokens=max_tokens,
                            temperature=temperature,
                            ctx={"file": os.path.basename(path), "run": idx,
                                 "tag": "noise"})
    if content is None:
        return {"run": idx, "fatal": meta.get("error"), "meta": meta}
    data = ds.parse_json(content)
    if data is None:
        ds.log_failure("json_parse_failed", content,
                       {"file": os.path.basename(path), "run": idx})
        return {"run": idx, "fatal": "JSON 解析失败", "meta": meta}
    return {"run": idx, "meta": meta, "data": data, "paras": ds.flatten(data),
            "content": content}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 1.0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--variant", choices=["user", "mine"], default="mine")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--tag", default="noise")
    a = ap.parse_args()

    lines = ds.read_lines(a.path)
    lo, hi = 1, len(lines)
    numbered = "\n".join(f"{i} | {ln}" for i, ln in enumerate(lines, 1))
    label, system = cmp.VARIANTS[a.variant]
    user = cmp.USER_TMPL.format(n=len(lines), lo=lo, hi=hi, numbered=numbered)

    print(f"文件 {os.path.basename(a.path)}  {len(lines)} 行 / {len(''.join(lines))} 字")
    print(f"提示词变体 [{a.variant}] {label}，temperature={a.temperature}，连跑 {a.runs} 次"
          f"（并发 {a.workers}）\n")

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(
            lambda i: one_run(i, system, user, a.path, a.max_tokens, a.temperature),
            range(1, a.runs + 1)))
    results.sort(key=lambda r: r["run"])

    ok = [r for r in results if "fatal" not in r]
    for r in results:
        if "fatal" in r:
            print(f"  run{r['run']}  【失败】{r['fatal']}（已记录，不重试）")
    if len(ok) < 2:
        print("\n有效样本不足 2 个，无法计算方差。")
        return 2

    # ---- 逐次指标
    print(f"{'run':<5}{'章':>4}{'节':>5}{'段':>5}{'覆盖':>8}{'逐字':>6}"
          f"{'章2起始行':>10}{'耗时s':>8}{'出tok':>8}")
    rows = []
    for r in ok:
        P = r["paras"]
        v = ds.verify(P, lo, hi, lines)
        ch = len({p["chapter"] for p in P})
        sec = len({(p["chapter"], p["section"]) for p in P})
        seam = next((p["start"] for p in P if p["chapter"] == 2), None)
        r["bounds"] = {p["start"] for p in P} - {lo}
        r["stats"] = {"chapters": ch, "sections": sec, "paragraphs": len(P),
                      "coverage": v["coverage_rate"], "identical": v["text_identical"],
                      "seam": seam, "issues": v["issues"]}
        rows.append(r["stats"])
        print(f"{r['run']:<5}{ch:>4}{sec:>5}{len(P):>5}{v['coverage_rate']:>8.1%}"
              f"{('是' if v['text_identical'] else '否'):>6}"
              f"{(seam if seam else '—'):>10}"
              f"{r['meta']['secs']:>8}{r['meta']['usage'].get('completion_tokens'):>8}")

    def rng(key):
        vals = [s[key] for s in rows if s[key] is not None]
        if not vals:
            return "—"
        return (f"{min(vals)}~{max(vals)}（均值 {statistics.mean(vals):.1f}"
                f"，极差 {max(vals) - min(vals)}）")

    print(f"\n--- 波动范围（n={len(ok)}）---")
    for key, name in (("chapters", "章数"), ("sections", "节数"),
                      ("paragraphs", "段数"), ("seam", "章2起始行")):
        print(f"  {name:<10}{rng(key)}")
    print(f"  行覆盖/逐字一致  "
          f"{'全部通过' if all(s['coverage'] == 1.0 and s['identical'] for s in rows) else '有失败'}")

    # ---- 两两边界重合度
    print(f"\n--- 两两边界 Jaccard 重合度 ---")
    js = []
    for x, y in itertools.combinations(ok, 2):
        j = jaccard(x["bounds"], y["bounds"])
        js.append(j)
        print(f"  run{x['run']} vs run{y['run']}: {j:.1%}"
              f"  （共同 {len(x['bounds'] & y['bounds'])} 个）")
    print(f"  平均重合度 {statistics.mean(js):.1%}  "
          f"最低 {min(js):.1%}  最高 {max(js):.1%}")

    # ---- 边界投票
    cnt = collections.Counter()
    for r in ok:
        cnt.update(r["bounds"])
    n = len(ok)
    hist = collections.Counter(cnt.values())
    print(f"\n--- 边界投票（{n} 次运行共出现 {len(cnt)} 个不同边界）---")
    for k in range(n, 0, -1):
        c = hist.get(k, 0)
        bar = "#" * min(c, 50)
        print(f"  被 {k}/{n} 次选中: {c:>3} 个  {bar}")

    thresh = math.ceil(n / 2)
    consensus = sorted([b for b, c in cnt.items() if c >= thresh])
    print(f"\n共识边界（≥{thresh}/{n} 票）：{len(consensus)} 个")

    # 用共识边界重建分段并校验
    starts = [lo] + consensus
    paras = []
    for i, s in enumerate(starts):
        e = starts[i + 1] - 1 if i + 1 < len(starts) else hi
        paras.append({"chapter": 1, "chapter_title": "", "chapter_summary": "",
                      "section": 1, "section_title": "", "section_summary": "",
                      "start": s, "end": e, "summary": "", "keywords": []})
    v = ds.verify(paras, lo, hi, lines)
    L = [p["end"] - p["start"] + 1 for p in paras]
    print(f"  重建分段：{len(paras)} 段，段长 min={min(L)} 中位={statistics.median(L)} "
          f"max={max(L)}，>40行的墙={sum(1 for x in L if x > 40)}")
    print(f"  校验：行覆盖 {v['coverage_rate']:.1%}，逐字一致 "
          f"{'是' if v['text_identical'] else '否'}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output",
                       f"{a.tag}_{a.variant}_{os.path.splitext(os.path.basename(a.path))[0]}.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps({
        "file": os.path.basename(a.path), "variant": a.variant, "runs": len(ok),
        "per_run": rows, "pairwise_jaccard": js,
        "vote_histogram": {str(k): hist.get(k, 0) for k in range(1, n + 1)},
        "consensus_threshold": thresh, "consensus_boundaries": consensus,
        "consensus_lengths": L,
    }, ensure_ascii=False, indent=2))
    print(f"\n明细：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

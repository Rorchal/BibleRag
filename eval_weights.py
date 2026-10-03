# -*- coding: utf-8 -*-
"""用 切分结果/*/测试提问.json 当测试集，扫权重组合，看 标题/问题/正文 怎么配最准。

每句测试话只有一个正确答案（它所属的段）。指标：
  Top-1  正确段排第一的比例
  Top-3  正确段进前三的比例
  MRR    正确段名次的倒数的平均（排第一得 1，第二得 0.5……）
权重按 0.1 步长扫所有组合（标题+问题+正文=1），再比较 norm=none / zscore。
另外单独报三路各自单用、以及常用的几组。

用法：
  python eval_weights.py                       # 全部测试集，写 docs/权重评测.md
  python eval_weights.py --only GH_约伯记1章1到8节_新热词
"""
from __future__ import annotations

import argparse
import glob
import io
import itertools
import json
import os
import sys

import numpy as np

from search import Index, default_index, normalize, FIELDS

HERE = os.path.dirname(os.path.abspath(__file__))


def load_testset(only: list[str] | None):
    items = []
    for f in sorted(glob.glob(os.path.join(HERE, "切分结果", "*", "测试提问.json"))):
        name = os.path.basename(os.path.dirname(f))
        if only and name not in only:
            continue
        items += json.load(io.open(f, encoding="utf-8"))["items"]
    return items


def metrics(final: np.ndarray, gold: np.ndarray) -> dict:
    """final: [查询数, 段数] 分数；gold: [查询数] 正确段下标。"""
    gold_score = final[np.arange(len(gold)), gold][:, None]
    rank = (final > gold_score).sum(1) + 1
    return {"top1": float((rank == 1).mean()), "top3": float((rank <= 3).mean()),
            "top5": float((rank <= 5).mean()), "mrr": float((1.0 / rank).mean())}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=None)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "docs", "权重评测.md"))
    a = ap.parse_args()

    idx = Index(a.index or default_index())
    id2i = {p["id"]: i for i, p in enumerate(idx.paras)}
    items = [t for t in load_testset(a.only) if t["id"] in id2i]
    if not items:
        raise SystemExit("没有测试集，先运行 gen_testset.py")
    gold = np.array([id2i[t["id"]] for t in items])
    print(f"测试集 {len(items)} 句，索引 {len(idx.paras)} 段，模型 {idx.model_name}", flush=True)

    qv = idx.encode([t["query"] for t in items])                    # [Q, D]
    raw = {"title": qv @ idx.title.T, "text": qv @ idx.text.T}      # [Q, P]
    per_q = qv @ idx.question.T                                      # [Q, 问题数]
    s_q = np.full((len(items), len(idx.paras)), -1.0, dtype=np.float32)
    for j, o in enumerate(idx.q_owner):
        s_q[:, o] = np.maximum(s_q[:, o], per_q[:, j])
    s_q[s_q == -1.0] = 0.0
    raw["question"] = s_q

    def scored(norm: str):
        if norm == "none":
            return raw
        return {k: np.stack([normalize(row, norm) for row in v]) for k, v in raw.items()}

    rows = []
    for norm in ("none", "zscore"):
        s = scored(norm)
        for wt, wq in itertools.product(range(0, 11), repeat=2):
            wx = 10 - wt - wq
            if wx < 0:
                continue
            w = {"title": wt / 10, "question": wq / 10, "text": wx / 10}
            final = sum(w[k] * s[k] for k in FIELDS)
            rows.append({"norm": norm, **w, **metrics(final, gold)})
    rows.sort(key=lambda r: (-r["mrr"], -r["top1"]))

    def fmt(r):
        return (f"| {r['norm']} | {r['title']:.1f} / {r['question']:.1f} / {r['text']:.1f} | "
                f"{r['top1']*100:.1f}% | {r['top3']*100:.1f}% | {r['top5']*100:.1f}% | {r['mrr']:.3f} |")

    head = "| 归一化 | 标题 / 问题 / 正文 | Top-1 | Top-3 | Top-5 | MRR |\n|---|---|---|---|---|---|"
    pick = lambda n, t, q, x: next(r for r in rows if r["norm"] == n and abs(r["title"]-t) < 1e-6
                                    and abs(r["question"]-q) < 1e-6 and abs(r["text"]-x) < 1e-6)
    singles = [pick("none", 1, 0, 0), pick("none", 0, 1, 0), pick("none", 0, 0, 1)]
    common = [pick(n, *w) for n in ("none", "zscore") for w in ((0.4, 0.4, 0.2), (0.3, 0.5, 0.2), (0.5, 0.3, 0.2), (0.3, 0.3, 0.4), (0.5, 0.5, 0.0))]
    by_sermon = {}
    for t in items:
        by_sermon.setdefault(t["id"].split("#")[0], 0)
        by_sermon[t["id"].split("#")[0]] += 1

    md = [f"# 权重评测：标题 / 问题 / 正文 怎么配", "",
          f"- 测试集 {len(items)} 句（" + "，".join(f"{k} {v}" for k, v in by_sermon.items()) + "），每句对应 1 个正确段",
          f"- 索引 {len(idx.paras)} 段 / {len(idx.question)} 条问题，模型 {idx.model_name}",
          "- 测试话由 DeepSeek 按「几周后弟兄姊妹随手搜索」的口吻另写（prompts/测试提问_v1.md），不进索引",
          "", "## 三路单独用", "", head, *map(fmt, singles),
          "", "## 常用组合", "", head, *map(fmt, common),
          "", "## 全部组合里最好的 15 组", "", head, *map(fmt, rows[:15]),
          "", "## 最差的 5 组（看看哪一路拖后腿）", "", head, *map(fmt, rows[-5:]), ""]
    io.open(a.out, "w", encoding="utf-8").write("\n".join(md))
    print("\n".join(md[5:]))
    print(f"\n→ {os.path.relpath(a.out, HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

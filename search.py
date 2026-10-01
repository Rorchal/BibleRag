# -*- coding: utf-8 -*-
"""按「标题 / 问题 / 正文」三路相似度加权重排的段落检索。

每一段有三路分数（都是余弦相似度）：
  s_title    查询 vs 段标题
  s_question 查询 vs 这段的每条问题，取最大值（一段有 1~2 条问题，哪条像就算哪条）
  s_text     查询 vs 段正文
最终分 = w_title*s_title + w_question*s_question + w_text*s_text，按最终分排序。
默认权重 0.4 / 0.4 / 0.2，用 --weights 改，三个数之和不必是 1，程序会归一化。

--norm 决定加权前要不要把三路分数拉到同一尺度：
  none    直接用余弦（默认）。注意问题短、和查询形状像，余弦普遍比标题、正文高 0.1~0.2，
          同样的权重下问题这一路实际占的比重更大。
  zscore  每一路在全部段上做 (x-均值)/标准差，再加权——权重就是真正的「比例」。
  minmax  每一路缩到 0~1 再加权。

用法：
  python search.py "三千骆驼为什么说相当于三千辆大货车"
  python search.py "撒但为什么能出现在神的众子中间" --weights 0.4,0.4,0.2 --top 5
  python search.py --queries 查询.txt --json 结果.json       # 批量：一行一个查询
  python search.py                                            # 交互模式，输入 :w 0.5,0.3,0.2 改权重
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ("title", "question", "text")


class Index:
    def __init__(self, path: str):
        self.path = path
        meta = json.load(io.open(os.path.join(path, "meta.json"), encoding="utf-8"))
        self.model_name = meta["model"]
        self.paras = meta["paragraphs"]
        self.title = np.load(os.path.join(path, "title.npy"))
        self.text = np.load(os.path.join(path, "text.npy"))
        self.question = np.load(os.path.join(path, "question.npy"))
        self.q_owner = np.load(os.path.join(path, "q_owner.npy"))
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    def encode(self, queries: list[str]) -> np.ndarray:
        return self.model.encode(queries, normalize_embeddings=True, convert_to_numpy=True,
                                 show_progress_bar=False).astype(np.float32)

    def scores(self, qv: np.ndarray) -> dict[str, np.ndarray]:
        """一个查询向量 → 三路分数，各 [段数]。"""
        s_title = self.title @ qv
        s_text = self.text @ qv
        s_q = np.full(len(self.paras), -1.0, dtype=np.float32)
        if len(self.question):
            per_q = self.question @ qv
            np.maximum.at(s_q, self.q_owner, per_q)       # 每段取它问题里的最大值
            s_q[s_q == -1.0] = 0.0                         # 没有问题的段记 0
            self._last_best_q = {}
            for i, o in enumerate(self.q_owner):
                if per_q[i] >= s_q[o] - 1e-6:
                    self._last_best_q[int(o)] = i
        return {"title": s_title, "question": s_q, "text": s_text}

    def search(self, query: str, weights: dict[str, float], top: int = 10, norm: str = "none") -> list[dict]:
        qv = self.encode([query])[0]
        s = self.scores(qv)
        wsum = sum(weights.values()) or 1.0
        w = {k: weights.get(k, 0.0) / wsum for k in FIELDS}
        final = sum(w[k] * normalize(s[k], norm) for k in FIELDS)
        ranks = {k: np.argsort(-s[k]).argsort() + 1 for k in FIELDS}   # 单路名次，便于看谁拉了谁
        order = np.argsort(-final)[:top]
        out = []
        for rank, i in enumerate(order, 1):
            p = self.paras[i]
            bq = self._last_best_q.get(int(i))
            out.append({"rank": rank, "score": round(float(final[i]), 4),
                        **{f"s_{k}": round(float(s[k][i]), 4) for k in FIELDS},
                        **{f"rank_{k}": int(ranks[k][i]) for k in FIELDS},
                        "id": p["id"], "sermon": p["sermon"], "section": p["section"],
                        "lines": f"{p['start']}-{p['end']}", "kind": p["kind"],
                        "title": p["title"],
                        "best_question": (self.paras[self.q_owner[bq]]["questions"]
                                          [bq - int(np.searchsorted(self.q_owner, self.q_owner[bq]))]
                                          if bq is not None else None),
                        "text": p["text"]})
        return out


def normalize(x: np.ndarray, how: str) -> np.ndarray:
    if how == "zscore":
        sd = x.std()
        return (x - x.mean()) / sd if sd > 1e-9 else x * 0
    if how == "minmax":
        lo, hi = x.min(), x.max()
        return (x - lo) / (hi - lo) if hi - lo > 1e-9 else x * 0
    return x


def parse_weights(s: str) -> dict[str, float]:
    parts = [float(x) for x in re.split(r"[,/\s]+", s.strip()) if x]
    if len(parts) != 3:
        raise SystemExit("权重要三个数：标题,问题,正文，如 0.4,0.4,0.2")
    return dict(zip(FIELDS, parts))


def default_index() -> str:
    cands = sorted(glob.glob(os.path.join(HERE, "index", "*", "meta.json")), key=os.path.getmtime)
    if not cands:
        raise SystemExit("没有索引，先运行 python embed_index.py")
    return os.path.dirname(cands[-1])


def show(query: str, hits: list[dict], weights: dict[str, float], text_chars: int = 80) -> None:
    w = "/".join(f"{weights[k]:g}" for k in FIELDS)
    print(f"\n查询：{query}    权重 标题/问题/正文 = {w}")
    for h in hits:
        print(f"{h['rank']:>2}. {h['score']:.3f}  [标题 {h['s_title']:.3f} #{h['rank_title']:<3} "
              f"问题 {h['s_question']:.3f} #{h['rank_question']:<3} 正文 {h['s_text']:.3f} #{h['rank_text']:<3}]  "
              f"{h['sermon']} {h['section']} 行{h['lines']}")
        print(f"      标题：{h['title']}")
        if h.get("best_question"):
            print(f"      最像的问题：{h['best_question']}")
        if text_chars:
            print(f"      正文：{h['text'][:text_chars]}{'…' if len(h['text']) > text_chars else ''}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?")
    ap.add_argument("--index", default=None, help="index/<模型名>/，默认最新的一个")
    ap.add_argument("--weights", default="0.4,0.4,0.2", help="标题,问题,正文 的权重")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--norm", default="none", choices=["none", "zscore", "minmax"], help="加权前各路分数的归一化")
    ap.add_argument("--queries", default=None, help="批量：一行一个查询的文本文件")
    ap.add_argument("--json", default=None, help="把结果写成 json")
    ap.add_argument("--text-chars", type=int, default=80, help="打印正文前几个字，0 不打印")
    a = ap.parse_args()

    idx = Index(a.index or default_index())
    weights = parse_weights(a.weights)
    print(f"索引 {os.path.relpath(idx.path, HERE)}：{len(idx.paras)} 段，{len(idx.question)} 条问题，模型 {idx.model_name}")

    if a.queries:
        qs = [l.strip() for l in io.open(a.queries, encoding="utf-8") if l.strip() and not l.startswith("#")]
    elif a.query:
        qs = [a.query]
    else:
        qs = None

    if qs is not None:
        results = []
        for q in qs:
            hits = idx.search(q, weights, a.top, a.norm)
            show(q, hits, weights, a.text_chars)
            results.append({"query": q, "weights": weights, "norm": a.norm, "hits": hits})
        if a.json:
            io.open(a.json, "w", encoding="utf-8").write(json.dumps(results, ensure_ascii=False, indent=1))
            print(f"\n→ {a.json}")
        return 0

    print("交互模式：输入问题回车；:w 0.5,0.3,0.2 改权重；:norm zscore 改归一化；:top 8 改条数；:q 退出")
    top = a.top
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q in (":q", "exit", "quit"):
            break
        if q.startswith(":w"):
            weights = parse_weights(q[2:])
            print("权重 →", weights)
            continue
        if q.startswith(":top"):
            top = int(q[4:])
            continue
        if q.startswith(":norm"):
            a.norm = q[5:].strip() or "none"
            continue
        show(q, idx.search(q, weights, top, a.norm), weights, a.text_chars)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""把每一段的 标题 / 正文 / 问题 分别向量化，存成本地索引，供 search.py 按权重组合检索。

输入：切分结果/<讲道>/问题.json（结构 + 每段 kind、questions）和同目录 校对后原文.txt
输出：index/<模型名>/
  meta.json        每段一条：id、讲道、章节、标题、正文、问题、kind
  title.npy        段标题向量      [段数, 维度]
  text.npy         段正文向量      [段数, 维度]
  question.npy     问题向量        [问题数, 维度]
  q_owner.npy      每条问题属于第几段
向量都做了 L2 归一化，点积即余弦相似度。

模型：默认 BAAI/bge-m3（中英都行、支持长文本、查询和文档不用加前缀）。
      首次运行会从 HuggingFace 下载约 2.2G；可用 --model 换成 BAAI/bge-large-zh-v1.5 等。

用法：
  python embed_index.py                      # 全部 切分结果/*/问题.json
  python embed_index.py --only GH_约伯记1章1到8节_新热词 GH_约伯记2章1到6节
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "切分结果")


def model_tag(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.split("/")[-1])


def load_paragraphs(only: list[str] | None = None) -> list[dict]:
    paras = []
    for f in sorted(glob.glob(os.path.join(RESULTS, "*", "问题.json"))):
        name = os.path.basename(os.path.dirname(f))
        if only and name not in only:
            continue
        d = json.load(io.open(f, encoding="utf-8"))
        txt = os.path.join(os.path.dirname(f), "校对后原文.txt")
        if not os.path.exists(txt):
            txt = os.path.join(HERE, d["source"]["原文"])
        lines = io.open(txt, encoding="utf-8-sig").read().splitlines()
        sermon = re.sub(r"^GH_", "", re.sub(r"_[^_]*$", "", name) if "_" in name[3:] else name)
        for c in d["chapters"]:
            for s in c["sections"]:
                for p in s["paragraphs"]:
                    paras.append({
                        "id": f"{name}#L{p['start']}-{p['end']}",
                        "sermon": sermon, "dir": name,
                        "chapter": c["no"], "chapter_title": c.get("title", ""),
                        "section": s["no"], "section_title": s.get("title", ""),
                        "start": p["start"], "end": p["end"],
                        "kind": p.get("kind"),
                        "title": p.get("title", ""),
                        "text": "".join(lines[p["start"] - 1:p["end"]]),
                        "questions": [q for q in p.get("questions", []) if q.strip()],
                    })
    return paras


def encode(model, texts: list[str], label: str, batch: int = 16) -> np.ndarray:
    t0 = time.time()
    v = model.encode(texts, batch_size=batch, normalize_embeddings=True, show_progress_bar=False,
                     convert_to_numpy=True)
    print(f"  {label}: {len(texts)} 条 → {v.shape}  {time.time() - t0:.0f}s", flush=True)
    return v.astype(np.float32)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-m3")
    ap.add_argument("--only", nargs="*", default=None, help="只索引这些 切分结果/ 目录")
    ap.add_argument("--out", default=None, help="默认 index/<模型名>/")
    ap.add_argument("--batch", type=int, default=16)
    a = ap.parse_args()

    paras = load_paragraphs(a.only)
    if not paras:
        raise SystemExit("没有找到 切分结果/*/问题.json")
    questions = [q for p in paras for q in p["questions"]]
    q_owner = np.array([i for i, p in enumerate(paras) for _ in p["questions"]], dtype=np.int32)
    print(f"{len(paras)} 段（{len({p['dir'] for p in paras})} 篇），{len(questions)} 条问题；模型 {a.model}", flush=True)

    from sentence_transformers import SentenceTransformer
    t0 = time.time()
    model = SentenceTransformer(a.model, device="cpu")
    print(f"模型加载 {time.time() - t0:.0f}s", flush=True)

    out = a.out or os.path.join(HERE, "index", model_tag(a.model))
    os.makedirs(out, exist_ok=True)
    np.save(os.path.join(out, "title.npy"), encode(model, [p["title"] for p in paras], "标题", a.batch))
    np.save(os.path.join(out, "text.npy"), encode(model, [p["text"] for p in paras], "正文", a.batch))
    np.save(os.path.join(out, "question.npy"), encode(model, questions, "问题", a.batch))
    np.save(os.path.join(out, "q_owner.npy"), q_owner)
    io.open(os.path.join(out, "meta.json"), "w", encoding="utf-8").write(json.dumps(
        {"model": a.model, "built": time.strftime("%Y-%m-%d %H:%M:%S"), "paragraphs": paras},
        ensure_ascii=False, indent=1))
    print(f"→ {os.path.relpath(out, HERE)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

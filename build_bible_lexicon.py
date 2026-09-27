# -*- coding: utf-8 -*-
"""从和合本全书找「和合本常用词」候选，供人工筛选进热词表。

步骤（每一步的数字都写进候选表，方便复查）：
  1. 新词发现：和合本按标点切句后取 2~4 字片段，出现 ≥10 次，
     内部凝固度（最小切分点 PMI）≥2.5、左右邻字熵 ≥1.0 —— 保证是个「词」，不是跨词碎片
  2. 圣经特有：圣经内频率 / 现代汉语频率（jieba 词频）≥30；拆开全是极高频词的（「有一个」）不算
  3. 首尾不是虚字（「的意思」「把它」），词典里的词照收
  4. 讲道证据：31 篇识别稿里原样出现 ≥3 次
  5. 同音防误伤：同音的常用现代词（jieba 词频 ≥2000）在识别稿里出现次数不少于它本身的（「奇事/其实」），不收

输出：
  lexicon/和合本.txt              全书原文，一节一行「书卷<TAB>章:节<TAB>经文」（来源 api.getbible.net 的 cus）
  lexicon/和合本常用词_候选.tsv   通过 1~5 的候选及统计；热词表「和合本常用词」分组从这里人工筛选

用法：
  python build_bible_lexicon.py --fetch   # 首次下载全书
  python build_bible_lexicon.py
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import math
import os
import re
import urllib.request

import jieba
from pypinyin import lazy_pinyin

import typo_fix

HERE = os.path.dirname(os.path.abspath(__file__))
BIBLE_TXT = os.path.join(HERE, "lexicon", "和合本.txt")
OUT = os.path.join(HERE, "lexicon", "和合本常用词_候选.tsv")
SRC_DIR = os.path.join(HERE, "识别结果", "豆包2.0")
URL = "https://api.getbible.net/v2/cus.json"
SPLIT = re.compile(r"[，。；：！？「」『』、,.;:!?　\s・（）()]+")
EDGE_HEAD = set("的所在是把着了就也都从被给又却还再必岂没这那哪一个们之中上下里来去说我你他她它得能要会可将与和及或而且若如")
EDGE_TAIL = set("的了着们个是说在里上下中去来到过吗呢吧啊一之所把被给就也都还再又不得地些样么")

jieba.setLogLevel(60)
jieba.initialize()
FREQ, JT = jieba.dt.FREQ, jieba.dt.total


def fetch() -> None:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (BibleRag lexicon builder)"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode("utf-8"))
    names = typo_fix.load_lexicon()["66卷书名"]
    rows = []
    for b in d["books"]:
        name = names[int(b["nr"]) - 1]
        for c in b["chapters"]:
            for v in c["verses"]:
                rows.append(f"{name}\t{v['chapter']}:{v['verse']}\t{v['text'].replace('　', '').replace(chr(0xfeff), '').strip()}")
    io.open(BIBLE_TXT, "w", encoding="utf-8").write(
        "# 和合本（简体）全书，来源 " + URL + "；一节一行「书卷\t章:节\t经文」，「神」前的全角空格已去掉\n"
        + "\n".join(rows) + "\n")
    print(f"{len(rows)} 节 → {os.path.relpath(BIBLE_TXT, HERE)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    a = ap.parse_args()
    if a.fetch or not os.path.exists(BIBLE_TXT):
        fetch()

    clauses = []
    for ln in io.open(BIBLE_TXT, encoding="utf-8"):
        if ln.startswith("#"):
            continue
        text = ln.rstrip("\n").split("\t")[2].replace("甚么", "什么")
        clauses += [x for x in SPLIT.split(text) if x]

    cnt, left, right = collections.Counter(), collections.defaultdict(collections.Counter), \
        collections.defaultdict(collections.Counter)
    for cl in clauses:
        s = "^" + cl + "$"
        for n in range(1, 5):
            for i in range(1, len(s) - n):
                g = s[i:i + n]
                cnt[g] += 1
                if n >= 2:
                    left[g][s[i - 1]] += 1
                    right[g][s[i + n]] += 1
    n1 = sum(v for k, v in cnt.items() if len(k) == 1)
    total = sum(map(len, clauses))

    def pmi(g):
        p = lambda x: cnt[x] / n1
        return min(math.log(p(g) / (p(g[:k]) * p(g[k:]))) for k in range(1, len(g)))

    def ent(c):
        t = sum(c.values())
        return -sum(v / t * math.log(v / t) for v in c.values())

    def common(g):
        if FREQ.get(g, 0) >= 2000:
            return True
        toks = jieba.lcut(g, HMM=False)
        return g not in FREQ and len(toks) > 1 and all(FREQ.get(t, 0) >= 20000 for t in toks)

    ratio = lambda g: (cnt[g] / total) / ((FREQ.get(g, 0) + 1) / JT)
    edge_ok = lambda g: g in FREQ or (g[0] not in EDGE_HEAD and g[-1] not in EDGE_TAIL)
    words = {g: c for g, c in cnt.items()
             if 2 <= len(g) <= 4 and c >= 10 and re.fullmatch(r"[一-鿿]+", g)
             and pmi(g) >= 2.5 and min(ent(left[g]), ent(right[g])) >= 1.0
             and ratio(g) >= 30 and not common(g) and edge_ok(g)}

    by = collections.defaultdict(list)
    for g in words:
        by[(len(g), " ".join(lazy_pinyin(g)))].append(g)
    ex, var = collections.Counter(), collections.defaultdict(collections.Counter)
    for f in sorted(os.listdir(SRC_DIR)):
        if not (f.startswith("GH_") and f.endswith("_热词识别.txt")):
            continue
        for line in io.open(os.path.join(SRC_DIR, f), encoding="utf-8-sig"):
            line = line.strip()
            if not line:
                continue
            py = lazy_pinyin(line)
            if len(py) != len(line):
                py = [(lazy_pinyin(ch) or [ch])[0] for ch in line]
            for n in range(2, 5):
                for i in range(len(line) - n + 1):
                    k = (n, " ".join(py[i:i + n]))
                    for g in by.get(k, ()):
                        if line[i:i + n] == g:
                            ex[g] += 1
                        else:
                            var[g][line[i:i + n]] += 1

    rows = []
    for g, c in words.items():
        if ex[g] < 3:
            continue
        risky = [s for s, k in var[g].items() if FREQ.get(s, 0) >= 2000 and k >= max(3, ex[g])]
        rows.append((g, c, round(ratio(g)), round(pmi(g), 1), round(min(ent(left[g]), ent(right[g])), 2),
                     ex[g], sum(var[g].values()), "、".join(f"{w}×{k}" for w, k in var[g].most_common(3)),
                     "排除：同音常用词 " + "/".join(risky) if risky else ""))
    rows.sort(key=lambda r: (r[8] != "", -(r[5] + r[6])))
    io.open(OUT, "w", encoding="utf-8").write(
        "# 词\t圣经次数\t圣经/现代频率比\t凝固度\t邻字熵\t识别稿原样\t同音误识\t误识写法\t备注\n"
        + "\n".join("\t".join(map(str, r)) for r in rows) + "\n")
    ok = sum(1 for r in rows if not r[8])
    print(f"候选 {ok} 个（另有 {len(rows) - ok} 个因同音常用词排除）→ {os.path.relpath(OUT, HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""从和合本约伯记原文生成热词：人名地名 + 经文短语，并统计讲道识别稿里的引用与同音误识。

输入：
  lexicon/和合本_约伯记.txt   原文，一节一行「章:节<TAB>经文」（来源 api.getbible.net 的 cus，和合本简体）
  识别结果/豆包2.0/GH_*_热词识别.txt

输出：
  lexicon/约伯记短语.tsv      每个短语一行：章:节、短语、原样出现次数、同音误识次数、误识写法
                               （同音 = 去声调拼音相同但字不同，正是热词能纠正的那一类）
  lexicon/热词表.txt          重写其中两组：「约伯记人名地名」「约伯记短语（同音易错）」

「同音易错」的口径（排除不是识别错的情况）：
  - 只是和合本旧写法 / 异体（甚么/什么、惟/唯、他/她/它、须要/需要……，见 VARIANTS）不算
  - 太日常的短句不收：少于 4 字，或识别稿里原样出现 ≥30 次（识别器本来就不会错）
  - 口吃叠字（「审审判人」）不算，见 NOT_ASR

短语 = 经文按标点切出的分句，3~8 字。2 字分句（「他说」「现在」）太泛，不收。

用法：
  python build_job_lexicon.py --fetch      # 首次：下载原文存到 lexicon/和合本_约伯记.txt
  python build_job_lexicon.py              # 统计并写 约伯记短语.tsv
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import os
import re
import urllib.request

from pypinyin import lazy_pinyin

HERE = os.path.dirname(os.path.abspath(__file__))
JOB_TXT = os.path.join(HERE, "lexicon", "和合本_约伯记.txt")
PHRASES = os.path.join(HERE, "lexicon", "约伯记短语.tsv")
SRC_DIR = os.path.join(HERE, "识别结果", "豆包2.0")
URL = "https://api.getbible.net/v2/cus/18.json"
SPLIT = re.compile(r"[，。；：！？「」『』、,.;:!?　\s]+")
MIN_LEN, MAX_LEN = 3, 8

# 约伯记里出现的人名、族名、地名、星宿名（原文里都核对过出现）
NAMES = ["约伯", "乌斯", "撒但", "耶和华", "示巴人", "迦勒底人", "以利法", "提幔人", "比勒达", "书亚人",
         "琐法", "拿玛人", "以利户", "巴拉迦", "布西人", "兰族", "耶米玛", "基洗亚", "基连哈朴", "俄斐",
         "古实", "提玛", "示巴", "北斗", "参星", "昴星", "鳄鱼", "河马", "拉哈伯"]


# 和合本旧写法 / 异体 / 代词：两边归一后相同的，不算识别错
VARIANTS = [("甚么", "什么"), ("惟", "唯"), ("忿", "愤"), ("帐棚", "帐篷"), ("作声", "做声"), ("记念", "纪念"),
            ("她", "他"), ("它", "他"), ("撒旦", "撒但"), ("须要", "需要"), ("决不", "绝不"), ("倚靠", "依靠"),
            ("嬉笑", "喜笑"), ("硷", "碱"), ("哪", "呐"), ("吗", "嘛"), ("蛋青", "蛋清")]
NOT_ASR = {"审审判人"}          # 讲道人口吃，不是识别错


def norm_variant(s: str) -> str:
    for a, b in VARIANTS:
        s = s.replace(a, b)
    return s


def set_group(path: str, group: str, words: list[str], note: str) -> None:
    """把热词表里某个分组的内容整组替换（分组不存在就插到「## 66卷书名」前面）。"""
    lines = io.open(path, encoding="utf-8").read().split("\n")
    head = f"## {group}"
    body = [f"# {note}"] + words + [""]
    if head in lines:
        i = lines.index(head)
        j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
        lines[i + 1:j] = body
    else:
        k = lines.index("## 66卷书名")
        lines[k:k] = [head] + body
    io.open(path, "w", encoding="utf-8").write("\n".join(lines))


def fetch() -> None:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (BibleRag lexicon builder)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8"))
    rows = []
    for c in d["chapters"]:
        for v in c["verses"]:
            rows.append(f"{v['chapter']}:{v['verse']}\t{v['text'].replace('　', '').strip()}")
    io.open(JOB_TXT, "w", encoding="utf-8").write(
        "# 和合本（简体）约伯记，来源 " + URL + "，一节一行；「神」前的全角空格已去掉\n" + "\n".join(rows) + "\n")
    print(f"{len(rows)} 节 → {os.path.relpath(JOB_TXT, HERE)}")


def load_job() -> list[tuple[str, str]]:
    out = []
    for ln in io.open(JOB_TXT, encoding="utf-8"):
        if ln.startswith("#") or not ln.strip():
            continue
        ref, text = ln.rstrip("\n").split("\t")
        out.append((ref, text))
    return out


def py(s: str) -> str:
    return " ".join(lazy_pinyin(s))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    a = ap.parse_args()
    if a.fetch or not os.path.exists(JOB_TXT):
        fetch()

    job = load_job()
    alltext = "".join(t for _, t in job).replace("・", "")
    missing = [n for n in NAMES if n not in alltext]
    if missing:
        print("注意：这些名字原文里没有，不收：", "、".join(missing))

    # 短语：首次出现的章节
    first = {}
    for ref, text in job:
        for c in SPLIT.split(text):
            if MIN_LEN <= len(c) <= MAX_LEN and c not in first:
                first[c] = ref
    by_py = collections.defaultdict(list)
    for c in first:
        by_py[(len(c), py(c))].append(c)

    # 扫识别稿：原样出现 / 同音不同字
    exact = collections.Counter()
    variant = collections.defaultdict(collections.Counter)
    files = sorted(f for f in os.listdir(SRC_DIR) if f.startswith("GH_") and f.endswith("_热词识别.txt"))
    for f in files:
        for line in io.open(os.path.join(SRC_DIR, f), encoding="utf-8-sig"):
            line = line.strip()
            if not line:
                continue
            pys = lazy_pinyin(line)
            if len(pys) != len(line):      # 极少数字符拼音拆分不一致，逐字重算
                pys = [lazy_pinyin(ch)[0] if lazy_pinyin(ch) else ch for ch in line]
            for n in range(MIN_LEN, MAX_LEN + 1):
                for i in range(len(line) - n + 1):
                    key = (n, " ".join(pys[i:i + n]))
                    if key not in by_py:
                        continue
                    seg = line[i:i + n]
                    for c in by_py[key]:
                        if seg == c:
                            exact[c] += 1
                        else:
                            variant[c][seg] += 1

    rows = []
    for c, ref in first.items():
        v = variant.get(c, collections.Counter())
        rows.append((ref, c, exact[c], sum(v.values()), "、".join(f"{w}×{k}" for w, k in v.most_common(5))))
    io.open(PHRASES, "w", encoding="utf-8").write(
        "# 约伯记短语：章:节\t短语\t识别稿原样出现\t同音误识\t误识写法（前5）\n"
        + "\n".join("\t".join(map(str, r)) for r in rows) + "\n")
    # 同音易错：真正的识别错才收
    easy = []
    for ref, c, ex, nv, _ in rows:
        if nv == 0 or len(c) < 4 or ex >= 30:
            continue
        if any(norm_variant(w) != norm_variant(c) and w not in NOT_ASR for w in variant[c]):
            easy.append(c)
    lex = os.path.join(HERE, "lexicon", "热词表.txt")
    names = [n for n in NAMES if n in alltext]
    set_group(lex, "约伯记人名地名", names, "由 build_job_lexicon.py 生成：和合本约伯记原文里出现的人名、族名、地名、星宿名")
    set_group(lex, "约伯记短语（同音易错）", easy,
              "由 build_job_lexicon.py 生成：约伯记原文短语里，讲道识别稿出现过同音误识的（不含旧写法异体、日常短句）")
    print(f"热词表：约伯记人名地名 {len(names)} 个，约伯记短语（同音易错）{len(easy)} 个")

    quoted = [r for r in rows if r[2] + r[3] > 0]
    misheard = [r for r in rows if r[3] > 0]
    print(f"约伯记 {len(job)} 节，短语（{MIN_LEN}~{MAX_LEN} 字）{len(rows)} 个；"
          f"讲道里说过的 {len(quoted)} 个，其中有同音误识的 {len(misheard)} 个 → {os.path.relpath(PHRASES, HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""把一次切分的结果导出成可读的成品，放进 切分结果/<讲道名>/。

  切分效果.md  章 → 节 → 段，每段带行号、标题和原文
  结构.json    同样的层级，只有行号和标题（下游入库用）

输入：原文 txt、切节结果（含 chapters/sections 的 parsed.json）、切段结果（seg_paras.py 的 parsed.json）。

用法：
  python export_result.py --txt input/GH_伯1章1到8节_校对.txt \
      --sections input/c2low4_se_v2_chapter_GH_伯1章1到8节.parsed.json \
      --paras output/paras_v4_p1 --name GH_伯1章1到8节
"""
from __future__ import annotations

import argparse
import io
import json
import os

import seg_paras as sp

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt", required=True)
    ap.add_argument("--sections", required=True, help="切节结果 parsed.json（含 chapters、sections）")
    ap.add_argument("--paras", required=True, help="切段结果目录（seg_paras.py 输出）")
    ap.add_argument("--name", required=True, help="讲道名，作为 切分结果/ 下的文件夹名")
    a = ap.parse_args()

    lines = sp.read_lines(a.txt)
    se = json.load(io.open(a.sections, encoding="utf-8"))
    pa = json.load(io.open(os.path.join(a.paras, "parsed.json"), encoding="utf-8"))
    paras_by_sec = {s["no"]: s["paragraphs"] for s in pa["sections"]}
    meta = pa["meta"]

    chapters = []
    for c in se["chapters"]:
        secs = []
        for s in se["sections"]:
            if s["chapter"] != c["no"]:
                continue
            secs.append({"no": s["no"], "start": s["start"], "end": s["end"], "title": s["title"],
                         "paragraphs": [{"start": p["start"], "end": p["end"], "title": p.get("title", "")}
                                        for p in paras_by_sec.get(s["no"], [])]})
        chapters.append({"no": c["no"], "start": c["start"], "end": c["end"],
                         "title": c.get("title", ""), "sections": secs})

    n_sec = sum(len(c["sections"]) for c in chapters)
    n_par = sum(len(s["paragraphs"]) for c in chapters for s in c["sections"])
    missing = [s["no"] for c in chapters for s in c["sections"] if not s["paragraphs"]]
    source = {
        "原文": os.path.relpath(a.txt, HERE),
        "切节": os.path.relpath(a.sections, HERE),
        "切段": os.path.relpath(a.paras, HERE),
        "切段提示词": meta.get("prompt"), "模型": meta.get("model"), "effort": meta.get("effort"),
    }

    outdir = os.path.join(HERE, "切分结果", a.name)
    os.makedirs(outdir, exist_ok=True)
    io.open(os.path.join(outdir, "结构.json"), "w", encoding="utf-8").write(json.dumps(
        {"source": source, "lines": len(lines), "chapters": chapters}, ensure_ascii=False, indent=2))

    md = [f"# {a.name} · 章节段切分效果", "",
          f"- 原文 `{source['原文']}`，{len(lines)} 行",
          f"- {len(chapters)} 章 / {n_sec} 节 / {n_par} 段",
          f"- 切节：`{source['切节']}`",
          f"- 切段：`{source['切段']}`（{source['切段提示词']} · {source['模型']} · effort {source['effort']}）"]
    if missing:
        md.append(f"- ⚠ 以下节没有切段结果：{'、'.join(missing)}")
    md += ["", "---", ""]
    for c in chapters:
        head = f"第{c['no']}章" + (f"　{c['title']}" if c["title"] else "")
        md += [f"## {head}（行 {c['start']}–{c['end']}）", ""]
        for s in c["sections"]:
            md += [f"### {s['no']}　{s['title']}（行 {s['start']}–{s['end']}）", ""]
            for p in s["paragraphs"]:
                md += [f"**行 {p['start']}–{p['end']}**　{p['title']}", ""]
                body = "".join(lines[i - 1] for i in range(p["start"], p["end"] + 1))
                md += [f"> {body}", ""]
    io.open(os.path.join(outdir, "切分效果.md"), "w", encoding="utf-8").write("\n".join(md))
    print(f"{len(chapters)} 章 / {n_sec} 节 / {n_par} 段 → {os.path.relpath(outdir, HERE)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

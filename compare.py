# -*- coding: utf-8 -*-
"""A/B 对比两版提示词在同一篇讲道稿上的表现。

公平性处理：两个变体挂完全相同的「输出格式 + 行号硬规则」尾块，
差异只保留在切分指导与概要指导上。用户原提示词未指定输出格式，
不补这一块就无法解析和校验，比的就不是切分思想了。

用法：python compare.py <txt> [--max-tokens 16000] [--tag ab1]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys

import ds_client as ds

# ---------------------------------------------------------------- 共用尾块

SCHEMA_TAIL = """
【行号硬规则】段用 start/end 行号表示，闭区间；所有段按行号升序排列，
完整覆盖首行到末行，后一段的 start 恒等于前一段的 end + 1，不允许空洞、不允许重叠。

只输出 JSON，不要 markdown 代码块，不要任何解释文字。格式：
{"doc_summary": "全篇一句话主旨",
 "chapters": [{"title": "章标题",
   "sections": [{"title": "节标题",
     "paragraphs": [{"start": 1, "end": 6, "summary": "本段概要",
                     "keywords": ["关键词"], "cue": "断段依据"}]}]}]}
"""

# ---------------------------------------------------------------- 变体 A：用户原文

SYS_USER = """你是中文长文档的结构分析助手。输入是一篇讲道录音的语音识别稿，一句一行，每行前面有行号。

根据章 节 段 进行结构化分层。
每个段落的切分规则是根据语义切分，不是按照几行几行的硬切分。
每个段落返回摘要。
每条概要不要都以"讲员"开头。
""" + SCHEMA_TAIL

# ---------------------------------------------------------------- 变体 B：改写版

SYS_MINE = """你是中文长文档的结构分析助手。输入是一篇讲道录音的语音识别稿，一句一行，每行前面有行号。

把全文拆成【章 → 节 → 段】三级结构，每段附一条供检索使用的概要。

【段落边界】这是本任务的核心，判断标准如下：
在下列位置断段（按可靠性排序）：
  1. 引入新的经文或文献出处（约伯记X章、以弗所书、海德堡要理问答等）
  2. 讲员报幕式的过渡语："好，""第一/第二""接下来""我们来看""回到"
  3. 从解经转向应用，或从举例转回论点
  4. 论证对象切换（从谈约伯转为谈撒旦、从谈教义转为谈会众）
注意："所以""那""但是"在本文出现上百次，是讲员的口头连接词，
      单独出现时【不是】断段信号，不要据此断段。
拿不准时，宁可合并成较长的一段，不要切碎。

【段落长度】不设每段行数限制。段的长短应当由上述边界决定，
可以出现 3 行的短段，也可以出现 25 行的长段。
全篇预计产生 50~80 段——这是对总数的粗略预期，不是每段的切分依据。

【层级】全篇约 4~6 章，每章 3~5 节。章是讲道的大部分（如"属灵征战""肉体苦难"），
节是章内的论证步骤。

【概要】用途是向量检索，不是给人读的摘要。因此：
- 必须把段内用代词、口语指代的对象显式写出来（人名、地名、书卷章节、神学术语）
- 直接陈述内容，不要出现"讲员""作者""本段"这类指代说话人的词：
    反例：讲员引用海德堡要理问答27问，说明护理包括荒年
    正例：海德堡要理问答27问指出，上帝的护理包括荒年、贫穷与疾病
- 25~50 字，书面语，不保留"哈""呃""啊"
- 只陈述原文说过的内容，不引申不评价

【cue 字段】每段用一句话说明你在此处断段的依据
（例："引入以弗所书六章" / "从解经转入应用"）。不许填"长度合适"这类空话。
""" + SCHEMA_TAIL

USER_TMPL = """下面是识别稿，格式为「行号 | 内容」，共 {n} 行，行号从 {lo} 到 {hi}。

{numbered}

请输出 JSON。"""

VARIANTS = {"user": ("你的提示词", SYS_USER), "mine": ("我的改写版", SYS_MINE)}


# ---------------------------------------------------------------- 指标


def metrics(paras: list[dict], lines: list[str], lo: int, hi: int,
            raw_data: dict) -> dict:
    v = ds.verify(paras, lo, hi, lines)
    L = [p["end"] - p["start"] + 1 for p in paras]
    if not L:
        return {"fatal": "无段落"}

    mean = statistics.mean(L)
    sd = statistics.pstdev(L)
    # 变异系数：等分切割 -> 趋近 0；真按语义切 -> 明显 > 0.3
    cv = sd / mean if mean else 0.0

    sums = [p["summary"] for p in paras if p["summary"]]
    jiangyuan = sum(1 for s in sums if s.startswith("讲员"))

    # cue 敷衍度：去重后还剩多少比例
    cues = [p.get("cue", "").strip() for p in paras]
    cues_nonempty = [c for c in cues if c]
    cue_unique = len(set(cues_nonempty)) / len(cues_nonempty) if cues_nonempty else 0.0

    sq = ds.check_summaries(paras, lines, lo)
    return {
        "chapters": len({p["chapter"] for p in paras}),
        "sections": len({(p["chapter"], p["section"]) for p in paras}),
        "paragraphs": len(paras),
        "len_min": min(L), "len_med": statistics.median(L), "len_max": max(L),
        "len_mean": round(mean, 1), "len_sd": round(sd, 2), "len_cv": round(cv, 3),
        "coverage": v["coverage_rate"], "text_identical": v["text_identical"],
        "issues": v["issues"],
        "sum_avg_len": sq["summary_len_avg"],
        "sum_with": sq["with_summary"],
        "jiangyuan_rate": round(jiangyuan / len(sums), 3) if sums else 0.0,
        "kw_hit": sq["keyword_in_text_rate"],
        "cue_filled": len(cues_nonempty),
        "cue_unique_rate": round(cue_unique, 3),
        "doc_summary": str(raw_data.get("doc_summary", "")).strip(),
    }


def run(key: str, path: str, lines: list[str], max_tokens: int, tag: str) -> dict:
    label, system = VARIANTS[key]
    lo, hi = 1, len(lines)
    numbered = "\n".join(f"{i} | {ln}" for i, ln in enumerate(lines, 1))
    print(f"\n=== 变体 [{key}] {label} —— 调用中（单次，不重试）…", flush=True)

    content, meta = ds.chat(
        system, USER_TMPL.format(n=len(lines), lo=lo, hi=hi, numbered=numbered),
        max_tokens=max_tokens,
        ctx={"file": os.path.basename(path), "variant": key, "tag": tag})

    out = {"key": key, "label": label, "meta": meta}
    if content is None:
        print(f"    【失败】{meta.get('error')}（已记录，不重试）")
        out["fatal"] = meta.get("error")
        return out

    data = ds.parse_json(content)
    if data is None:
        ds.log_failure("json_parse_failed", content,
                       {"file": os.path.basename(path), "variant": key})
        print("    【失败】返回非合法 JSON（已记录，不重试）")
        out["fatal"] = "JSON 解析失败"
        out["content_head"] = content[:300]
        return out

    paras = ds.flatten(data)
    for p, raw in zip(paras, [q for ch in data.get("chapters", [])
                              for se in ch.get("sections", [])
                              for q in se.get("paragraphs", [])]):
        p["cue"] = str(raw.get("cue", "")).strip()

    out["m"] = metrics(paras, lines, lo, hi, data)
    out["paragraphs"] = paras
    out["content"] = content
    print(f"    完成 {meta['secs']}s  completion={meta['usage'].get('completion_tokens')} tok"
          f"  finish={meta.get('finish_reason')}")

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output",
                        f"{tag}_{key}_{os.path.splitext(os.path.basename(path))[0]}")
    io.open(base + ".raw.json", "w", encoding="utf-8").write(content)
    io.open(base + ".md", "w", encoding="utf-8").write(
        ds.build_markdown(paras, lines, lo, out["m"].get("doc_summary", "")))
    out["base"] = base
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--tag", default="ab")
    a = ap.parse_args()

    lines = ds.read_lines(a.path)
    print(f"对比文件：{os.path.basename(a.path)}  {len(lines)} 行 / {len(''.join(lines))} 字")
    print(f"两个变体使用完全相同的输出格式块与行号硬规则，差异仅在切分与概要指导语。")

    results = [run(k, a.path, lines, a.max_tokens, a.tag) for k in ("user", "mine")]

    rows = [
        ("调用耗时 (s)", lambda r: r["meta"].get("secs")),
        ("输出 token", lambda r: r["meta"]["usage"].get("completion_tokens")),
        ("章数", lambda r: r["m"]["chapters"]),
        ("节数", lambda r: r["m"]["sections"]),
        ("段数", lambda r: r["m"]["paragraphs"]),
        ("段长 最短/中位/最长", lambda r: f"{r['m']['len_min']}/{r['m']['len_med']}/{r['m']['len_max']}"),
        ("段长标准差", lambda r: r["m"]["len_sd"]),
        ("段长变异系数 CV", lambda r: r["m"]["len_cv"]),
        ("行覆盖率", lambda r: f"{r['m']['coverage']:.1%}"),
        ("拼回逐字一致", lambda r: "是" if r["m"]["text_identical"] else "否"),
        ("概要平均字数", lambda r: r["m"]["sum_avg_len"]),
        ("概要以「讲员」开头", lambda r: f"{r['m']['jiangyuan_rate']:.1%}"),
        ("关键词命中原文", lambda r: f"{r['m']['kw_hit']:.1%}"),
        ("cue 填写数", lambda r: r["m"]["cue_filled"]),
        ("cue 去重率", lambda r: f"{r['m']['cue_unique_rate']:.1%}"),
    ]

    print("\n" + "=" * 64)
    print(f"{'指标':<22}{'你的提示词':>18}{'我的改写版':>18}")
    print("-" * 64)
    for name, fn in rows:
        cells = []
        for r in results:
            cells.append("—" if "fatal" in r else str(fn(r)))
        print(f"{name:<22}{cells[0]:>20}{cells[1]:>20}")
    print("=" * 64)

    for r in results:
        if "fatal" in r:
            print(f"\n[{r['label']}] 失败：{r['fatal']}")
            continue
        if r["m"]["issues"]:
            print(f"\n[{r['label']}] 行号问题：{r['m']['issues']}")
        print(f"\n[{r['label']}] 全篇主旨：{r['m']['doc_summary']}")

    dump = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output",
                        f"{a.tag}_compare.json")
    io.open(dump, "w", encoding="utf-8").write(json.dumps(
        [{k: v for k, v in r.items() if k != "content"} for r in results],
        ensure_ascii=False, indent=2))
    print(f"\n明细：{dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

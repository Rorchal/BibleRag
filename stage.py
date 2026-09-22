# -*- coding: utf-8 -*-
"""三阶段结构切分运行器：章 → 节 → 段，每级一次独立调用。

与 v3.py 的区别：v3.py 一口气让模型出三级，本脚本拆成三个阶段，
每个阶段只给本级的判据（见 prompts_stage.py 的动机说明）。

产出与 v3.py 对齐，可直接喂给 goldeval.py：
    output/<tag>_stage_<篇名>.parsed.json

调用次数 = 1 + 章数 + 节数。969 行的稿子约 50 次，比单次调用慢得多，
但每次的输入只有本块的行，prompt 小、判据单一。

用法：
    export DS_BASE=https://api.deepseek.com/chat/completions
    export DS_KEY=<你的密钥>
    export DS_MODEL=deepseek-flash
    python stage.py <txt> --tag s1              # 跑满三级
    python stage.py <txt> --tag s1 --stop-after ch   # 只跑章，用来单独评测章级
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import ds_client as ds
import prompts_stage as P
import v3

USER_CH = """{fewshot}

━━━ 待处理的转写稿 ━━━
共 {n} 行,行号 {lo}..{hi}。格式为「行号 | 内容」。

{numbered}

只输出「章」的 json。"""

USER_SE = """{fewshot}

━━━ 待处理的章 ━━━
这是第 {no} 章,标题「{title}」,占行 {lo}..{hi},共 {n} 行。格式为「行号 | 内容」。

{numbered}

把这一章切成「节」,no 用 "{no}.1"、"{no}.2" 这样的编号。只输出「节」的 json。"""

USER_PA = """{fewshot}

━━━ 待处理的节 ━━━
这是节 {no},标题「{title}」,占行 {lo}..{hi},共 {n} 行。格式为「行号 | 内容」。

{numbered}

把这一节切成「段」。只输出「段」的 json。"""


def numbered(lines: list[str], lo: int, hi: int) -> str:
    """取 [lo, hi] 的行，带上原稿的绝对行号。"""
    return "\n".join(f"{i} | {lines[i - 1]}" for i in range(lo, hi + 1))


def call(system: str, user: str, ctx: dict, max_tokens: int, temperature: float) -> dict | None:
    content, meta = ds.chat(system, user, max_tokens=max_tokens, temperature=temperature,
                            timeout=600, ctx=ctx)
    if content is None:
        print(f"    【失败】{meta.get('error')}")
        return None
    data = ds.parse_json(content)
    if data is None:
        ds.log_failure("json_parse_failed", content, ctx)
        print("    【失败】返回非合法 JSON")
        return None
    return data


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tag", default="stage")
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--stop-after", default="pa", choices=["ch", "se", "pa"],
                    help="只跑到哪一级为止，用于分阶段验证")
    a = ap.parse_args()

    lines = ds.read_lines(a.path)
    N = len(lines)
    base_ctx = {"file": os.path.basename(a.path), "tag": a.tag}
    print(f"文件 {os.path.basename(a.path)}  {N} 行")

    # ── 阶段一：章 ──────────────────────────────────────────────
    print("\n[1/3] 切章…", flush=True)
    d = call(P.SYSTEM_CH,
             USER_CH.format(fewshot=P.FEWSHOT_CH, n=N, lo=1, hi=N,
                            numbered=numbered(lines, 1, N)),
             {**base_ctx, "stage": "ch"}, a.max_tokens, a.temperature)
    if d is None:
        return 2
    chapters = []
    for c in d.get("chapters") or []:
        try:
            chapters.append({"no": c.get("no"), "start": int(c["start"]), "end": int(c["end"]),
                             "title": str(c.get("title", "")).strip(),
                             "trigger": str(c.get("trigger", "")).strip(),
                             "evidence": str(c.get("evidence", "")).strip(),
                             "confidence": c.get("confidence")})
        except (KeyError, TypeError, ValueError):
            continue
    chapters.sort(key=lambda x: x["start"])
    unc = list(d.get("uncertain") or [])
    print(f"      章 {len(chapters)}  边界 {[c['start'] for c in chapters[1:]]}")
    issues = v3.check_seamless(chapters, 1, N, "章")
    for m in issues:
        print("      -", m)

    sections, paragraphs = [], []

    # ── 阶段二：节（逐章）────────────────────────────────────────
    if a.stop_after in ("se", "pa"):
        print(f"\n[2/3] 逐章切节（{len(chapters)} 次调用）…", flush=True)
        for c in chapters:
            lo, hi = c["start"], c["end"]
            print(f"  第{c['no']}章 [{lo}-{hi}] {hi - lo + 1} 行", flush=True)
            d = call(P.SYSTEM_SE,
                     USER_SE.format(fewshot=P.FEWSHOT_SE, no=c["no"], title=c["title"],
                                    lo=lo, hi=hi, n=hi - lo + 1,
                                    numbered=numbered(lines, lo, hi)),
                     {**base_ctx, "stage": "se", "chapter": c["no"]}, a.max_tokens, a.temperature)
            got = []
            if d:
                for s in d.get("sections") or []:
                    try:
                        got.append({"no": s.get("no"), "start": int(s["start"]), "end": int(s["end"]),
                                    "title": str(s.get("title", "")).strip(),
                                    "chapter": c["no"], "synthesized": False})
                    except (KeyError, TypeError, ValueError):
                        continue
                unc += list(d.get("uncertain") or [])
            if not got:  # 失败时整章当一节，标记出来，不中断整条管线
                got = [{"no": f"{c['no']}.1", "start": lo, "end": hi,
                        "title": c["title"], "chapter": c["no"], "synthesized": True}]
                print("    （该章切节失败，整章降级为 1 节）")
            got.sort(key=lambda x: x["start"])
            sections += got
            print(f"    → {len(got)} 节")

    # ── 阶段三：段（逐节）────────────────────────────────────────
    if a.stop_after == "pa":
        print(f"\n[3/3] 逐节切段（{len(sections)} 次调用）…", flush=True)
        for s in sections:
            lo, hi = s["start"], s["end"]
            d = call(P.SYSTEM_PA,
                     USER_PA.format(fewshot=P.FEWSHOT_PA, no=s["no"], title=s["title"],
                                    lo=lo, hi=hi, n=hi - lo + 1,
                                    numbered=numbered(lines, lo, hi)),
                     {**base_ctx, "stage": "pa", "section": s["no"]}, a.max_tokens, a.temperature)
            got = []
            if d:
                for p in d.get("paragraphs") or []:
                    try:
                        got.append({"start": int(p["start"]), "end": int(p["end"]),
                                    "title": str(p.get("title", "")).strip(),
                                    "section": s["no"], "chapter": s["chapter"]})
                    except (KeyError, TypeError, ValueError):
                        continue
                unc += list(d.get("uncertain") or [])
            if not got:
                got = [{"start": lo, "end": hi, "title": s["title"],
                        "section": s["no"], "chapter": s["chapter"]}]
                print(f"  节{s['no']} 切段失败，整节降级为 1 段")
            got.sort(key=lambda x: x["start"])
            paragraphs += got
            print(f"  节{s['no']} [{lo}-{hi}] → {len(got)} 段", flush=True)

    # ── 校验与落盘 ──────────────────────────────────────────────
    print(f"\n章 {len(chapters)}  节 {len(sections)}  段 {len(paragraphs)}  uncertain {len(unc)}")
    print("\n--- 无缝覆盖校验 ---")
    issues = v3.check_seamless(chapters, 1, N, "章")
    if paragraphs:
        issues += v3.check_seamless(paragraphs, 1, N, "段")
    for c in chapters:
        subs = [s for s in sections if s["chapter"] == c["no"]]
        if subs:
            issues += v3.check_seamless(subs, c["start"], c["end"], f"章{c['no']}内的节")
    for s in sections:
        subs = [p for p in paragraphs if p["section"] == s["no"]]
        if subs:
            issues += v3.check_seamless(subs, s["start"], s["end"], f"节{s['no']}内的段")
    if issues:
        print(f"发现 {len(issues)} 处问题：")
        for m in issues[:20]:
            print("  -", m)
        if len(issues) > 20:
            print(f"  …另有 {len(issues) - 20} 处")
    else:
        print("全部无缝覆盖，无问题")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output",
                       f"{a.tag}_stage_{os.path.splitext(os.path.basename(a.path))[0]}")
    io.open(out + ".parsed.json", "w", encoding="utf-8").write(json.dumps(
        {"meta": {"pipeline": "stage", "tag": a.tag, "stop_after": a.stop_after,
                  "calls": 1 + (len(chapters) if a.stop_after in ("se", "pa") else 0)
                           + (len(sections) if a.stop_after == "pa" else 0)},
         "issues": issues, "chapters": chapters, "sections": sections,
         # 只跑到节时不输出 paragraphs 键，避免下游把空列表当成「切了 0 段」
         **({"paragraphs": paragraphs} if a.stop_after == "pa" else {}),
         "uncertain": unc},
        ensure_ascii=False, indent=2))
    print(f"\n输出：{out}.parsed.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

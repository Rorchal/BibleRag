# -*- coding: utf-8 -*-
"""
讲道转写稿语义排版：把「一句一行」的识别结果，切分为段落 + 章节标题。

两种模式：
  rewrite —— 让小模型直接输出排版后的全文（然后逐字校验它没改原文）
  index   —— 只让小模型输出「段落起始行号 + 标题」，正文由本脚本用原始行拼回
             （结构上不可能改动原文，校验必然通过）

用法：
  python segment.py <txt文件> [--mode rewrite|index] [--window 40] [--limit N]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("SERMON_MODEL", "qwen3:8b")

# ---------------------------------------------------------------- 文本工具


def read_lines(path: str) -> list[str]:
    """读取 UTF-8(BOM) 文本，返回非空行列表（去掉行首尾空白）。"""
    text = io.open(path, encoding="utf-8-sig").read()
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def norm(s: str) -> str:
    """归一化用于比对：去掉所有空白字符（含换行），其余一律保留。"""
    return re.sub(r"\s+", "", s)


def strip_titles(md: str) -> tuple[str, list[str]]:
    """把 markdown 里的标题行摘出来，返回 (正文, 标题列表)。"""
    body, titles = [], []
    for ln in md.splitlines():
        if re.match(r"^\s*#{1,6}\s+", ln):
            titles.append(re.sub(r"^\s*#{1,6}\s+", "", ln).strip())
        else:
            body.append(ln)
    return "\n".join(body), titles


def first_diff(a: str, b: str) -> str:
    """定位两个归一化字符串的第一处差异，给出上下文。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    if i == n and len(a) == len(b):
        return "无差异"
    lo = max(0, i - 25)
    return (
        f"首个差异在第 {i} 个字符（原文长 {len(a)}，模型长 {len(b)}）\n"
        f"    原文 …{a[lo:i]}[{a[i:i + 15] or '<到此结束>'}]…\n"
        f"    模型 …{b[lo:i]}[{b[i:i + 15] or '<到此结束>'}]…"
    )


# ---------------------------------------------------------------- 模型调用


def ollama_chat(system: str, user: str, num_ctx: int = 8192, fmt: str | None = None) -> str:
    payload = {
        "model": MODEL,
        "stream": False,
        "think": False,  # qwen3 关掉思考链，快且省 token
        "options": {"temperature": 0.1, "top_p": 0.9, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if fmt:
        payload["format"] = fmt
    req = urllib.request.Request(
        OLLAMA + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]


# ---------------------------------------------------------------- 模式 A：改写

SYS_REWRITE = """你是中文文稿排版助手，处理的是讲道录音的语音识别稿（一句一行）。

你的唯一任务是【排版】，把连续的句子按语义合并成自然段，并为语义相近的若干自然段加小标题。

绝对禁止（违反即为失败）：
- 禁止修改、删除、增加任何一个汉字、数字或标点符号
- 禁止改写、润色、纠正错别字、纠正识别错误、补全省略、删除口语词（"呃""哈""啊"等一律保留）
- 禁止总结、概括、翻译、解释
- 禁止输出任何说明性文字（如"以下是排版结果"）

允许的操作只有两种：
1. 把原文的换行改为空格或删除（即把多行合并成一个自然段），段与段之间用一个空行分隔
2. 在段落之间插入 markdown 二级标题行，格式为 `## 标题`（标题由你自己撰写，不超过 12 字）

输出：直接输出排版后的正文，不要代码块包裹。"""

USER_REWRITE = """把下面的识别稿排版成自然段，并在合适处插入 `## 标题`。
记住：正文的每一个字、每一个标点都必须与原文完全一致，你只能动换行和插入标题行。

---原文开始---
{body}
---原文结束---"""


def run_rewrite(lines: list[str]) -> tuple[str, dict]:
    body = "\n".join(lines)
    out = ollama_chat(SYS_REWRITE, USER_REWRITE.format(body=body), num_ctx=16384)
    out = re.sub(r"^\s*```[a-z]*\s*|\s*```\s*$", "", out.strip())
    text, titles = strip_titles(out)
    ok = norm(text) == norm(body)
    return out, {
        "ok": ok,
        "titles": titles,
        "orig_chars": len(norm(body)),
        "model_chars": len(norm(text)),
        "diff": "无差异" if ok else first_diff(norm(body), norm(text)),
    }


# ---------------------------------------------------------------- 模式 B：只给行号

SYS_INDEX = """你是中文文稿结构分析助手，处理的是讲道录音的语音识别稿（一句一行，每行带行号）。

任务：判断段落边界和章节标题，只输出结构信息，不要输出正文。

输出严格的 JSON，格式：
{{"breaks": [1, 8, 15, ...], "titles": {{"1": "开场问安", "15": "宣读经文"}}}}

breaks（段落起始行号）规则：
- 必须升序，必须包含第一行的行号
- 一个自然段【至少 6 行、通常 8~15 行】，只在话题或论证步骤真正转换处断段
- 讲道中反复强调同一件事的句子属于同一段，不要因为句子多就断段

titles（章节标题）规则：
- 标题总数【最多 {max_titles} 个】，宁少勿多；本段文本若没有明显的话题切换，titles 可以为空对象 {{}}
- 键必须是 breaks 里的某个行号，表示在那一段【之前】插入标题
- 标题概括的是它【后面】那几段的内容，不是前面的内容
- 一个章节应覆盖多个自然段（通常 4 段以上），不要给每段都起标题
- 标题不超过 12 字，用名词短语，不要用"第一层""第二点"这类纯序号

不要输出 JSON 以外的任何内容。"""

USER_INDEX = """识别稿（行号 | 内容）：

{numbered}

输出段落起始行号与章节标题的 JSON。"""


def run_index(lines: list[str], base: int, max_titles: int = 2) -> tuple[str, dict]:
    numbered = "\n".join(f"{base + i} | {ln}" for i, ln in enumerate(lines))
    raw = ollama_chat(SYS_INDEX.format(max_titles=max_titles),
                      USER_INDEX.format(numbered=numbered), num_ctx=16384, fmt="json")
    lo, hi = base, base + len(lines) - 1
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}

    breaks = sorted({int(b) for b in data.get("breaks", []) if lo <= int(b) <= hi})
    if not breaks or breaks[0] != lo:
        breaks = [lo] + breaks
    cand = []
    for k, v in (data.get("titles") or {}).items():
        try:
            k = int(k)
        except (TypeError, ValueError):
            continue
        if k in breaks and isinstance(v, str) and v.strip():
            cand.append((k, v.strip()[:20]))
    # 超预算时保留靠前的标题，保证顺序稳定
    titles = dict(sorted(cand)[:max_titles])

    # 用原始行拼回正文 —— 模型碰不到一个字
    out = []
    for idx, start in enumerate(breaks):
        end = breaks[idx + 1] if idx + 1 < len(breaks) else hi + 1
        if start in titles:
            out.append(f"## {titles[start]}")
        out.append("".join(lines[start - base:end - base]))
    md = "\n\n".join(out)
    text, tl = strip_titles(md)
    return md, {
        "ok": norm(text) == norm("\n".join(lines)),  # 恒为真，作为回归断言
        "titles": tl,
        "paragraphs": len(breaks),
        "orig_chars": len(norm("\n".join(lines))),
        "model_chars": len(norm(text)),
        "diff": "无差异（结构化模式下模型不接触正文）",
    }


# ---------------------------------------------------------------- 主流程


def process(path: str, mode: str, window: int, limit: int | None, max_titles: int = 2) -> dict:
    lines = read_lines(path)
    chunks = [lines[i:i + window] for i in range(0, len(lines), window)]
    if limit:
        chunks = chunks[:limit]

    md_parts, reports = [], []
    t0 = time.time()
    for i, ch in enumerate(chunks, 1):
        base = (i - 1) * window + 1
        ts = time.time()
        try:
            md, rep = run_rewrite(ch) if mode == "rewrite" else run_index(ch, base, max_titles)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            md, rep = "\n\n".join(ch), {"ok": False, "titles": [], "diff": f"调用失败: {e}",
                                        "orig_chars": len(norm("\n".join(ch))), "model_chars": 0}
        rep.update(chunk=i, lines=len(ch), secs=round(time.time() - ts, 1))
        reports.append(rep)
        md_parts.append(md)
        flag = "OK " if rep["ok"] else "FAIL"
        print(f"  [{flag}] 窗口{i}/{len(chunks)} {len(ch)}行 {rep['secs']}s "
              f"标题{len(rep['titles'])}个 原文{rep['orig_chars']}字 → 模型{rep['model_chars']}字",
              file=sys.stderr, flush=True)

    n_ok = sum(1 for r in reports if r["ok"])
    return {
        "file": os.path.basename(path),
        "mode": mode,
        "window": window,
        "model": MODEL,
        "total_lines": len(lines),
        "chunks": len(chunks),
        "chunks_ok": n_ok,
        "pass_rate": round(n_ok / len(chunks), 3) if chunks else 0.0,
        "elapsed_s": round(time.time() - t0, 1),
        "markdown": "\n\n".join(md_parts),
        "reports": reports,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--mode", choices=["rewrite", "index"], default="index")
    ap.add_argument("--window", type=int, default=40)
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 个窗口（试跑用）")
    ap.add_argument("--max-titles", type=int, default=2, help="每个窗口最多几个章节标题")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    res = process(a.path, a.mode, a.window, a.limit, a.max_titles)
    out = a.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "output",
        re.sub(r"\.txt$", "", os.path.basename(a.path)) + f".{a.mode}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    io.open(out, "w", encoding="utf-8").write(res["markdown"])
    io.open(out + ".report.json", "w", encoding="utf-8").write(
        json.dumps({k: v for k, v in res.items() if k != "markdown"}, ensure_ascii=False, indent=2))

    print(f"\n模式={res['mode']} 模型={res['model']} 窗口={res['window']}行")
    print(f"窗口通过 {res['chunks_ok']}/{res['chunks']}（{res['pass_rate']:.0%}） "
          f"耗时 {res['elapsed_s']}s")
    print(f"输出 {out}")
    return 0 if res["chunks_ok"] == res["chunks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

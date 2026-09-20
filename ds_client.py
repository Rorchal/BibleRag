# -*- coding: utf-8 -*-
"""
DeepSeek (chatapi.weixin.qq.com 网关) 客户端 + 讲道稿「章/节/段」结构拆分。

设计约束（来自用户）：
- 只让模型输出【段的起止行号】，正文一律从原文拼回，模型碰不到一个字
- 调用失败或返回不可用时【不重试】，记录到 logs/api_failures.jsonl 后跳过
- 拿到返回必须做结构校验：行号覆盖完整、不重叠、不越界、拼回后与原文逐字相同

注意：请求体必须以 UTF-8 字节发出（本文件用 urllib 直接发 bytes）。
      不要用 curl -d '{...中文...}' 传参，命令行层会破坏编码。
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.request

BASE = os.environ.get("DS_BASE", "https://chatapi.weixin.qq.com/openai/v1/chat/completions")
KEY = os.environ.get("DS_KEY", "")  # 必填；不再内置默认密钥（旧默认值已泄露，见 docs/目录结构梳理.md P0）
MODEL = os.environ.get("DS_MODEL", "Deepseek-v4-flash")

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ROOT, "logs")


def log_failure(kind: str, detail: str, ctx: dict) -> None:
    """失败只记录，不重试。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
           "detail": detail[:2000], **ctx}
    with io.open(os.path.join(LOG_DIR, "api_failures.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def chat(system: str, user: str, max_tokens: int = 4000, temperature: float = 0.2,
         timeout: int = 180, ctx: dict | None = None,
         reasoning_effort: str | None = None) -> tuple[str | None, dict]:
    """单次调用，不重试。返回 (content 或 None, meta)。

    reasoning_effort: None=不带该参数（模型默认不思考，reasoning_tokens=0）；
                      "low"/"high" 会启用思考模式。
                      注意：thinking / enable_thinking / seed 这三个参数本网关
                      静默忽略（不报错也不生效），不要用。
    """
    if not KEY:
        raise RuntimeError(
            "未设置环境变量 DS_KEY。本文件不再内置默认密钥——\n"
            "  bash:       export DS_KEY=<你的密钥>\n"
            "  PowerShell: $env:DS_KEY=\"<你的密钥>\"\n"
            "注意：仓库历史里那条旧密钥已公开泄露，不要再用。")
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")  # 关键：显式 UTF-8 bytes
    req = urllib.request.Request(BASE, data=body, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + KEY,
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
            json.JSONDecodeError) as e:
        detail = f"{type(e).__name__}: {e}"
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail += " | " + e.read().decode("utf-8", "replace")[:500]
            except OSError:
                pass
        log_failure("request_failed", detail, ctx or {})
        return None, {"ok": False, "error": detail, "secs": round(time.time() - t0, 1)}

    meta = {"ok": True, "secs": round(time.time() - t0, 1),
            "usage": raw.get("usage", {}), "id": raw.get("id"),
            "reasoning_effort": reasoning_effort}
    try:
        choice = raw["choices"][0]
        content = choice["message"]["content"]
        meta["finish_reason"] = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        log_failure("bad_response_shape", json.dumps(raw, ensure_ascii=False)[:1000], ctx or {})
        return None, {"ok": False, "error": "返回结构异常", **meta}

    if not content or not content.strip():
        log_failure("empty_content", json.dumps(raw, ensure_ascii=False)[:1000], ctx or {})
        return None, {"ok": False, "error": "返回内容为空", **meta}
    if meta.get("finish_reason") == "length":
        meta["truncated"] = True
    return content, meta


# ------------------------------------------------------------------ 提示词

SYSTEM = """你是中文长文档的结构分析助手。输入是一篇讲道录音的语音识别稿，一句一行，每行前面有行号。

你的任务：把全文拆成【章 → 节 → 段】三级结构，并为每一段写一句概要。只输出结构与概要，绝不输出正文原句。

输出严格的 JSON（不要 markdown 代码块，不要任何解释文字）：
{
  "doc_summary": "全篇一句话主旨，不超过 40 字",
  "chapters": [
    {
      "title": "章标题",
      "summary": "本章讲了什么，一到两句，不超过 60 字",
      "sections": [
        {
          "title": "节标题",
          "summary": "本节讲了什么，一句，不超过 40 字",
          "paragraphs": [
            {"start": 1, "end": 6,
             "summary": "这几行在讲什么，一句完整的话，25~50 字",
             "keywords": ["约伯", "撒旦", "属灵征战"]}
          ]
        }
      ]
    }
  ]
}

【行号规则】违反即判为失败：
1. start/end 是行号，闭区间，start <= end
2. 所有段按行号升序，必须完整覆盖第一行到最后一行的每个行号：
   首段 start = 全文第一行行号，末段 end = 全文最后一行行号，
   后一段的 start 恒等于前一段的 end + 1，不允许空洞、不允许重叠
3. 一个段是一个自然段，【必须控制在 5~15 行】。全文会因此产生较多的段，这是预期的，不要为了省事把二三十行合成一段。
4. 一个节含 2~5 段，一个章含 3~8 节

【概要规则】这是给检索系统用的，不是给人看的摘要，务必遵守：
5. 概要必须把该段中用代词、口头语指代的对象【显式写出来】。
   例：原文是"他这样做是无故的吗？"，概要要写成"撒旦质疑约伯敬畏神是有条件的，因为神赐福保护他"。
6. 概要必须包含该段涉及的具体人物、地名、经文出处、神学概念，不要写"讲员继续解释这个问题"这类空话。
7. 概要用书面语，不要保留"哈""呃""啊"等口头禅。
8. 概要只能陈述原文确实说过的内容，不得引申、评价或补充原文没有的信息。
9. keywords 给 3~6 个：人名、地名、书卷章节、神学术语。只填该段真正涉及的，不要硬凑。

【标题规则】
10. 章/节标题不超过 15 字，用名词短语概括内容，不要用"第一部分""其二"这类纯序号。

只输出 JSON。"""

USER_TMPL = """下面是识别稿，格式为「行号 | 内容」，共 {n} 行，行号从 {lo} 到 {hi}。

{numbered}

请输出章/节/段结构与概要的 JSON。
两条硬要求：段的行号必须连续覆盖 {lo} 到 {hi} 的全部行不重不漏；每段 5~15 行，且每段都要有 summary 和 keywords。"""


# ------------------------------------------------------------------ 校验


def read_lines(path: str) -> list[str]:
    text = io.open(path, encoding="utf-8-sig").read()
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def parse_json(content: str) -> dict | None:
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def flatten(data: dict) -> list[dict]:
    """展平成段列表，带上所属章/节的标题与概要。"""
    out = []
    for ci, ch in enumerate(data.get("chapters") or [], 1):
        for si, sec in enumerate(ch.get("sections") or [], 1):
            for p in sec.get("paragraphs") or []:
                try:
                    kws = p.get("keywords") or []
                    if isinstance(kws, str):
                        kws = [k.strip() for k in re.split(r"[,，、;；]", kws) if k.strip()]
                    out.append({
                        "chapter": ci,
                        "chapter_title": str(ch.get("title", "")).strip(),
                        "chapter_summary": str(ch.get("summary", "")).strip(),
                        "section": si,
                        "section_title": str(sec.get("title", "")).strip(),
                        "section_summary": str(sec.get("summary", "")).strip(),
                        "start": int(p["start"]), "end": int(p["end"]),
                        "summary": str(p.get("summary", "")).strip(),
                        "keywords": [str(k).strip() for k in kws if str(k).strip()],
                    })
                except (KeyError, TypeError, ValueError):
                    continue
    return out


FILLERS = ("哈", "呃", "啊", "呢", "吧")
VAGUE = ("讲员", "继续解释", "进一步说明", "这个问题", "上述内容", "本段", "这一段")


def check_summaries(paras: list[dict], lines: list[str], lo: int) -> dict:
    """概要是模型新生成的内容，不能逐字校验；这里只做可自动判定的质量检查。"""
    n = len(paras)
    missing = [p["start"] for p in paras if not p["summary"]]
    no_kw = [p["start"] for p in paras if not p["keywords"]]
    too_short = [p["start"] for p in paras if p["summary"] and len(p["summary"]) < 15]
    too_long = [p["start"] for p in paras if len(p["summary"]) > 80]
    vague = [p["start"] for p in paras if any(v in p["summary"] for v in VAGUE)]
    oral = [p["start"] for p in paras
            if p["summary"].rstrip("。！？").endswith(FILLERS)]

    # keywords 是否真的出现在该段原文里（没出现不一定错，但比例过低说明在编）
    hit = tot = 0
    miss_samples = []
    for p in paras:
        body = "".join(lines[p["start"] - lo:p["end"] - lo + 1])
        for k in p["keywords"]:
            tot += 1
            if k in body:
                hit += 1
            elif len(miss_samples) < 8:
                miss_samples.append(f"{p['start']}-{p['end']}:{k}")

    lens = [len(p["summary"]) for p in paras if p["summary"]]
    return {
        "paragraphs": n,
        "with_summary": n - len(missing),
        "with_keywords": n - len(no_kw),
        "summary_len_avg": round(sum(lens) / len(lens), 1) if lens else 0,
        "keyword_total": tot,
        "keyword_in_text_rate": round(hit / tot, 3) if tot else 0.0,
        "keyword_not_in_text_samples": miss_samples,
        "missing_summary": missing[:10],
        "missing_keywords": no_kw[:10],
        "too_short": too_short[:10],
        "too_long": too_long[:10],
        "vague_wording": vague[:10],
        "oral_wording": oral[:10],
    }


def verify(paras: list[dict], lo: int, hi: int, lines: list[str]) -> dict:
    """核验模型返回的行号结构，并验证按此拼回的正文与原文逐字相同。"""
    issues = []
    if not paras:
        return {"ok": False, "issues": ["模型未返回任何段"], "covered": 0,
                "text_identical": False}

    for p in paras:
        if p["start"] > p["end"]:
            issues.append(f"段 {p['start']}-{p['end']} 起止颠倒")
        if p["start"] < lo or p["end"] > hi:
            issues.append(f"段 {p['start']}-{p['end']} 越界（应在 {lo}-{hi}）")

    ordered = sorted(paras, key=lambda x: (x["start"], x["end"]))
    if [id(x) for x in ordered] != [id(x) for x in paras]:
        issues.append("段未按行号升序排列")

    if ordered[0]["start"] != lo:
        issues.append(f"首段从 {ordered[0]['start']} 开始，应为 {lo}")
    if ordered[-1]["end"] != hi:
        issues.append(f"末段到 {ordered[-1]['end']} 结束，应为 {hi}")

    gaps, overlaps = [], []
    for a, b in zip(ordered, ordered[1:]):
        if b["start"] > a["end"] + 1:
            gaps.append(f"{a['end'] + 1}-{b['start'] - 1}")
        elif b["start"] <= a["end"]:
            overlaps.append(f"{b['start']}-{a['end']}")
    if gaps:
        issues.append(f"漏掉行段: {', '.join(gaps[:10])}")
    if overlaps:
        issues.append(f"重叠行段: {', '.join(overlaps[:10])}")

    seen = set()
    for p in ordered:
        seen |= set(range(max(p["start"], lo), min(p["end"], hi) + 1))
    covered = len(seen)

    # 用原文行拼回，逐字比对
    rebuilt = "".join("".join(lines[p["start"] - lo:p["end"] - lo + 1]) for p in ordered)
    original = "".join(lines)
    identical = rebuilt == original
    if not identical:
        issues.append(f"拼回正文与原文不一致（原文 {len(original)} 字，拼回 {len(rebuilt)} 字）")

    return {"ok": not issues, "issues": issues, "covered": covered,
            "coverage_rate": round(covered / (hi - lo + 1), 4),
            "text_identical": identical,
            "orig_chars": len(original), "rebuilt_chars": len(rebuilt)}


def build_markdown(paras: list[dict], lines: list[str], lo: int,
                   doc_summary: str = "", with_summary: bool = True) -> str:
    """正文一律来自原始行；概要以引用块形式单独呈现，与正文视觉隔离。"""
    out, cur_ch, cur_sec = [], None, None
    if doc_summary:
        out.append(f"> **全篇主旨**：{doc_summary}")
    for p in paras:
        if p["chapter"] != cur_ch:
            out.append(f"# {p['chapter_title'] or '（无标题）'}")
            if with_summary and p["chapter_summary"]:
                out.append(f"> {p['chapter_summary']}")
            cur_ch, cur_sec = p["chapter"], None
        if p["section"] != cur_sec:
            out.append(f"## {p['section_title'] or '（无标题）'}")
            if with_summary and p["section_summary"]:
                out.append(f"> {p['section_summary']}")
            cur_sec = p["section"]
        if with_summary and p["summary"]:
            kw = ("　`" + "` `".join(p["keywords"]) + "`") if p["keywords"] else ""
            out.append(f"> *[{p['start']}-{p['end']}]* {p['summary']}{kw}")
        out.append("".join(lines[p["start"] - lo:p["end"] - lo + 1]))
    return "\n\n".join(out)


def build_chunks(paras: list[dict], lines: list[str], lo: int, doc: str,
                 doc_summary: str = "") -> list[dict]:
    """产出可直接入库的检索单元：embed_text 用于嵌入，text 用于回答。"""
    chunks = []
    for i, p in enumerate(paras):
        body = "".join(lines[p["start"] - lo:p["end"] - lo + 1])
        head = " / ".join(x for x in (p["chapter_title"], p["section_title"]) if x)
        chunks.append({
            "id": f"{doc}#{p['start']}-{p['end']}",
            "doc": doc,
            "seq": i,
            "start_line": p["start"], "end_line": p["end"],
            "chapter_title": p["chapter_title"], "section_title": p["section_title"],
            "summary": p["summary"], "keywords": p["keywords"],
            "text": body,
            # 检索侧：标题路径 + 概要 + 关键词 + 原文，一起嵌入
            "embed_text": f"{head}\n{p['summary']}\n{' '.join(p['keywords'])}\n{body}",
            "chars": len(body), "lines": p["end"] - p["start"] + 1,
        })
    return chunks

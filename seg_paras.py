# -*- coding: utf-8 -*-
"""按已切好的节切段：每章调用一次 DeepSeek 官方接口，共 8 次，不重试。

提示词取自 prompts/切段_v1.md（SYSTEM + FEWSHOT 原样使用）。
v1 设计是一节一调；这里按章送，一次把本章所有节交给模型逐节切段，
所以 user 消息末尾把输出包成 {"chapter": n, "sections": [每节一个 v1 格式对象]}。

调用前、全部调用后各查一次余额（GET /user/balance）。

用法：
  export DEEPSEEK_API_KEY=...
  python seg_paras.py [--prompt v1|v2] [--effort low|none] [--model <id>] [--tag p1]
结果写到 output/paras_<prompt>_<tag>/。
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

from verify_paragraphs import verify_paragraphs

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")
KEY = os.environ.get("DEEPSEEK_API_KEY", "")

TXT = os.path.join(HERE, "input", "GH_伯1章1到8节_校对.txt")
# 专名表（切段 v4 起使用）：讲道人姓名 + 录音识别时的热词表（task.json 里的 hotwords）
NAMES = "陈社炳、约伯、约伯记、乌斯、以利法、提幔、比勒达、书亚、琐法、拿玛、以利户、巴拉迦、布西、示巴、迦勒底、耶和华、撒但"
SECTIONS = os.path.join(HERE, "input", "c2low4_se_v2_chapter_GH_伯1章1到8节.parsed.json")


def http(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + KEY,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def balance() -> str:
    d = http("GET", "/user/balance")
    parts = [f"{b['currency']} {b['total_balance']}"
             f"（充值 {b.get('topped_up_balance')}，赠送 {b.get('granted_balance')}）"
             for b in d.get("balance_infos", [])]
    return "；".join(parts) + ("" if d.get("is_available", True) else "  【不可用】")


def pick_model(explicit: str | None) -> str:
    if explicit:
        return explicit
    ids = [m["id"] for m in http("GET", "/models").get("data", [])]
    flash = [i for i in ids if "flash" in i.lower()]
    if len(flash) != 1:
        raise SystemExit(f"无法唯一确定 flash 模型，可用模型：{ids}，请用 --model 指定")
    return flash[0]


def load_prompt(ver: str) -> tuple[str, str]:
    md = io.open(os.path.join(HERE, "prompts", f"切段_{ver}.md"), encoding="utf-8").read()
    system = re.search(r"## 一、SYSTEM\n(.*?)\n---\n", md, re.S).group(1).strip()
    fewshot = re.search(r"## 二、FEWSHOT[^\n]*\n(.*?)\n---\n", md, re.S).group(1).strip()
    return system, fewshot


def read_lines(path: str) -> list[str]:
    # 不能丢空行：行号必须与原稿一致
    lines = io.open(path, encoding="utf-8-sig").read().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def build_user(fewshot: str, ch_no: int, secs: list[dict], lines: list[str],
               names: str | None = None) -> str:
    """names 不为空时（v4 起）按 v4 模板在每节前加专名表，结尾加「逐项核对」一句。"""
    blocks = []
    for s in secs:
        lo, hi = s["start"], s["end"]
        numbered = "\n".join(f"{i} | {lines[i - 1]}" for i in range(lo, hi + 1))
        blocks.append(
            f"━━━ 待处理的节 ━━━\n"
            + (f"专名表:{names}\n" if names else "")
            + f"所在节:{s['no']}「{s['title']}」\n"
            f"行号 {lo}..{hi},共 {hi - lo + 1} 行。格式为「行号 | 内容」。\n\n{numbered}")
    spec = "\n".join(f"- 节 {s['no']}:cuts 第一个数必须是 {s['start']};最后一段的 end 必须是 {s['end']}"
                     for s in secs)
    return (f"{fewshot}\n\n"
            f"本次输入是第 {ch_no} 章,共 {len(secs)} 节,节的范围已定、不许改动。"
            f"请对每一节分别按规则切段,各节互不影响。\n\n"
            + "\n\n".join(blocks)
            + f"\n\n把以上每一节分别切成「段」。\n{spec}\n"
            + ("写完每个标题,逐项核对主体、对象、身份、范围、因果。" if names else "")
            + f"只输出 json,格式为 {{\"chapter\": {ch_no}, \"sections\": [每一节一个对象,"
            f"字段同 SYSTEM 里的 section/cuts/paragraphs/uncertain,按节号顺序]}}。")


def parse_content(content: str) -> tuple[dict, bool]:
    """解析模型输出；遇到尾逗号（如 `[...], }`）这类小毛病就去掉再解析。返回 (数据, 是否修过)。"""
    try:
        return json.loads(content), False
    except json.JSONDecodeError:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", content)), True


# 标题禁用词（切段 v2「标题禁用词」第 1 条），命中算硬错误
BANNED = re.compile(r"讲者|讲员|讲道人|讲道者|作者|牧师|本段|这段|本节|这一节")
# 第一、二人称（切段 v3「标题禁用词」第 3 条），「」内直接引语除外，命中算硬错误
def person_hits(title: str) -> list[str]:
    bare = re.sub(r"「[^」]*」", "", title)
    return sorted(set(re.findall(r"我们|你们|咱们|我|你|咱", bare)))


# 指示词（第 2 条），是否悬空要人看，只作警告
DEMONS = re.compile(r"这个|那个|这种|那种|这样|那样|这些|那些|这件|据此|(?<![因由如彼从])此")


def title_warnings(P: list[dict]) -> list[str]:
    out = []
    for p in P:
        t = p.get("title") or ""
        hit = sorted(set(DEMONS.findall(t)))
        if hit:
            out.append(f"指示词 {'/'.join(hit)}（行{p.get('start')}-{p.get('end')}）")
    return out


def check(sec: dict, out: dict | None) -> list[str]:
    if out is None:
        return ["模型没有输出这一节"]
    lo, hi = sec["start"], sec["end"]
    P = out.get("paragraphs") or []
    iss = []
    if not P:
        return ["没有段"]
    if P[0].get("start") != lo:
        iss.append(f"首段 start={P[0].get('start')}≠{lo}")
    if P[-1].get("end") != hi:
        iss.append(f"末段 end={P[-1].get('end')}≠{hi}")
    for a, b in zip(P, P[1:]):
        if b.get("start") != a.get("end", -9) + 1:
            iss.append(f"断缝 {a.get('end')}→{b.get('start')}")
    if [p.get("start") for p in P] != out.get("cuts"):
        iss.append("cuts 与段起点不一致")
    for p in P:
        n = len(p.get("title") or "")
        if not 20 <= n <= 110:
            iss.append(f"标题长度 {n}（行{p.get('start')}-{p.get('end')}）")
        bad = sorted(set(BANNED.findall(p.get("title") or "")))
        if bad:
            iss.append(f"禁用词 {'/'.join(bad)}（行{p.get('start')}-{p.get('end')}）")
        per = person_hits(p.get("title") or "")
        if per:
            iss.append(f"人称 {'/'.join(per)}（行{p.get('start')}-{p.get('end')}）")
    return iss


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", choices=["v1", "v2", "v3", "v4a", "v4"], default="v1", help="切段提示词版本")
    ap.add_argument("--effort", choices=["none", "low", "high"], default="low")
    ap.add_argument("--model", default=None)
    ap.add_argument("--tag", default="p1")
    ap.add_argument("--txt", default=None, help="原文 txt（默认伯1:1-8 校对版）")
    ap.add_argument("--sections", default=None, help="切节结果 parsed.json（默认 c2low4）")
    ap.add_argument("--name", default=None,
                    help="结果目录名，默认 paras_<prompt>_<tag>；换讲道时建议写成 paras_<prompt>_<讲道名>")
    ap.add_argument("--from-raw", action="store_true",
                    help="不调用接口，用 output/paras_<prompt>_<tag>/ch*.raw.json 重新整理结果")
    a = ap.parse_args()
    global TXT, SECTIONS
    TXT = a.txt or TXT
    SECTIONS = a.sections or SECTIONS
    if a.from_raw:
        return rebuild(a)
    if not KEY:
        raise SystemExit("缺少环境变量 DEEPSEEK_API_KEY")

    print(f"调用前余额：{balance()}")
    model = pick_model(a.model)
    system, fewshot = load_prompt(a.prompt)
    lines = read_lines(TXT)
    secs = json.load(io.open(SECTIONS, encoding="utf-8"))["sections"]
    chapters = sorted({s["chapter"] for s in secs})
    print(f"模型 {model}，effort={a.effort}，temperature=0.2，{len(lines)} 行，"
          f"{len(chapters)} 章 / {len(secs)} 节，每章调用 1 次，不重试\n")

    outdir = os.path.join(HERE, "output", a.name or f"paras_{a.prompt}_{a.tag}")
    os.makedirs(outdir, exist_ok=True)
    merged, calls = [], []
    for ch in chapters:
        cs = [s for s in secs if s["chapter"] == ch]
        payload = {"model": model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": build_user(
                                    fewshot, ch, cs, lines,
                                    NAMES if a.prompt >= "v4" and a.prompt != "v4a" else None)}],
                   "temperature": 0.2, "max_tokens": 64000,
                   "response_format": {"type": "json_object"}}
        if a.effort != "none":
            payload["reasoning_effort"] = a.effort
        t0 = time.time()
        rec = {"chapter": ch, "sections": len(cs), "lines": cs[-1]["end"] - cs[0]["start"] + 1}
        try:
            raw = http("POST", "/chat/completions", payload, timeout=1200)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            detail = f"{type(e).__name__}: {e}"
            if isinstance(e, urllib.error.HTTPError):
                detail += " | " + e.read().decode("utf-8", "replace")[:300]
            rec.update(error=detail, secs=round(time.time() - t0, 1))
            calls.append(rec)
            print(f"第{ch}章 【失败】{detail}")
            merged += [{**s, "issues": ["调用失败"], "paragraphs": []} for s in cs]
            continue
        rec.update(secs=round(time.time() - t0, 1), usage=raw.get("usage"),
                   finish_reason=raw["choices"][0].get("finish_reason"))
        calls.append(rec)
        io.open(os.path.join(outdir, f"ch{ch}.raw.json"), "w", encoding="utf-8").write(
            json.dumps(raw, ensure_ascii=False, indent=1))
        n_bad = collect(raw, ch, cs, merged, rec)
        u = raw.get("usage") or {}
        print(f"第{ch}章  {len(cs)} 节 {rec['lines']} 行  {rec['secs']}s  "
              f"入 {u.get('prompt_tokens')}（缓存命中 {u.get('prompt_cache_hit_tokens')}）"
              f" 出 {u.get('completion_tokens')}  finish={rec['finish_reason']}  "
              f"段 {sum(len(m['paragraphs']) for m in merged if m['chapter'] == ch)}"
              f"  有问题的节 {n_bad}")

    write_outputs(outdir, {"prompt": f"切段_{a.prompt}", "txt": os.path.relpath(TXT, HERE),
                           "sections": os.path.relpath(SECTIONS, HERE), "model": model, "effort": a.effort,
                           "temperature": 0.2, "calls": calls}, merged)

    print(f"\n调用后余额：{balance()}")
    print(summary(merged))
    print(f"结果：{outdir}")
    return 0


def collect(raw: dict, ch: int, cs: list[dict], merged: list[dict], rec: dict) -> int:
    content = raw["choices"][0]["message"].get("content") or ""
    try:
        data, fixed = parse_content(content)
        got = {str(x.get("section")): x for x in data.get("sections", [])}
    except (json.JSONDecodeError, AttributeError):
        got, fixed = {}, False
    rec["json_fixed"] = fixed
    rec["empty"] = not content.strip()
    lines = read_lines(TXT)
    n_bad = 0
    for s in cs:
        o = got.get(s["no"])
        iss = ["模型返回内容为空（输出全是推理）"] if rec["empty"] else check(s, o)
        n_bad += bool(iss)
        # 用户提供的校验脚本（verify_paragraphs.py）：fatal 按规定应重跑一次，这里只记录
        vf, vw = (verify_paragraphs(o, lines, s["start"], s["end"], s["title"])
                  if o else (["没有输出任何段"], []))
        merged.append({"no": s["no"], "start": s["start"], "end": s["end"],
                       "title": s["title"], "chapter": ch,
                       "paragraphs": (o or {}).get("paragraphs", []),
                       "uncertain": (o or {}).get("uncertain", []), "issues": iss,
                       "verify_fatal": vf, "verify_warns": vw,
                       "warnings": title_warnings((o or {}).get("paragraphs", []))})
    return n_bad


def rebuild(a) -> int:
    outdir = os.path.join(HERE, "output", a.name or f"paras_{a.prompt}_{a.tag}")
    meta = json.load(io.open(os.path.join(outdir, "parsed.json"), encoding="utf-8"))["meta"]
    secs = json.load(io.open(SECTIONS, encoding="utf-8"))["sections"]
    merged = []
    for rec in meta["calls"]:
        ch = rec["chapter"]
        cs = [s for s in secs if s["chapter"] == ch]
        path = os.path.join(outdir, f"ch{ch}.raw.json")
        if not os.path.exists(path):
            merged += [{**s, "issues": ["调用失败"], "paragraphs": [], "uncertain": []} for s in cs]
            continue
        n_bad = collect(json.load(io.open(path, encoding="utf-8")), ch, cs, merged, rec)
        print(f"第{ch}章  段 {sum(len(m['paragraphs']) for m in merged if m['chapter'] == ch)}"
              f"  有问题的节 {n_bad}{'  （JSON 尾逗号已修复）' if rec['json_fixed'] else ''}")
    write_outputs(outdir, meta, merged)
    print(summary(merged))
    return 0


def summary(merged: list[dict]) -> str:
    P = [p for s in merged for p in s["paragraphs"]]
    ban = sum(bool(BANNED.search(p.get("title") or "")) for p in P)
    dem = sum(bool(DEMONS.search(p.get("title") or "")) for p in P)
    per = sum(bool(person_hits(p.get("title") or "")) for p in P)
    vf = sum(len(s.get("verify_fatal", [])) for s in merged)
    vw = sum(len(s.get("verify_warns", [])) for s in merged)
    return (f"verify_paragraphs：致命 {vf} 条，警告 {vw} 条\n"
            f"合计 {len(P)} 段；有问题的节 {sum(bool(s['issues']) for s in merged)} / {len(merged)}；"
            f"标题含禁用词 {ban} 段，含第一二人称 {per} 段，含指示词 {dem} 段（后者需人工看是否悬空）")


def write_outputs(outdir: str, meta: dict, merged: list[dict]) -> None:
    io.open(os.path.join(outdir, "parsed.json"), "w", encoding="utf-8").write(json.dumps(
        {"meta": meta, "sections": merged}, ensure_ascii=False, indent=2))
    md = [f"# 伯1:1-8 按节切段（{meta['prompt']}，每章一次调用）", ""]
    for s in merged:
        md.append(f"## {s['no']} {s['title']}（行{s['start']}–{s['end']}）")
        for p in s["paragraphs"]:
            md.append(f"- **行{p.get('start')}–{p.get('end')}**　{p.get('title')}")
        for i in s["issues"]:
            md.append(f"- ⚠ {i}")
        for w in s.get("warnings", []):
            md.append(f"- ？{w}")
        md.append("")
    io.open(os.path.join(outdir, "paragraphs.md"), "w", encoding="utf-8").write("\n".join(md))


if __name__ == "__main__":
    raise SystemExit(main())

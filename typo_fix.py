# -*- coding: utf-8 -*-
"""错字纠正：全局规则 + 模型上报的「错词→正词」按行替换，并把正词补进热词表。

模型不重写原文，只在切章 / 切段的输出里顺带给出 typos：
    "typos": [{"wrong": "净败", "right": "敬拜", "lines": [248]}]
程序按行替换，行数不变，所以行号、gold、切分结果都不受影响。

校验（任一不过就丢弃该条，记进日志）：
  - wrong / right 非空、不相同、都不超过 10 字、长度差不超过 2
  - 不能只是删字或加字（right 包含于 wrong 或反之）：那是修口误（「并不能能」），不是识别错字
  - wrong 必须原样出现在它报的那一行（逐行判断，找不到的行单独丢弃）
  - wrong 本身是热词表里的词 → 不改（防止模型把正确的专名「改错」）
  - 同一行同一个 wrong 被报成两个不同的 right → 两条都丢弃

用法：
  python typo_fix.py normalize <原文.txt> <输出.txt>          # 只做全局规则（撒旦→撒但 等）
  python typo_fix.py apply <原文.txt> <结果.json>... --out <输出.txt> --log <日志.json> [--add-hotwords]
  python typo_fix.py hotwords-json [--groups 人名地名,66卷书名]  # 生成语音识别用的热词 JSON
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LEXICON = os.path.join(HERE, "lexicon", "热词表.txt")
RULES = os.path.join(HERE, "lexicon", "替换规则.tsv")
AUTO_GROUP = "自动补充"


# ------------------------------------------------------------------ 热词表与规则

def load_lexicon(path: str = LEXICON) -> dict[str, list[str]]:
    """按分组读热词表：{分组名: [词, ...]}，保持顺序。"""
    groups, cur = {}, None
    for ln in io.open(path, encoding="utf-8"):
        s = ln.strip()
        if s.startswith("## "):
            cur = s[3:].strip()
            groups.setdefault(cur, [])
        elif s and not s.startswith("#") and cur is not None:
            groups[cur].append(s)
    return groups


def all_words(groups: dict[str, list[str]] | None = None) -> list[str]:
    groups = groups or load_lexicon()
    seen, out = set(), []
    for ws in groups.values():
        for w in ws:
            if w not in seen:
                seen.add(w)
                out.append(w)
    return out


def load_rules(path: str = RULES) -> list[tuple[str, str]]:
    rules = []
    for ln in io.open(path, encoding="utf-8"):
        if ln.startswith("#") or not ln.strip():
            continue
        wrong, right = ln.rstrip("\n").split("\t")[:2]
        rules.append((wrong, right))
    return rules


def apply_rules(text: str, rules: list[tuple[str, str]] | None = None) -> str:
    for wrong, right in rules if rules is not None else load_rules():
        text = text.replace(wrong, right)
    return text


def add_hotwords(words: list[str], path: str = LEXICON) -> list[str]:
    """把不在热词表里的词追加到「自动补充」分组末尾，返回实际新增的词。"""
    have = set(all_words(load_lexicon(path)))
    new = [w for w in dict.fromkeys(words) if w not in have and 2 <= len(w) <= 8]
    if new:
        text = io.open(path, encoding="utf-8").read().rstrip("\n") + "\n" + "\n".join(new) + "\n"
        io.open(path, "w", encoding="utf-8").write(text)
    return new


# ------------------------------------------------------------------ 读原文（保留行号）

def read_lines(path: str) -> list[str]:
    lines = io.open(path, encoding="utf-8-sig").read().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def write_lines(path: str, lines: list[str]) -> None:
    io.open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


# ------------------------------------------------------------------ 收集与校验 typos

def collect_typos(result_paths: list[str]) -> list[dict]:
    """从切章结果（顶层 typos）和切段结果（sections[].typos）里收集词对，标上来源。"""
    out = []
    for p in result_paths:
        d = json.load(io.open(p, encoding="utf-8"))
        src = os.path.relpath(p, HERE)
        for t in d.get("typos") or []:
            out.append({**t, "source": src})
        for s in d.get("sections") or []:
            for t in s.get("typos") or []:
                out.append({**t, "source": src, "section": s.get("no")})
    return out


def check_pair(t: dict, protected: set[str]) -> str | None:
    w, r = str(t.get("wrong") or ""), str(t.get("right") or "")
    if not w or not r:
        return "wrong/right 为空"
    if w == r:
        return "wrong 与 right 相同"
    if len(w) > 10 or len(r) > 10:
        return "超过 10 字，不是词级错字"
    if abs(len(w) - len(r)) > 2:
        return "长度差超过 2，像是改写而不是纠错"
    if "\n" in w or "\n" in r:
        return "含换行"
    if r in w or w in r:
        return "只是删字或加字，像是在修口误（叠字、漏字），不是识别错字"
    if w in protected:
        return f"「{w}」本身在热词表里，不改"
    return None


def plan_fixes(lines: list[str], typos: list[dict], protected: set[str]) -> tuple[list[dict], list[dict]]:
    """逐条逐行校验，返回 (通过的替换, 丢弃的记录)。"""
    ok, rejected = [], []
    for t in typos:
        why = check_pair(t, protected)
        if why:
            rejected.append({**t, "reason": why})
            continue
        lns = t.get("lines") or ([t["line"]] if t.get("line") else [])
        if not lns:
            rejected.append({**t, "reason": "没有行号"})
            continue
        for n in lns:
            try:
                n = int(n)
            except (TypeError, ValueError):
                rejected.append({**t, "lines": [n], "reason": "行号不是整数"})
                continue
            if not 1 <= n <= len(lines):
                rejected.append({**t, "lines": [n], "reason": "行号越界"})
            elif t["wrong"] not in lines[n - 1]:
                rejected.append({**t, "lines": [n], "reason": "wrong 不在这一行"})
            else:
                ok.append({"line": n, "wrong": t["wrong"], "right": t["right"],
                           "source": t.get("source"), "section": t.get("section")})
    # 同一行同一个 wrong 有不同的 right：冲突，全丢
    by_key = {}
    for f in ok:
        by_key.setdefault((f["line"], f["wrong"]), set()).add(f["right"])
    conflict = {k for k, v in by_key.items() if len(v) > 1}
    final, seen = [], set()
    for f in ok:
        k = (f["line"], f["wrong"])
        if k in conflict:
            rejected.append({**f, "reason": f"同一行被报成不同的正词 {sorted(by_key[k])}"})
        elif k not in seen:          # 同一处被两层（切章、切段）重复上报，只换一次
            seen.add(k)
            final.append(f)
    return final, rejected


def apply_fixes(lines: list[str], fixes: list[dict]) -> list[str]:
    out = list(lines)
    for f in sorted(fixes, key=lambda x: -len(x["wrong"])):   # 长词先换，避免短词截断长词
        i = f["line"] - 1
        before = out[i]
        out[i] = out[i].replace(f["wrong"], f["right"])
        f["count"] = before.count(f["wrong"])
    return out


# ------------------------------------------------------------------ 命令行

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("normalize")
    n.add_argument("src")
    n.add_argument("dst")
    a_ = sub.add_parser("apply")
    a_.add_argument("src")
    a_.add_argument("results", nargs="+")
    a_.add_argument("--out", required=True)
    a_.add_argument("--log", required=True)
    a_.add_argument("--add-hotwords", action="store_true")
    h = sub.add_parser("hotwords-json")
    h.add_argument("--groups", default=None, help="逗号分隔的分组名，默认全部")
    a = ap.parse_args()

    if a.cmd == "normalize":
        lines = read_lines(a.src)
        rules = load_rules()
        fixed = [apply_rules(l, rules) for l in lines]
        changed = sum(x != y for x, y in zip(lines, fixed))
        write_lines(a.dst, fixed)
        print(f"全局规则 {len(rules)} 条，改动 {changed} 行 → {a.dst}")
        return 0

    if a.cmd == "hotwords-json":
        groups = load_lexicon()
        if a.groups:
            groups = {k: v for k, v in groups.items() if k in a.groups.split(",")}
        print(json.dumps({"hotwords": [{"word": w} for w in all_words(groups)]}, ensure_ascii=False))
        return 0

    lines = read_lines(a.src)
    protected = set(all_words())
    typos = collect_typos(a.results)
    fixes, rejected = plan_fixes(lines, typos, protected)
    fixed = apply_fixes(lines, fixes)
    assert len(fixed) == len(lines)
    write_lines(a.out, fixed)
    added = add_hotwords([f["right"] for f in fixes]) if a.add_hotwords else []
    io.open(a.log, "w", encoding="utf-8").write(json.dumps(
        {"src": os.path.relpath(a.src, HERE), "out": os.path.relpath(a.out, HERE),
         "reported": len(typos), "applied": fixes, "rejected": rejected, "hotwords_added": added},
        ensure_ascii=False, indent=2))
    print(f"上报 {len(typos)} 条 → 替换 {len(fixes)} 处，丢弃 {len(rejected)} 条；"
          f"热词表新增 {len(added)} 个{'：' + '、'.join(added) if added else ''}")
    for f in fixes:
        print(f"   行{f['line']}  {f['wrong']} → {f['right']}")
    for r in rejected:
        print(f"   丢弃 {r.get('wrong')}→{r.get('right')} 行{r.get('lines') or r.get('line')}：{r['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

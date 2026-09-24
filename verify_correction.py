# -*- coding: utf-8 -*-
"""校验复核者给出的「纠正版」。通过就以纠正版为准,不通过就重跑复核者,再不通过退回原版。

复核者输出:
  {"sections": [...纠正后的完整节...],
   "changes":  [...每一处改动...],
   "uncertain":[...]}

changes 的四种类型(全部用行号定位,不用节编号,避免重新编号后对不上):
  {"type":"move",   "line":176, "new_line":175, "evidence":"行175「…」——…", "confidence":0.9}
  {"type":"merge",  "line":37,                  "evidence":"行37「…」——…",  "confidence":0.9}
  {"type":"split",  "at":55,                    "evidence":"行55「…」——…",  "confidence":0.85}
  {"type":"retitle","start":115, "end":174, "problems":["遗漏"], "detail":"…"}
"""
import re

EVID = re.compile(r"行(\d+)「([^」]{2,40})」")


def evidence_ok(ev, lines):
    m = EVID.search(ev or "")
    if not m:
        return False
    n, frag = int(m.group(1)), m.group(2)
    return 1 <= n <= len(lines) and frag in lines[n - 1]


def verify_correction(orig, corr, changes, lines, lo, hi, min_conf=0.75):
    """返回 (是否通过, 问题列表)。"""
    errs = []
    o = sorted(orig, key=lambda x: x["start"])
    c = sorted(corr, key=lambda x: x["start"])

    # 1. 纠正版本身的结构
    if not c or c[0]["start"] != lo or c[-1]["end"] != hi:
        errs.append(f"纠正版没有覆盖整章 {lo}..{hi}")
    for a, b in zip(c, c[1:]):
        if a["end"] + 1 != b["start"]:
            errs.append(f"纠正版 {a['start']}-{a['end']} 与 {b['start']}-{b['end']} 断裂或重叠")
    for x in c:
        n = x["end"] - x["start"] + 1
        if n > 60 and hi - lo + 1 >= 10:
            errs.append(f"纠正版 {x['start']}-{x['end']} 仍有 {n} 行,超过 60")
        if not 25 <= len(x.get("title", "")) <= 50:
            errs.append(f"纠正版 {x['start']}-{x['end']} 标题 {len(x.get('title',''))} 字")

    # 2. 每条改动的证据
    for ch in changes:
        t = ch.get("type")
        if t in ("move", "merge", "split"):
            if not evidence_ok(ch.get("evidence"), lines):
                errs.append(f"{t} 的 evidence 对不上原文: {ch.get('evidence')}")
            if ch.get("confidence", 0) < min_conf:
                errs.append(f"{t} 置信度 {ch.get('confidence')} 低于 {min_conf},没把握就不该改")
        elif t == "retitle":
            if not ch.get("problems"):
                errs.append(f"retitle {ch.get('start')}-{ch.get('end')} 没写 problems")
        else:
            errs.append(f"未知的改动类型: {t}")

    # 3. 实际差异必须和 changes 一一对应
    bo = {x["start"] for x in o if x["start"] != lo}
    bc = {x["start"] for x in c if x["start"] != lo}
    removed, added = bo - bc, bc - bo
    say_removed, say_added = set(), set()
    for ch in changes:
        if ch.get("type") == "move":
            say_removed.add(ch.get("line")); say_added.add(ch.get("new_line"))
        elif ch.get("type") == "merge":
            say_removed.add(ch.get("line"))
        elif ch.get("type") == "split":
            say_added.add(ch.get("at"))
    if removed - say_removed:
        errs.append(f"删掉了边界 {sorted(removed - say_removed)} 却没有在 changes 里说明")
    if added - say_added:
        errs.append(f"新增了边界 {sorted(added - say_added)} 却没有在 changes 里说明")
    if say_removed - removed:
        errs.append(f"changes 说删掉了边界 {sorted(say_removed - removed)},纠正版里却还在")
    if say_added - added:
        errs.append(f"changes 说新增了边界 {sorted(say_added - added)},纠正版里却没有")

    # 4. 区间没变的节,标题要么原样照抄,要么有 retitle 说明
    o_by_rng = {(x["start"], x["end"]): x["title"] for x in o}
    retitled = {(ch.get("start"), ch.get("end")) for ch in changes if ch.get("type") == "retitle"}
    for x in c:
        rng = (x["start"], x["end"])
        if rng in o_by_rng and x["title"] != o_by_rng[rng] and rng not in retitled:
            errs.append(f"{rng[0]}-{rng[1]} 区间没变,标题却被改了,且没有 retitle 说明")

    # 5. 改动过半 = 在重切,不是在复核(超 60 行节的拆分不计入)
    long_secs = {(x["start"], x["end"]) for x in o if x["end"] - x["start"] + 1 > 60}
    def mandated(ch):
        return ch.get("type") == "split" and any(s < ch.get("at", 0) <= e for s, e in long_secs)
    free = [ch for ch in changes if ch.get("type") in ("move", "merge", "split") and not mandated(ch)]
    if len(free) > max(1, len(o) - 1) / 2:
        errs.append(f"非必要的结构改动 {len(free)} 处,超过边界数一半,像是在重切")

    return (not errs), errs


def pick_final(orig, run_reviewer, lines, lo, hi):
    """以纠正版为准的完整流程:复核 → 校验 → 不过就重跑一次 → 再不过就退回原版。"""
    for attempt in (1, 2):
        out = run_reviewer()
        ok, errs = verify_correction(orig, out["sections"], out.get("changes", []), lines, lo, hi)
        if ok:
            return out["sections"], f"采用纠正版(第 {attempt} 次)", out.get("changes", [])
        print(f"第 {attempt} 次纠正版未通过:", *errs, sep="\n  ")
    return orig, "两次纠正都未通过校验,退回切节原结果", []

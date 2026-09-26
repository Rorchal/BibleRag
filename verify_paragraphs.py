# -*- coding: utf-8 -*-
"""切段结果的机械校验。配合 prompts_切段_v1.md 使用。

用法:
    fatal, warns = verify_paragraphs(out, lines, lo, hi, sec_title)
    out   : 模型输出的 json(已 json.loads)
    lines : 全文按行的列表, lines[0] 是第 1 行
    lo,hi : 本节起止行号(绝对行号)
    sec_title: 本节标题(用来放行从节标题补全的专名和数字)

有 fatal 就重跑一次;再有就报人工。warns 只记录,留给复核者。
"""
import re

BANNED = ("讲员", "讲者", "讲道人", "讲道者", "牧师", "作者", "本段", "这段", "本节", "这一节", "这一段")
# 「作者」指书卷作者时放行
AUTHOR_OK = ("约伯记的作者", "经文的作者", "书卷的作者", "约伯记作者")
PERSON = ("我", "你", "咱")
ORAL = ("哈", "呃", "嘛", "咱")
CN_DIGIT = dict(zip("零一二三四五六七八九十", range(11)))


def _cn_to_int(s):
    """把「十一」「二十二」「九」这类中文数字转成整数,转不了返回 None。"""
    if not s or any(c not in CN_DIGIT for c in s):
        return None
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        if len(a) > 1 or len(b) > 1:  # 「一五一十」这类成语不是数字
            return None
        return (CN_DIGIT[a] if a else 1) * 10 + (CN_DIGIT[b] if b else 0)
    return int("".join(str(CN_DIGIT[c]) for c in s))


def _numbers_in(text):
    """文本里出现的所有数(阿拉伯数字 + 中文数字)。"""
    nums = {int(x) for x in re.findall(r"\d+", text)}
    for x in re.findall(r"[零一二三四五六七八九十]+", text):
        v = _cn_to_int(x)
        if v is not None:
            nums.add(v)
    return nums


def verify_paragraphs(out, lines, lo, hi, sec_title=""):
    fatal, warns = [], []
    paras = out.get("paragraphs") or []
    cuts = out.get("cuts") or []
    n_sec = hi - lo + 1

    # 1. 结构:cuts 与 paragraphs 一致、无缝覆盖
    if not paras:
        return ["没有输出任何段"], warns
    paras = sorted(paras, key=lambda p: p.get("start", 0))
    if sorted(cuts) != [p.get("start") for p in paras]:
        fatal.append(f"cuts {cuts} 与各段 start {[p.get('start') for p in paras]} 不一致")
    if paras[0]["start"] != lo:
        fatal.append(f"首段 start={paras[0]['start']},应为 {lo}")
    if paras[-1]["end"] != hi:
        fatal.append(f"末段 end={paras[-1]['end']},应为 {hi}")
    for a, b in zip(paras, paras[1:]):
        if a["end"] + 1 != b["start"]:
            fatal.append(f"段 {a['start']}-{a['end']} 与 {b['start']}-{b['end']} 断裂或重叠")

    # 2. 段长
    for p in paras:
        k = p["end"] - p["start"] + 1
        tag = f"段 {p['start']}-{p['end']}"
        if k < 3 and n_sec >= 6:
            fatal.append(f"{tag} 只有 {k} 行,应并入相邻段")
        elif k > 25:
            fatal.append(f"{tag} 有 {k} 行,超过 25 行必须拆开")
        elif k > 20:
            warns.append(f"{tag} 有 {k} 行,超过 20 行,可能漏切")
        elif k > 16:
            warns.append(f"{tag} 有 {k} 行,偏长")

    # 3. 标题与 ending
    src_ok = _numbers_in(sec_title)
    seen = []
    for p in paras:
        tag = f"段 {p['start']}-{p['end']}"
        t = (p.get("title") or "").strip()
        e = (p.get("ending") or "").strip()
        if not 20 <= len(t) <= 110:
            fatal.append(f"{tag} 标题 {len(t)} 字,不在 20-110 之间")
        if not e:
            warns.append(f"{tag} 缺 ending")
        t_chk = t
        for ok in AUTHOR_OK:
            t_chk = t_chk.replace(ok, "")
        for w in BANNED:
            if w in t_chk:
                fatal.append(f"{tag} 标题含「{w}」")
        outside = re.sub(r"「[^」]*」", "", t)          # 「」里的直接引语不查人称
        for w in PERSON:
            if w in outside:
                fatal.append(f"{tag} 标题含第一、二人称「{w}」")
                break
        if len(t) > 90:
            warns.append(f"{tag} 标题 {len(t)} 字,超过 90 字,可能在逐句复述")
        for w in ORAL:
            if w in t:
                warns.append(f"{tag} 标题含口语词「{w}」")
        if t.count("；") + t.count(";") >= 3 or t.count("、") >= 8:
            warns.append(f"{tag} 标题分号或顿号过多,可能是关键词堆砌")
        # 标题里的数字必须出自本段原文或节标题
        body = "".join(lines[i - 1] for i in range(p["start"], p["end"] + 1))
        extra = _numbers_in(t) - _numbers_in(body) - src_ok
        if extra:
            warns.append(f"{tag} 标题里的数字 {sorted(extra)} 在本段原文和节标题里都找不到")
        # 同节标题雷同
        for prev in seen:
            if t[:12] and t[:12] == prev[:12]:
                warns.append(f"{tag} 标题开头与前面某段相同,可能雷同")
        seen.append(t)

    return fatal, warns

# -*- coding: utf-8 -*-
"""v3 提示词（用户撰写的 SYSTEM + FEWSHOT，逐字保留）+ 运行器。

与 v1/v2 的结构差异：
- 章/节/段三级都带 start/end（v2 只有段带行号，章节靠嵌套隐含）
- 章带 trigger / evidence / confidence
- 顶层多一个 uncertain 数组
所以 flatten 不能复用 ds_client.flatten，这里单独解析。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import ds_client as ds
import prompts_v5
import prompts_v6
import prompts_v7
import prompts_v8

SYSTEM_V3A = """你是中文长文本结构分析器。输入是按行编号的口语转写稿(讲座/讲道/课程录音),一行一句。
你的任务:识别三级结构,输出每一级的起止行号。

## 铁律(违反即失败)
1. 只输出行号和你概括的标题。绝不复述、改写、摘抄原文。原文由调用方按行号取回。
2. 无缝覆盖:
   - 所有「章」必须连续覆盖 1..N,不重叠不留空,前一章 end+1 严格等于后一章 start
   - 一章内的所有「节」必须无缝覆盖该章区间
   - 一节内的所有「段」必须无缝覆盖该节区间
3. 输出合法 json,不带 markdown 代码围栏,json 之外不要有任何文字。

## 三级的定义(按判据切,不按感觉切)

### 章 —— 功能或场景的整体转换
一章 = 讲者在做一件性质不同的事。典型的章:开场问候 / 诵读经文 / 祷告 /
讲道第一部分 / 讲道第二部分 / 收尾 / 结束祷告。
判据:讲者通常会**显式宣告**("我们分两个部分""第二部分""我们一起来祷告""今天就到这里"),
或者言语行为整个变了(从讲解变成念经文,从论述变成祷告)。
数量:全篇通常 4-10 章。超过 12 章说明你把「节」当成「章」了。

### 节 —— 章内的论题单元
一节 = 在讨论同一个对象、回答同一个问题。
判据:分别给前后两块各拟一个标题,如果**主语或问题不同**,就是两节。
数量:每章 1-6 节。

### 段 —— 节内的论证步骤
一段 = 一个完整的论证动作:提出主张 / 展开论证 / 举一个例子 / 收束回到主张。
数量:每节 2-8 段。
**长度:每段不少于 3 行,不多于 60 行。**
  - 少于 3 行的,并入相邻段,不要单独成段——碎块在下游检索里没有价值
  - 多于 60 行的,说明你漏切了,再找一刀

## 过渡句归属规则(必须遵守,否则每次结果都不一样)
承上启下的句子("闲言少叙,我们看经文""好,那我们看第二部分")
一律**归入它引出的那一块的开头**,不归入上一块的结尾。
即:过渡句是新单元的第一行。

## 非边界(一律不切)
a 口语填充词开头:"所以""那""因为""但是""其实""你看""好"。
  这些每几行就出现一次,单独出现绝不构成边界。
b 修辞性设问:讲者自问自答。问句之后 1-3 行内自己给了答案的,属同一段。
c 口语重复与改口:"我再说一遍""换句话说""我再说哈"——是复述,不是新话题。
d 举例内部:一个完整的例子(从"我举个例子"到该例讲完)**不可分割**,
  哪怕内部出现"另外""还有一拨人""你信不信"也不许切开。
e 并列列举内部:"第一点…第二点…"若支撑同一论点,整体算一段。

## 最高原则:宁少勿多
漏切只是某块偏长;过切是把一个完整论证劈成两半,语义破坏不可恢复。
拿不准就不要切,把该行写进 uncertain,交给下游复核。

## 输出 json 格式
{
  "chapters": [
    {
      "no": 3,
      "start": 34, "end": 80,
      "title": "引言:讲道框架与说明",
      "trigger": "A",
      "evidence": "行34「我们就分两个部分来讲」—— 显式宣告",
      "confidence": 0.95,
      "sections": [
        {
          "no": "3.1", "start": 34, "end": 48, "title": "讲道分两部分",
          "paragraphs": [
            { "start": 34, "end": 36, "title": "分两个部分来讲" },
            { "start": 37, "end": 41, "title": "第一部分:1-5节约伯的现实背景" },
            { "start": 42, "end": 48, "title": "第二部分:6-8节约伯的属灵背景" }
          ]
        }
      ]
    }
  ],
  "uncertain": [
    { "line": 175, "reason": "有「我们看第一节」的宣告,但也可能只是节内的细节推进" }
  ]
}

字段说明:
- trigger: 仅「章」需要。取值 A(显式宣告)/ B(论题转换)/ C(功能转换)/ D(场景转换)
- evidence: 引用触发该章边界的**那一行的片段**(20字内)+ 一句为什么。
  这是唯一允许出现原文的字段,且只许引用边界行本身。
- confidence: 0-1。低于 0.7 的,同时写进 uncertain。
- title:
    用途是向量检索，不是给人读的摘要。因此：
  - 必须把段内用代词、口语指代的对象显式写出来（人名、地名、书卷章节、神学术语）
  - 直接陈述内容，不要出现"讲员""作者""本段"这类指代说话人的词：
      反例：讲员引用海德堡要理问答27问，说明护理包括荒年
      正例：海德堡要理问答27问指出，上帝的护理包括荒年、贫穷与疾病
- 25~50 字，书面语，不保留"哈""呃""啊"
- 只陈述原文说过的内容，不引申不评价"""

SYSTEM_V3 = SYSTEM_V3A  # 当前生效版本（title 规则已更新为检索导向）

FEWSHOT_V3 = """下面一组正误对照,来自同类型讲道稿。请严格照正确示范的粒度和字段来做。

━━━ 正确示范 ━━━
{
 "chapters":[{
  "no":3,"start":34,"end":80,"title":"引言:讲道框架与说明",
  "trigger":"A","evidence":"行34「我们就分两个部分来讲」—— 显式宣告","confidence":0.95,
  "sections":[
   {"no":"3.1","start":34,"end":48,"title":"讲道分两部分","paragraphs":[
     {"start":34,"end":36,"title":"分两个部分来讲"},
     {"start":37,"end":41,"title":"第一部分:1-5节约伯的现实背景"},
     {"start":42,"end":48,"title":"第二部分:6-8节约伯的属灵背景"}]},
   {"no":"3.2","start":49,"end":54,"title":"自我介绍与服侍任务","paragraphs":[
     {"start":49,"end":54,"title":"陈社炳与本年服侍方向"}]},
   {"no":"3.3","start":55,"end":67,"title":"为何选约伯记","paragraphs":[
     {"start":55,"end":58,"title":"今日信仰关注个人多于国度"},
     {"start":59,"end":63,"title":"约伯记用来消除顽固观念"},
     {"start":64,"end":67,"title":"智慧书第一卷,主题是智慧"}]},
   {"no":"3.4","start":68,"end":80,"title":"本讲要讨论的两个问题","paragraphs":[
     {"start":68,"end":73,"title":"问题一:存不存在绝对的平安"},
     {"start":74,"end":80,"title":"问题二:被神认可是否远离灾祸"}]}]}]
}

━━━ 错误示范(绝对不要输出这样的东西)━━━
[1-25] 讲员开场问候,宣读经文约伯记1:1-8,并带领祷告。
[26-48] 讲员介绍讲道大纲,分为两个部分:约伯的现实背景和属灵背景,并说明约伯是敬畏神的人。
[49-80] 讲员自我介绍,并说明服侍任务,强调约伯记是智慧书第一卷,并引出两个核心问题。

它错在四处,每一处都是硬性违规:
1. 没有层级。只有一层,章/节/段全糊成一坨。
2. 输出的是**摘要句**,不是结构标题。"讲员开场问候,宣读经文并带领祷告"
   是对内容的复述,不是一个可定位的标签。这直接违反铁律 1。
3. 一块内混了多种言语行为。[1-25] 把「寒暄」「诵读圣经原文」「祷告」塞进同一块——
   这三件事性质完全不同,必须是三个不同的**章**。
4. 边界划错位。[26-48] 把大纲预告和经文内容混在一起;[49-80] 把自我介绍、
   选书理由、两个问题三个不同论题压成一块。

记住:你的输出是**给程序用的索引**,不是给人读的摘要。"""

USER_TMPL_V3 = """{fewshot}

━━━ 待处理的转写稿 ━━━
共 {n} 行,行号 {lo}..{hi}。格式为「行号 | 内容」。

{numbered}

输出三级结构的 json。"""


def parse_v3(data: dict) -> dict:
    """解析 v3 三级结构，返回各级区间列表。"""
    chapters, sections, paragraphs = [], [], []
    for ch in data.get("chapters") or []:
        try:
            c = {"no": ch.get("no"), "start": int(ch["start"]), "end": int(ch["end"]),
                 "title": str(ch.get("title", "")).strip(),
                 "trigger": str(ch.get("trigger", "")).strip(),
                 "evidence": str(ch.get("evidence", "")).strip(),
                 "confidence": ch.get("confidence")}
        except (KeyError, TypeError, ValueError):
            continue
        chapters.append(c)
        subs = ch.get("sections") or []
        if not subs and (ch.get("paragraphs") or []):
            # FEWSHOT 的正确示范把 paragraphs 直接挂在 chapters 元素下，没有
            # sections 层。模型照抄示范时走这条分支：合成一个覆盖全章的节，
            # 并打上 synthesized 标记，避免整篇解析成 0 节 0 段。
            subs = [{"no": f"{c['no']}.1", "start": c["start"], "end": c["end"],
                     "title": c["title"], "paragraphs": ch["paragraphs"],
                     "_synthesized": True}]
        for se in subs:
            try:
                s = {"no": se.get("no"), "start": int(se["start"]), "end": int(se["end"]),
                     "title": str(se.get("title", "")).strip(), "chapter": c["no"],
                     "synthesized": bool(se.get("_synthesized"))}
            except (KeyError, TypeError, ValueError):
                continue
            sections.append(s)
            for p in se.get("paragraphs") or []:
                try:
                    paragraphs.append({"start": int(p["start"]), "end": int(p["end"]),
                                       "title": str(p.get("title", "")).strip(),
                                       "section": s["no"], "chapter": c["no"]})
                except (KeyError, TypeError, ValueError):
                    continue
    return {"chapters": chapters, "sections": sections, "paragraphs": paragraphs,
            "uncertain": data.get("uncertain") or []}


def check_seamless(items: list[dict], lo: int, hi: int, name: str) -> list[str]:
    """检查一级区间是否无缝覆盖 [lo, hi]。"""
    if not items:
        return [f"{name}: 空"]
    issues = []
    o = sorted(items, key=lambda x: (x["start"], x["end"]))
    if o[0]["start"] != lo:
        issues.append(f"{name}: 首块从 {o[0]['start']} 开始，应为 {lo}")
    if o[-1]["end"] != hi:
        issues.append(f"{name}: 末块到 {o[-1]['end']} 结束，应为 {hi}")
    for a, b in zip(o, o[1:]):
        if b["start"] != a["end"] + 1:
            issues.append(f"{name}: {a['end']} → {b['start']} "
                          f"（{'空洞' if b['start'] > a['end'] + 1 else '重叠'}）")
    for x in items:
        if x["start"] > x["end"]:
            issues.append(f"{name}: {x['start']}-{x['end']} 起止颠倒")
    return issues


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--effort", default="low", choices=["none", "low", "high"])
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--tag", default="v3")
    ap.add_argument("--prompt", default="v4", choices=["v4", "v5", "v6", "v7", "v8"])
    a = ap.parse_args()

    lines = ds.read_lines(a.path)
    lo, hi = 1, len(lines)
    numbered = "\n".join(f"{i} | {ln}" for i, ln in enumerate(lines, 1))
    system, fewshot = {
        "v4": (SYSTEM_V3, FEWSHOT_V3),
        "v5": (prompts_v5.SYSTEM_V5, prompts_v5.FEWSHOT_V5),
        "v6": (prompts_v6.SYSTEM_V6, prompts_v6.FEWSHOT_V6),
        "v7": (prompts_v7.SYSTEM_V7, prompts_v7.FEWSHOT_V7),
        "v8": (prompts_v8.SYSTEM_V8, prompts_v8.FEWSHOT_V8),
    }[a.prompt]
    user = USER_TMPL_V3.format(fewshot=fewshot, n=len(lines), lo=lo, hi=hi,
                               numbered=numbered)

    print(f"文件 {os.path.basename(a.path)}  {len(lines)} 行 / {len(''.join(lines))} 字")
    print(f"提示词 {a.prompt}（你的 SYSTEM + FEWSHOT），reasoning_effort={a.effort}，"
          f"temperature={a.temperature}")
    print("调用中（单次，不重试）…", flush=True)

    content, meta = ds.chat(
        system, user, max_tokens=a.max_tokens, temperature=a.temperature,
        reasoning_effort=None if a.effort == "none" else a.effort,
        timeout=600,
        ctx={"file": os.path.basename(a.path), "tag": a.tag, "effort": a.effort})

    u = meta.get("usage", {})
    print(f"\n耗时 {meta.get('secs')}s  finish={meta.get('finish_reason')}")
    print(f"usage: prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} "
          f"reasoning={u.get('reasoning_tokens')}")
    if content is None:
        print(f"【失败】{meta.get('error')}（已记录，不重试）")
        return 2
    if meta.get("truncated"):
        print("【警告】被 max_tokens 截断")

    data = ds.parse_json(content)
    if data is None:
        ds.log_failure("json_parse_failed", content, {"file": os.path.basename(a.path)})
        print("【失败】返回非合法 JSON，前 400 字：")
        print(content[:400])
        return 3

    st = parse_v3(data)
    print(f"\n章 {len(st['chapters'])}  节 {len(st['sections'])}  "
          f"段 {len(st['paragraphs'])}  uncertain {len(st['uncertain'])}")

    print("\n--- 无缝覆盖校验 ---")
    issues = check_seamless(st["chapters"], lo, hi, "章")
    issues += check_seamless(st["paragraphs"], lo, hi, "段")
    for c in st["chapters"]:
        subs = [s for s in st["sections"] if s["chapter"] == c["no"]]
        issues += check_seamless(subs, c["start"], c["end"], f"章{c['no']}内的节")
    for s in st["sections"]:
        subs = [p for p in st["paragraphs"] if p["section"] == s["no"]]
        issues += check_seamless(subs, s["start"], s["end"], f"节{s['no']}内的段")
    if issues:
        print(f"发现 {len(issues)} 处问题：")
        for m in issues[:20]:
            print("  -", m)
        if len(issues) > 20:
            print(f"  …另有 {len(issues) - 20} 处")
    else:
        print("三级全部无缝覆盖，无问题")

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output",
                        f"{a.tag}_{a.prompt}_{a.effort}_"
                        f"{os.path.splitext(os.path.basename(a.path))[0]}")
    io.open(base + ".raw.json", "w", encoding="utf-8").write(content)
    # 提示词版本写进 meta：文件名可能被改，meta 不会，评测时据此认人
    meta["prompt"] = a.prompt
    meta["tag"] = a.tag
    io.open(base + ".parsed.json", "w", encoding="utf-8").write(
        json.dumps({"meta": meta, "issues": issues, **st}, ensure_ascii=False, indent=2))
    print(f"\n输出：{base}.raw.json / .parsed.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""一篇讲道的完整流水线：先把错字一次纠正完，再在纠正后的原文上切 章 → 节 → 段。

  第 1 步  检验错字    全局规则（lexicon/替换规则.tsv，如 撒旦→撒但）先替换已知的错字；
                       再把 热词表 + 本篇经文短语 + 全文 交给模型（prompts/校对_v1.md），
                       只报「错词→正词」和行号，不重写原文
  第 2 步  加进热词表  校验通过的词对，正词追加到 lexicon/热词表.txt 的「自动补充」分组（人工复核后再挪进正式分组）
  第 3 步  更新文本    按行替换，行数不变 → 校对后原文
  第 4 步  切章        在校对后原文上切（prompts/切章.md）
  第 5 步  切节        同一份原文，逐章切（prompts/切节_v2.md）
  第 6 步  切段        同一份原文，逐章切（prompts/切段_v4.md）
  第 7 步  导出        切分结果/<名字>/：切分效果.md、结构.json、校对后原文.txt、纠错记录.json

切分的三步都不再报错字，看到的都是同一份校对后原文。每一步只调一次模型、不重试。
第 2、3 步由 typo_fix.py apply 一条命令完成（先校验，再补热词，再替换）。

中间文件在 output/work/<名字>/：
  0_规则.txt    原文经全局规则替换
  1_校对后.txt  再按第 1 步模型上报的词对替换 —— 第 4~6 步都用这一份

用法：
    export DEEPSEEK_API_KEY=<密钥>
    python pipeline.py 识别结果/豆包2.0/GH_约伯记2章1到6节_热词识别.txt --name GH_约伯记2章1到6节 --effort low
    python pipeline.py ... --only-proofread      # 只做第 1~3 步，先看错字清单
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

import typo_fix

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(args: list[str], env: dict) -> None:
    print("\n$", " ".join(os.path.relpath(x, HERE) if os.path.isabs(x) else x for x in args), flush=True)
    r = subprocess.run([PY, *args], cwd=HERE, env=env)
    if r.returncode not in (0, 2):          # 2 = 有问题但有结果，继续
        raise SystemExit(f"步骤失败（退出码 {r.returncode}）：{args[0]}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("txt")
    ap.add_argument("--name", required=True, help="讲道名，如 GH_约伯记2章1到6节（也用来认出所讲经文）")
    ap.add_argument("--effort", default="low", choices=["low", "high", "max"])
    ap.add_argument("--proofread-prompt", default="校对_v1")
    ap.add_argument("--chapter-prompt", default="切章")
    ap.add_argument("--para-prompt", default="v4")
    ap.add_argument("--only-proofread", action="store_true", help="只做第 1~3 步（检验错字、补热词、更新文本）")
    a = ap.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DS_KEY")
    if not key:
        raise SystemExit("缺少环境变量 DEEPSEEK_API_KEY")
    env = {**os.environ, "DEEPSEEK_API_KEY": key, "DS_KEY": key, "DS_MODEL": "deepseek-flash",
           "DS_BASE": "https://api.deepseek.com/chat/completions", "PYTHONIOENCODING": "utf-8"}

    work = os.path.join(HERE, "output", "work", a.name)
    os.makedirs(work, exist_ok=True)
    t0 = os.path.join(work, "0_规则.txt")
    t1 = os.path.join(work, "1_校对后.txt")
    log = os.path.join(work, "纠错_校对.json")
    tag = a.name.replace("GH_", "")

    # 第 1 步：检验错字（全局规则 + 模型校对）
    run(["typo_fix.py", "normalize", a.txt, t0], env)
    p = typo_fix.parse_passage(a.name) or typo_fix.parse_passage(os.path.basename(a.txt))
    passage = ["--passage", f"{p[0]}:{p[1]}-{p[2]}:{p[3]}"] if p else []
    run(["correct_typos.py", t0, "--tag", tag, "--prompt", a.proofread_prompt, "--effort", a.effort, *passage], env)
    typos = os.path.join(HERE, "output", f"{tag}_typos_0_规则.json")

    # 第 2、3 步：校验词对 → 正词补进热词表 → 按行替换，得到校对后原文
    run(["typo_fix.py", "apply", t0, typos, "--out", t1, "--log", log, "--add-hotwords"], env)
    if a.only_proofread:
        print(f"\n只做了校对：校对后原文 {os.path.relpath(t1, HERE)}，纠错记录 {os.path.relpath(log, HERE)}")
        return 0

    # 第 4~6 步：在校对后原文上切 章 → 节 → 段
    run(["cut_chapters.py", t1, "--tag", tag, "--prompt", a.chapter_prompt, "--effort", a.effort], env)
    ch = os.path.join(HERE, "output", f"{tag}_ch_1_校对后.parsed.json")
    run(["cut_se_v2.py", t1, "--mode", "chapter", "--chapters-json", ch, "--tag", tag, "--effort", a.effort], env)
    se = os.path.join(HERE, "output", f"{tag}_se_v2_chapter_1_校对后.parsed.json")
    pdir = f"paras_{a.para_prompt}_{tag}"
    run(["seg_paras.py", "--prompt", a.para_prompt, "--effort", a.effort, "--model", "deepseek-flash",
         "--txt", t1, "--sections", se, "--name", pdir], env)

    # 第 7 步：导出
    run(["export_result.py", "--txt", t1, "--sections", se, "--paras", os.path.join(HERE, "output", pdir),
         "--name", a.name], env)
    out = os.path.join(HERE, "切分结果", a.name)
    shutil.copy(t1, os.path.join(out, "校对后原文.txt"))
    shutil.copy(log, os.path.join(out, "纠错记录.json"))
    print(f"\n完成 → {os.path.relpath(out, HERE)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""一篇讲道的完整流水线：全局规则 → 切章(+错字) → 纠错 → 切节 → 切段(+错字) → 纠错 → 导出。

每一步只调一次模型、不重试；纠错全部由 typo_fix.py 按行替换，行数不变，前后各步的行号一致。

  0_规则.txt        原文经 lexicon/替换规则.tsv 全局替换（撒旦→撒但）
  1_切章纠错.txt    再按切章上报的 typos 替换 —— 切节、切段都用这一份
  2_切段纠错.txt    再按切段上报的 typos 替换 —— 最终的校对后原文

中间文件在 output/work/<名字>/，成品导出到 切分结果/<名字>/（含 校对后原文.txt、纠错记录.json）。

用法：
    export DEEPSEEK_API_KEY=<密钥>
    python pipeline.py 识别结果/豆包2.0/GH_约伯记2章1到6节_热词识别.txt --name GH_约伯记2章1到6节 --effort low
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys

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
    ap.add_argument("--name", required=True, help="讲道名，如 GH_约伯记2章1到6节")
    ap.add_argument("--effort", default="low", choices=["low", "high", "max"])
    ap.add_argument("--chapter-prompt", default="切章_v2")
    ap.add_argument("--para-prompt", default="v5")
    a = ap.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DS_KEY")
    if not key:
        raise SystemExit("缺少环境变量 DEEPSEEK_API_KEY")
    env = {**os.environ, "DEEPSEEK_API_KEY": key, "DS_KEY": key, "DS_MODEL": "deepseek-flash",
           "DS_BASE": "https://api.deepseek.com/chat/completions", "PYTHONIOENCODING": "utf-8"}

    work = os.path.join(HERE, "output", "work", a.name)
    os.makedirs(work, exist_ok=True)
    t0 = os.path.join(work, "0_规则.txt")
    t1 = os.path.join(work, "1_切章纠错.txt")
    t2 = os.path.join(work, "2_切段纠错.txt")
    tag = a.name.replace("GH_", "")

    run(["typo_fix.py", "normalize", a.txt, t0], env)

    run(["cut_chapters.py", t0, "--tag", tag, "--prompt", a.chapter_prompt, "--effort", a.effort], env)
    ch = os.path.join(HERE, "output", f"{tag}_ch_0_规则.parsed.json")
    run(["typo_fix.py", "apply", t0, ch, "--out", t1, "--log", os.path.join(work, "纠错_切章.json"),
         "--add-hotwords"], env)

    run(["cut_se_v2.py", t1, "--mode", "chapter", "--chapters-json", ch, "--tag", tag, "--effort", a.effort], env)
    se = os.path.join(HERE, "output", f"{tag}_se_v2_chapter_1_切章纠错.parsed.json")

    pdir = f"paras_{a.para_prompt}_{tag}"
    run(["seg_paras.py", "--prompt", a.para_prompt, "--effort", a.effort, "--model", "deepseek-flash",
         "--txt", t1, "--sections", se, "--name", pdir], env)
    pj = os.path.join(HERE, "output", pdir, "parsed.json")
    run(["typo_fix.py", "apply", t1, pj, "--out", t2, "--log", os.path.join(work, "纠错_切段.json"),
         "--add-hotwords"], env)

    run(["export_result.py", "--txt", t2, "--sections", se, "--paras", os.path.join(HERE, "output", pdir),
         "--name", a.name], env)
    out = os.path.join(HERE, "切分结果", a.name)
    shutil.copy(t2, os.path.join(out, "校对后原文.txt"))
    logs = {k: json.load(io.open(os.path.join(work, f"纠错_{k}.json"), encoding="utf-8")) for k in ("切章", "切段")}
    io.open(os.path.join(out, "纠错记录.json"), "w", encoding="utf-8").write(
        json.dumps(logs, ensure_ascii=False, indent=2))
    n = {k: len(v["applied"]) for k, v in logs.items()}
    print(f"\n完成：切章纠错 {n['切章']} 处，切段纠错 {n['切段']} 处 → {os.path.relpath(out, HERE)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

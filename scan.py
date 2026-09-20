# -*- coding: utf-8 -*-
"""
定时增量扫描讲道转写稿目录，对新增/变更的文件做语义排版。

规则：
  - 目标文件由正则 GH_约伯记.*热词识别.txt 决定（严格版见 PATTERN）
  - 「增量」= 正则命中、且（从未处理过 或 内容哈希变了）的文件
  - 空文件（无非空行）不算增量、也不记入已处理，下个周期继续检查
  - 本周期无增量 → 打印一行跳过，不做任何处理，等下个周期
  - --hours 到期后自动退出（默认 24 小时）

用法：
  python scan.py --once                      # 扫一次
  python scan.py --watch                     # 每 15 分钟一轮，跑满 24 小时后退出
  python scan.py --watch --interval 600 --hours 24
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import segment

HERE = os.path.dirname(os.path.abspath(__file__))
# 默认指向仓库内的转写稿目录；数据若移到别处，用 SERMON_SRC 覆盖。
# （旧默认值是一台特定 Windows 机器的绝对路径，在其他机器上必然扫空目录）
SRC_DIR = os.environ.get("SERMON_SRC", os.path.join(HERE, "识别结果", "豆包2.0"))
# 旧正则 ^GH_约伯记\d+章\d+到\d+节_热词识别\.txt$ 只命中 32 篇里的 29 篇：
# 跨章的「6章28节到7章2节」和用简称+日期的「伯1章1到8节20260104」都被静默跳过。
PATTERN = re.compile(r"^GH_.*_热词识别\.txt$")
# 长得像转写稿、但没被 PATTERN 命中的，记一行日志，避免再出现静默丢篇
LOOSE = re.compile(r"^(?!.*_带时间\.txt$).*_热词(识别|测试)\.txt$")
STATE = os.path.join(HERE, "state", "seen.json")
OUT_DIR = os.path.join(HERE, "output")
LOG = os.path.join(HERE, "state", "scan.log")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if os.path.exists(STATE):
        try:
            return json.load(io.open(STATE, encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"状态文件损坏，重建：{STATE}")
    return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    io.open(STATE, "w", encoding="utf-8").write(
        json.dumps(st, ensure_ascii=False, indent=2, sort_keys=True))


def sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def find_increments(state: dict) -> tuple[list[str], list[str]]:
    """返回 (待处理文件名, 被跳过的空文件名)。"""
    todo, empty = [], []
    if not os.path.isdir(SRC_DIR):
        log(f"目标目录不存在：{SRC_DIR}")
        return todo, empty
    unmatched = []
    for name in sorted(os.listdir(SRC_DIR)):
        if not PATTERN.match(name):
            if LOOSE.match(name):
                unmatched.append(name)
            continue
        path = os.path.join(SRC_DIR, name)
        if not os.path.isfile(path):
            continue
        # 空文件不算增量，也不记状态，下轮继续看
        if os.path.getsize(path) == 0 or not segment.read_lines(path):
            empty.append(name)
            continue
        digest = sha1(path)
        prev = state.get(name)
        if prev and prev.get("sha1") == digest:
            continue
        todo.append(name)
    if unmatched:
        log(f"长得像转写稿但未被 PATTERN 命中，未处理：{', '.join(unmatched)}")
    return todo, empty


def process_one(name: str, window: int, max_titles: int) -> dict:
    path = os.path.join(SRC_DIR, name)
    res = segment.process(path, "index", window, None, max_titles)
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, re.sub(r"\.txt$", "", name) + ".md")
    io.open(out, "w", encoding="utf-8").write(res["markdown"])
    io.open(out + ".report.json", "w", encoding="utf-8").write(
        json.dumps({k: v for k, v in res.items() if k != "markdown"},
                   ensure_ascii=False, indent=2))
    res["out"] = out
    return res


def one_round(window: int, max_titles: int) -> int:
    state = load_state()
    todo, empty = find_increments(state)
    if empty:
        log(f"空文件跳过（不计为增量，下轮再查）：{', '.join(empty)}")
    if not todo:
        log("本轮无增量，跳过。")
        return 0

    log(f"发现增量 {len(todo)} 个：{', '.join(todo)}")
    done = 0
    for name in todo:
        log(f"→ 处理 {name}")
        try:
            res = process_one(name, window, max_titles)
        except Exception as e:  # 单个文件失败不拖垮整轮，状态不落盘，下轮重试
            log(f"  失败：{type(e).__name__}: {e}")
            continue
        ok = res["chunks_ok"] == res["chunks"]
        log(f"  完成 {res['total_lines']}行 / {res['chunks']}窗口 "
            f"校验{res['chunks_ok']}/{res['chunks']} 耗时{res['elapsed_s']}s → {res['out']}")
        if ok:
            path = os.path.join(SRC_DIR, name)
            state[name] = {
                "sha1": sha1(path),
                "size": os.path.getsize(path),
                "processed_at": datetime.now().isoformat(timespec="seconds"),
                "lines": res["total_lines"],
                "out": os.path.basename(res["out"]),
            }
            save_state(state)
            done += 1
        else:
            log("  逐字校验未全通过，不记入已处理，下轮重试。")
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="只扫一轮")
    g.add_argument("--watch", action="store_true", help="循环扫描直到有效期结束")
    ap.add_argument("--interval", type=int, default=900, help="轮询间隔秒，默认 900")
    ap.add_argument("--hours", type=float, default=24.0, help="有效期小时数，默认 24")
    ap.add_argument("--window", type=int, default=80)
    ap.add_argument("--max-titles", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true", help="只列出本轮会处理哪些文件，不调模型")
    a = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    if a.dry_run:
        todo, empty = find_increments(load_state())
        log(f"正则 {PATTERN.pattern}")
        log(f"空文件跳过：{empty or '无'}")
        log(f"本轮增量 {len(todo)} 个：{todo or '无（跳过，下轮继续）'}")
        return 0

    if not a.watch:
        one_round(a.window, a.max_titles)
        return 0

    deadline = datetime.now() + timedelta(hours=a.hours)
    log(f"=== 开始定时扫描：每 {a.interval}s 一轮，有效期至 {deadline:%Y-%m-%d %H:%M:%S} ===")
    log(f"目标目录 {SRC_DIR}")
    log(f"正则 {PATTERN.pattern}")
    rnd = 0
    while datetime.now() < deadline:
        rnd += 1
        log(f"--- 第 {rnd} 轮 ---")
        one_round(a.window, a.max_titles)
        if datetime.now() + timedelta(seconds=a.interval) >= deadline:
            break
        time.sleep(a.interval)
    log(f"=== 有效期结束，共 {rnd} 轮，退出 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

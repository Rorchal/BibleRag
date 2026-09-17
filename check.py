# -*- coding: utf-8 -*-
"""对已生成的 markdown 做全篇逐字校验 + 结构统计。用法: python check.py <原始txt> <生成md>"""
import io
import statistics
import sys

from segment import norm, read_lines, strip_titles

sys.stdout.reconfigure(encoding="utf-8")
src, gen = sys.argv[1], sys.argv[2]

md = io.open(gen, encoding="utf-8").read()
body, titles = strip_titles(md)
orig = norm("".join(read_lines(src)))

print("=== 全篇逐字校验 ===")
same = norm(body) == orig
print(f"原文 {len(orig)} 字 / 输出正文 {len(norm(body))} 字  =>  {'完全一致' if same else '不一致'}")
if not same:
    from segment import first_diff
    print(first_diff(orig, norm(body)))

paras = [p for p in body.split("\n\n") if p.strip()]
L = [len(p) for p in paras]
print("\n=== 结构统计 ===")
print(f"段落 {len(paras)} 个，标题 {len(titles)} 个")
print(f"段长 min/中位/均值/max: {min(L)} / {statistics.median(L)} / {round(statistics.mean(L))} / {max(L)}")
print(f"平均每 {len(paras) / max(len(titles), 1):.1f} 段一个标题")
print("\n=== 全部标题 ===")
for i, t in enumerate(titles, 1):
    print(f"{i:2d}. {t}")

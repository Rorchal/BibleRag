# -*- coding: utf-8 -*-
"""发一次 DeepSeek 调用，把 HTTP 响应原样落盘，不做任何加工。

用途：让人肉眼检查接口到底返回了什么（含外层信封、usage、finish_reason 等）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.request

import compare as cmp
import ds_client as ds


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--variant", choices=["user", "mine"], default="mine")
    ap.add_argument("--lines", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--out", default="output/raw_response.json")
    a = ap.parse_args()

    all_lines = ds.read_lines(a.path)
    lines = all_lines if a.lines == 0 else all_lines[:a.lines]
    lo, hi = 1, len(lines)
    numbered = "\n".join(f"{i} | {ln}" for i, ln in enumerate(lines, 1))
    label, system = cmp.VARIANTS[a.variant]

    payload = {
        "model": ds.MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": cmp.USER_TMPL.format(
                n=len(lines), lo=lo, hi=hi, numbered=numbered)},
        ],
        "temperature": 0.2,
        "max_tokens": a.max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # 请求体也一起落盘，方便核对到底发出去了什么
    req_path = os.path.splitext(a.out)[0] + ".request.json"
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    io.open(req_path, "wb").write(body)

    print(f"POST {ds.BASE}")
    print(f"model={ds.MODEL}  variant={a.variant}（{label}）  "
          f"输入 {len(lines)} 行 / 请求体 {len(body)} 字节")

    req = urllib.request.Request(ds.BASE, data=body, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + ds.KEY,
    })
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        status = r.status
        headers = dict(r.headers)
        raw_bytes = r.read()          # 原始字节，未经任何处理
    secs = round(time.time() - t0, 1)

    io.open(a.out, "wb").write(raw_bytes)

    print(f"\nHTTP {status}  {secs}s  响应体 {len(raw_bytes)} 字节")
    print("响应头：")
    for k, v in headers.items():
        print(f"  {k}: {v}")
    print(f"\n原始响应体已落盘：{a.out}")
    print(f"本次请求体已落盘：{req_path}")

    # 只解析一次用于展示信封字段，不改动落盘内容
    try:
        obj = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"\n（响应体无法解析为 JSON：{e}，请直接看文件）")
        return 0

    print("\n=== 响应信封（content 字段之外的全部内容）===")
    env = json.loads(json.dumps(obj))
    for ch in env.get("choices", []):
        msg = ch.get("message", {})
        if isinstance(msg.get("content"), str):
            msg["content"] = f"<<{len(msg['content'])} 字符，见下方或文件>>"
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

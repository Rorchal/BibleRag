# BibleRag

讲道录音转写稿的**结构化流水线**：把豆包语音识别产出的「一句一行」纯文本，
交给大/小模型切分成「章 / 节 / 段」三级结构，并对结果做逐字校验与量化评测。

核心约束（贯穿全部脚本）：**模型只输出行号和标题，正文一律由脚本按行号从原文拼回**，
所以模型在结构上不可能改动一个字；每次产出都会做「拼回后与原文逐字相同」的校验。

详细的目录审计、冗余量化与清理建议见 [`docs/目录结构梳理.md`](docs/目录结构梳理.md)。

---

## 目录结构

仓库分两层：**根目录是代码（约 100 KB，扁平放置）**，`识别结果/` 是数据（271 MB）。

```
BibleRag/
├── README.md
├── .gitignore
├── docs/
│   ├── 目录结构梳理.md          # 目录审计报告 + 清理建议
│   ├── v6提示词一致性问题.md    # v6 提示词内部三处冲突的复核
│   ├── v6评测结果.md            # v6 首次实跑 + 对 gold_v2 的评测
│   └── v6_bad_cases.md          # v6 实跑 bad case 台账（只记录，不改提示词）
│
├── ds_client.py                 # ★ 公共库：DeepSeek 网关客户端 + 结构校验 + 失败落盘
├── prompts_v5.py                # ★ 公共库：v5 提示词常量（被 v3.py 引用）
├── prompts_v6.py                # ★ 公共库：v6 提示词常量（被 v3.py 引用）
├── segment.py                   # ★ 公共库 + 入口：本地 Ollama 小模型排版
│
├── scan.py                      # 入口：定时增量扫描转写稿目录 → 调 segment 排版
├── check.py                     # 入口：对已生成 md 做全篇逐字校验 + 结构统计
├── trial.py                     # 入口：单篇试跑「章/节/段 + 概要」
├── compare.py                   # 入口：A/B 对比两版提示词（user / mine）
├── noise.py                     # 入口：同参数连跑 N 次，量化重跑方差 + 边界投票
├── v3.py                        # 入口：v4/v5/v6 提示词运行器（三级都带行号）
├── goldeval.py                  # 入口：以豆包结构为 gold，算边界 P/R/F1
├── raw_call.py                  # 入口：发一次调用，HTTP 响应原样落盘（排查用）
│
├── output/                      # 所有脚本的产物（41 个文件，1.2 MB），按 --tag 前缀区分
├── logs/
│   └── api_failures.jsonl       # 调用失败记录（只记录，不重试）
├── state/
│   ├── seen.json                # scan.py 的增量游标（文件名 → 内容哈希）
│   └── scan.log                 # scan.py 运行日志
├── probe/                       # 4 个手写的最小请求体样例，脚本未引用
│
└── 识别结果/                     # 语音识别原始数据，271 MB
    ├── 官方示例.{json,txt}
    └── 豆包2.0/                  # 32 篇约伯记讲道，每篇 8 个文件 + 1 个目录
        ├── <篇名>.txt            # ← 唯一的流水线输入：一句一行的纯文本
        ├── <篇名>.srt            # 带时间轴字幕
        ├── <篇名>.json           # 接口完整返回（缩进版）
        ├── <篇名>.raw.json       # 接口完整返回（紧凑版，内容同上）
        ├── <篇名>.segments.json  # 按句切分 + 毫秒时间戳
        ├── <篇名>.task.json      # 提交任务的元信息（request_id / audio_url / 热词）
        ├── <篇名>_带时间.txt     # 人读版：时间 + 文本
        ├── <篇名>_时间对照.md    # 人读版：markdown 时间对照表
        └── <篇名>_接口返回/      # 提交与轮询的每一次 HTTP 响应逐条落盘
```

`★` 标记的三个文件同时是被 import 的库；其余脚本都只作为命令行入口使用。

### 模块依赖

```
ds_client.py ──┬── trial.py
               ├── compare.py ──┬── noise.py
               │                └── raw_call.py
               ├── goldeval.py
               └── v3.py ── prompts_v5.py

segment.py ────┬── scan.py
               └── check.py
```

`ds_client.py` 是唯一的扇入中心（5 个脚本依赖）；`segment.py` 自成一条本地小模型支线。
两条支线目前没有交汇点：`ds_client` 走远程 DeepSeek 网关，`segment` 走本机 Ollama。

---

## 数据流

```
音频 ──(豆包语音识别，仓库外)──> 识别结果/豆包2.0/<篇名>.txt
                                        │
              ┌─────────────────────────┴──────────────────────────┐
              │                                                    │
    scan.py（增量扫描，定时）                          trial/compare/noise/v3（手工试验）
              │ 调 segment.process(index)                          │ 调 ds_client.chat
              ↓ 本机 Ollama qwen3:8b                               ↓ 远程 DeepSeek-v4-flash
      output/<篇名>.md                                    output/<tag>_<篇名>.{md,raw.json,parsed.json}
              │                                                    │
              ↓                                                    ↓
      check.py 逐字校验 + 结构统计                    goldeval.py 对 output/gold*.md 算 P/R/F1
```

---

## 运行方式

```bash
# 单篇排版（本地 Ollama，index 模式=只出行号，正文脚本拼回）
python segment.py 识别结果/豆包2.0/GH_约伯记2章11到13节_热词识别.txt --mode index

# 增量扫描：扫一次 / 每 15 分钟一轮跑满 24 小时
python scan.py --once
python scan.py --watch --interval 900 --hours 24

# 校验产物
python check.py 识别结果/豆包2.0/<篇名>.txt output/<篇名>.md

# 远程 DeepSeek：单篇试跑 / A-B 对比 / 噪声基线
python trial.py <txt> --max-tokens 16000 --tag full2
python compare.py <txt> --tag ab1
python noise.py <txt> --runs 5 --variant mine

# v6 提示词跑约伯记 1 章 1-8 节（输出落 output/v6_v6_none_<篇名>.{raw,parsed}.json）
# 下面是 2026-09-20 实际跑通的那一条：官方端点 + deepseek-flash + 64000 token
export DS_BASE=https://api.deepseek.com/chat/completions
export DS_KEY=<你的官方平台密钥>       # sk- 开头
export DS_MODEL=deepseek-flash
python v3.py 识别结果/豆包2.0/GH_伯1章1到8节20260104_热词识别.txt \
  --prompt v6 --effort none --max-tokens 64000 --tag v6

# 评测：gold 为 output/gold_v2.md（8 章 / 28 节 / 83 段，覆盖行 1-969）
# --pred 必须写成「标签=路径」，可一次传多个做横向对比
python goldeval.py --gold output/gold_v2.md \
  --pred v6=output/v6_v6_none_GH_伯1章1到8节20260104_热词识别.parsed.json \
         v5_low=output/v5_low_GH_伯1章1到8节20260104_热词识别.parsed.json \
         v5_none=output/v5_none_GH_伯1章1到8节20260104_热词识别.parsed.json \
         v4_none=output/v4_none_GH_伯1章1到8节20260104_热词识别.parsed.json \
         v3_low=output/v3_low_GH_约伯记1章1到8节_热词识别.parsed.json \
         v3_none=output/v3_none_GH_约伯记1章1到8节_热词识别.parsed.json \
  --txt 识别结果/豆包2.0/GH_伯1章1到8节20260104_热词识别.txt
```

六个方案（v3/v4/v5/v6）对 `gold_v2.md` 的完整对照表在
[`output/gold_v2_baseline.txt`](output/gold_v2_baseline.txt)，结果解读见
[`docs/v6评测结果.md`](docs/v6评测结果.md)。

> 注意：v6 那一行跑在**官方 `api.deepseek.com` 的 `deepseek-flash`** 上，其余五行跑在
> **微信网关的 `Deepseek-v4-flash`** 上。两者是否同一模型无从证实，所以 v6 与其余行的
> 差异里，提示词的贡献和模型的贡献目前分不开。

> **跑之前注意 max_tokens，不只是 `--effort`。** `logs/api_failures.jsonl` 里的
> `empty_content` 都是同一个失败：模型把预算全烧在 `reasoning_content` 里，`content`
> 返回空串，`ds_client` 判定失败且**不重试**。
>
> 早先两条发生在网关的 `effort=low` 下，所以曾以为 `--effort none` 就能躲开。2026-09-20
> 实测**不能**：官方端点的 `deepseek-flash` 默认就推理（一个 "回复：OK" 的请求也产生
> 19 个 reasoning token），`--effort none` 只是不发 `reasoning_effort` 参数，拦不住推理。
> v6 在 `--effort none --max-tokens 16000` 下照样 `finish=length` 空返回，提到 64000 后一次通过。
>
> 结论：无论哪个 effort，这篇 969 行稿子都给到 **32000 以上**再跑。详见 `docs/v6评测结果.md`。

## 环境变量

| 变量 | 用处 | 默认值 |
| --- | --- | --- |
| `DS_BASE` | DeepSeek 网关地址 | `https://chatapi.weixin.qq.com/openai/v1/chat/completions` |
| `DS_KEY` | 网关密钥 | **必填**，无默认值；缺失时 `chat()` 抛错。旧的硬编码密钥已移除，但仍在 git 历史里，**务必去网关后台吊销** |
| `DS_MODEL` | 远程模型 | `Deepseek-v4-flash` |
| `OLLAMA_HOST` | 本机 Ollama 地址 | `http://127.0.0.1:11434` |
| `SERMON_MODEL` | 本地模型 | `qwen3:8b` |
| `SERMON_SRC` | `scan.py` 扫描的源目录 | `识别结果/豆包2.0`（仓库内）；数据移走时用它覆盖 |

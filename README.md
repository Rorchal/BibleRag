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
│   └── 目录结构梳理.md          # 目录审计报告 + 清理建议
│
├── ds_client.py                 # ★ 公共库：DeepSeek 网关客户端 + 结构校验 + 失败落盘
├── prompts_v5.py                # ★ 公共库：v5 提示词常量（被 v3.py 引用）
├── segment.py                   # ★ 公共库 + 入口：本地 Ollama 小模型排版
│
├── scan.py                      # 入口：定时增量扫描转写稿目录 → 调 segment 排版
├── check.py                     # 入口：对已生成 md 做全篇逐字校验 + 结构统计
├── trial.py                     # 入口：单篇试跑「章/节/段 + 概要」
├── compare.py                   # 入口：A/B 对比两版提示词（user / mine）
├── noise.py                     # 入口：同参数连跑 N 次，量化重跑方差 + 边界投票
├── v3.py                        # 入口：v3/v4/v5 提示词运行器（三级都带行号）
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

# 远程 DeepSeek：单篇试跑 / A-B 对比 / 噪声基线 / v5 提示词
python trial.py <txt> --max-tokens 16000 --tag full2
python compare.py <txt> --tag ab1
python noise.py <txt> --runs 5 --variant mine
python v3.py <txt> --prompt v5 --effort low --tag v5_low

# 评测：以豆包结构为 gold
python goldeval.py --gold output/gold_v2.md --pred output/v5_low_*.parsed.json --txt <txt>
```

## 环境变量

| 变量 | 用处 | 默认值 |
| --- | --- | --- |
| `DS_BASE` | DeepSeek 网关地址 | `https://chatapi.weixin.qq.com/openai/v1/chat/completions` |
| `DS_KEY` | 网关密钥 | **代码里硬编码了真实密钥，必须吊销并改为必填，见审计报告** |
| `DS_MODEL` | 远程模型 | `Deepseek-v4-flash` |
| `OLLAMA_HOST` | 本机 Ollama 地址 | `http://127.0.0.1:11434` |
| `SERMON_MODEL` | 本地模型 | `qwen3:8b` |
| `SERMON_SRC` | `scan.py` 扫描的源目录 | 写死的 Windows 绝对路径，**指向仓库外**，见审计报告 |

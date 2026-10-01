# 协作原则

- **所有回复一律用中文。** 包括进度说明、结果汇报、提问与总结；代码、命令、文件名、字段名保持原样。
- **DeepSeek 调用一律用 `deepseek-flash`**（`DS_MODEL=deepseek-flash`），不用 `deepseek-v4-pro`，复核者也一样。
  即使提示词文档里建议用 pro，也以这条为准。
- **生成问题（段→检索用问题）固定用 `prompts/段问题_v6.md`**，脚本 `gen_questions.py`，按章调用（一章的所有段一批，8 章就 8 次），
  最后合并成 `切分结果/<名字>/问题.md` 与 `问题.json`。这是 `pipeline.py` 的第 8 步（最后一步）。
  调用时 SYSTEM 和 user 开头固定不动，只换每段的 id/title/text——这样每次都命中 DeepSeek 的前缀缓存。
  不要为了某一篇改提示词的字；要改就升版本号另存一份。

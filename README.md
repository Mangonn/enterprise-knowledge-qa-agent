# 企业知识问答 Agent（题目 3）

可运行的 CLI：对合成 FAQ 和制度进行检索、读取原文，给出附文档 ID、版本日期和原文片段的回答；遇到无优先级的冲突会列出双方依据并追问适用制度。支持真实 DeepSeek 模型的逐步工具决策，以及明确标记的离线 Mock 流程检查。

## 环境与安装

- Python 3.11+。仅使用标准库；`requirements.txt` 无外部依赖。
- 在项目根目录运行。离线模式无需环境变量。

```bash
python3 --version
python3 -m unittest discover -s tests -v
```

## 可复制的 CLI 运行命令

```bash
python3 -m src.cli examples/inputs/clear_answer.json --mode mock
python3 -m src.cli examples/inputs/conflict.json --mode mock
python3 -m src.cli examples/inputs/solo_policy.json --mode mock --corpus data/solo_reimbursement.json
python3 -m src.cli examples/inputs/tool_failure.json --mode mock --fail-once read_snippet
```

`--output work/result.json` 可写入 JSON 文件。退出码 0 表示已回答、需澄清或明确资料不足；未完成、模型不可用、输入错误返回 2。`--max-steps` 限制每次模型决策步数，`--max-tools` 限制检索与读取调用次数；超限返回 `incomplete` 和明确原因。

## 真实模型入口

复制 `.env.example` 为 `.env` 并设置自己的 `DEEPSEEK_API_KEY`，或在运行环境设置该变量。`.env` 已忽略，勿提交密钥。若本地 Python 证书链不完整，可设置 `SSL_CERT_FILE` 为系统证书路径。

```bash
cp .env.example .env
python3 -m src.cli examples/inputs/conflict.json --mode deepseek
python3 -m src.cli examples/inputs/tool_failure.json --mode deepseek --fail-once read_snippet
```

模型每轮选择一个 function call；工具观察作为下一轮 tool message 回传。缺少密钥时返回 `model_unavailable`，不会暗中改用 Mock。离线 `MockPolicy` 只能验证流程和证据边界，**不证明真实模型的决策能力**。真实模型运行记录见 [`examples/model_traces/`](examples/model_traces/)；可用 `python3 examples/run_model_examples.py` 重跑，需密钥与网络。

## 评审入口

| 内容 | 位置 |
| --- | --- |
| 问题输入、实际输出、逐步 Trace | [`examples/`](examples/) |
| 13 组离线案例重跑 | `python3 examples/run_examples.py` |
| 自动测试与结果 | [`tests/test_agent.py`](tests/test_agent.py)、[`examples/test-results.txt`](examples/test-results.txt) |
| 带 ID、版本日期及替换样例的合成资料 | [`data/`](data/) |
| 需求、设计、评估、AI 使用与复盘 | [`docs/`](docs/) |

示例中的制度、FAQ 和版本日期均为合成数据，不代表真实公司政策。回答由程序依据**实际读取**的原文生成，模型负责检索、补查、读取、追问或停止的动作选择。当前冲突识别针对合成报销期限案例，不应直接用于生产制度判定。

## 2026-11-03 评审后改进

追问“收不到验证码邮件”时，D03 的“企业邮箱不可用”不再被当成直接答案；返回资料不足并给出 D03 引用，真实模型 Trace 已更新。见 [评估](docs/03-evaluation.md)。

# 03 验证结果

## 运行记录

2026-11-01 在 Python 3.12 环境执行：

```bash
python3 -m unittest discover -s tests -v
python3 examples/run_examples.py
python3 examples/run_model_examples.py  # 需 DEEPSEEK_API_KEY 和网络
```

结果：19 项自动测试通过；12 组离线 CLI 输入、实际输出及 Trace；6 组真实 DeepSeek 模型实际输出及 Trace。测试日志见 [`examples/test-results.txt`](../examples/test-results.txt)。离线 Mock 只验证流程和边界；真实模型运行记录证明的是这六次运行的动作选择，不代表稳定成功率。

## 验收点与证据

| 验收点 | 输入、实际输出、Trace | 实际结果 |
| --- | --- | --- |
| 明确答案 | [`clear_answer` 输入](../examples/inputs/clear_answer.json)、[`真实输出`](../examples/model_outputs/deepseek_clear_answer.json)、[`真实 Trace`](../examples/model_traces/deepseek_clear_answer.json) | 搜索 D03、读取 S01，回答含 D03 原文与版本日期 |
| 文档冲突 | [`conflict` 输入](../examples/inputs/conflict.json)、[`真实输出`](../examples/model_outputs/deepseek_conflict.json)、[`真实 Trace`](../examples/model_traces/deepseek_conflict.json) | 读取 D01 的 30 天和 D02 的 15 天；标记冲突，追问适用制度，不选较新的日期 |
| 资料外问题 | [`outside` 输入](../examples/inputs/outside.json)、[`真实输出`](../examples/model_outputs/deepseek_outside.json)、[`真实 Trace`](../examples/model_traces/deepseek_outside.json) | 模型多次尝试不同检索词仍无命中，返回资料不足且无虚构引用 |
| 改写后的相似问题 | [`paraphrase` 输入](../examples/inputs/paraphrase.json)、[`真实输出`](../examples/model_outputs/deepseek_paraphrase.json)、[`真实 Trace`](../examples/model_traces/deepseek_paraphrase.json) | “花完钱最晚几天报账”检索到 D01/D02，保留同样的冲突判断 |
| 上下文追问 | [`followup` 输入](../examples/inputs/followup.json)、[`真实输出`](../examples/model_outputs/deepseek_followup.json)、[`真实 Trace`](../examples/model_traces/deepseek_followup.json) | 当前句无主题词，结合历史找到 D03；模型在一次结束校验拒绝后补查，再给引用回答 |
| 工具失败 | [`tool_failure` 输入](../examples/inputs/tool_failure.json)、[`真实输出`](../examples/model_outputs/deepseek_tool_failure.json)、[`真实 Trace`](../examples/model_traces/deepseek_tool_failure.json) | 首次读取超时记为失败，模型观察后再次读取成功；失败读取未进入引用 |
| 单一制度 | [`solo_policy` 输出](../examples/outputs/solo_policy.json)、[`Trace`](../examples/traces/solo_policy.json) | 仅 D01 时可依据原文答 30 天 |
| 原文替换与动作变化 | [`45 天资料`](../data/changed_policy_45.json)、[`实际输出`](../examples/outputs/changed_policy_45.json)、[`Trace`](../examples/traces/changed_policy_45.json) | D01 从 30 改为 45 后，答案及引用同步变为 45 天 |
| 冲突消除 | [`同为 30 天的资料`](../data/aligned_policies.json)、[`实际输出`](../examples/outputs/aligned_policies.json)、[`Trace`](../examples/traces/aligned_policies.json) | D02 改为 30 后，动作从追问变为有依据回答 |
| 细节缺失 | [`unsupported_detail` 输出](../examples/outputs/unsupported_detail.json)、[`Trace`](../examples/traces/unsupported_detail.json) | FAQ 未写验证码有效期，返回资料不足，不编造天数 |
| 上限 | [`step_limit` 输出](../examples/outputs/step_limit.json)、[`Trace`](../examples/traces/step_limit.json) | 明确 `incomplete/step_limit`，没有回答 |

真实 Trace 每步均含模型动作、参数、工具观察、后续动作及简短决策依据；不记录模型内部思维。真实模型偶尔请求并行工具，执行器将整批拒绝并要求重新单步选择，拒绝事件不计业务工具调用数。

## 安全与失败验证

- 假模型未检索就要求回答：结束被拒绝为 `search_required`；未读取原文也无法完成有依据回答。
- 假模型直接读取未检索的 D03：工具拒绝 `document_not_in_search_results`。
- D01/D02 全部读完后，若模型仍要求单一答案：结束被拒绝为 `unresolved_document_conflict`。
- 读取与搜索各注入一次超时：失败步骤保持失败，后续成功结果有单独步骤。工具调用或决策步数达限会明确未完成。
- 若所有检索都失败，结束校验拒绝将工具故障冒充“资料不存在”。
- 没有模型密钥时 `--mode deepseek` 返回 `model_unavailable`，不把 Mock 运行冒充模型运行。

## 局限

别名检索适合这套小型合成语料，未验证大规模、拼写错误和跨文档语义检索。程序级冲突检测主要针对报销期限的“数字 + 天”，其他隐含矛盾可能无法识别；细节缺失判定也只覆盖示例中定义的问法。六次真实模型运行不能说明稳定成功率。资料、日期、权限和 IT 流程均为合成数据，没有连接内部知识库或审批系统。

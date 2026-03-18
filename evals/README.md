# 电商客服离线评测

固定种子 `20261124` 生成 1200 条合成样例。原问题与其改写共享同一调优或留出分组，六类样例分别为意图 260、RAG 260、Workflow/MCP 220、长对话记忆 160、运营问数 180、故障和隔离 120。标签包括最终路由、来源 ID、事实、SQL 结果、工具动作和受限身份的预期响应。故障用例使用专门启用故障注入的模拟环境。

```powershell
python -m evals.dataset --output evals/cases.jsonl
python -m evals.run --profile smoke --require-mode demo --fail-on-regression --output artifacts/eval-smoke.json
python -m evals.run --profile all --require-mode demo --faults --workers 4 --output artifacts/eval-demo.json
```

启动后端时设置 `ECOMMERCE_ENABLE_FAULTS=1` 即可运行 `--faults` 用例。未传入 `--faults` 时，报告记录这些用例的跳过原因。可使用 `--profile tune` 或 `--profile holdout` 固定切分，使用 `--category rag` 等参数取指定类别，或用 `--case rag.return_conditions.01` 回放单条用例。`--fail-on-regression` 在断言、协议或 Judge 执行失败时返回非零退出码。

报告保留数据哈希、运行形态、模型、提示词和知识版本，按类别和分组记录通过、失败与跳过。意图准确率和分类召回以意图样例计算。RAG 计算引用、事实检查及检索 MRR 与 Recall@1/3，已启用的检索阶段未返回候选时得零分。工具、转人工、长对话订单引用保留率和 SQL 结果使用对应样例。开放回答在启用 Judge 时额外记录事实、完整性、引用支撑和兜底四项评分。Token 差从评测前后的 `/metrics` 读取，评测时使用独占后端以获得可比数据。可通过 `EVAL_MODEL_VERSION`、`EVAL_PROMPT_VERSION` 和 `EVAL_KNOWLEDGE_VERSION` 补充版本。

## 对照实验

分别启动 `ECOMMERCE_RETRIEVAL_VARIANT=dense`、`hybrid` 与 `rerank` 实例，保持政策版本、模型、提示词、评测切分和并发参数一致。取同一批 RAG 题目，将上一组报告交给 `--compare`。记忆同理，使用 `ECOMMERCE_MEMORY_VARIANT=recent` 和 `layered` 比较相同长对话。两种记忆方式均使用应用默认 Token 预算。报告以百分点表示指标差异，另给出记忆的相对变化、P95 延迟和观测 Token 差值。模拟模式应标为 `demo`，真实依赖运行使用 `--require-mode full`。

```powershell
python -m evals.run --base-url http://127.0.0.1:8765 --profile holdout --category rag --output artifacts/rag-dense.json
python -m evals.run --base-url http://127.0.0.1:8766 --profile holdout --category rag --compare artifacts/rag-dense.json --output artifacts/rag-hybrid.json
python -m evals.run --base-url http://127.0.0.1:8767 --profile holdout --category rag --compare artifacts/rag-hybrid.json --output artifacts/rag-rerank.json
python -m evals.run --base-url http://127.0.0.1:8768 --profile holdout --category memory --output artifacts/memory-recent.json
python -m evals.run --base-url http://127.0.0.1:8767 --profile holdout --category memory --compare artifacts/memory-recent.json --output artifacts/memory-layered.json
```

## Judge 与真实链路

为 `--judge` 提供支持 JSON 输出的 OpenAI 兼容推理服务。环境变量为 `EVAL_JUDGE_BASE_URL`（含 `/v1` 的 API 根地址）、`EVAL_JUDGE_API_KEY` 和 `EVAL_JUDGE_MODEL`。Judge 只处理 RAG 和业务 Workflow 的开放回答。在隔离的合成数据环境运行，可选通过 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL` 把评分挂到对应追踪。

人工抽样按同一 0–2 量表填写 `factual`、`complete`、`supported` 和 `fallback`，整理成 JSON 数组。每项包含 `case_id`、人工评分对象 `human`，以及包含 `answer`、`route`、`status`、`sources` 的合成回复对象 `response`。传给 `--judge --judge-calibration <文件>` 后，评测会先计算各项完全一致率和平均绝对误差，再执行选定用例。更换 Judge 模型或规则时复用同一组人工样本。

GitHub Actions 每次变更验证固定数据与冒烟回归、反馈样例和前端产物。手动触发 `full_integration` 时，配置 `EVAL_FULL_BASE_URL` 及 `EVAL_TOKEN_ALICE`、`EVAL_TOKEN_BOB`、`EVAL_TOKEN_OPS` 的 Secret。实例需要使用相应的合成订单和政策，并启用完整的真实服务依赖。补齐 Judge Secret 和 `EVAL_JUDGE_MODEL` Variable 后，同一任务会执行完整 Judge 评测。缺少完整环境地址、身份凭证或 Judge 配置时，报告会记录对应的跳过项。

## 反馈回放

先运行默认政策的基线 API 实例。准备独立候选政策文件，然后在另一个端口启动候选 API 实例。

```powershell
python -m evals.feedback_case prepare --output .runtime/review/policies.json
$env:ECOMMERCE_DATA_DIR = '.runtime/review/runtime'
$env:ECOMMERCE_KNOWLEDGE_FILE = '.runtime/review/policies.json'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8766
```

在另一终端执行回放。回放将基线的赠品问题交给客服，保存顾客合成低评分与客服复核。复核后的同一预期先在基线失败，再在候选版本通过。报告记录反馈 ID、两个运行编号、条款版本和断言结果。新增人工复核样例时，可沿用 `evals/reviews/gift-return.json` 的格式，通过 `--fixture` 指定它。

```powershell
python -m evals.feedback_case replay --baseline-url http://127.0.0.1:8765 --candidate-url http://127.0.0.1:8766 --output artifacts/feedback-replay.json
```

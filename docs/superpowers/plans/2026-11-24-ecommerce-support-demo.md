# 智能电商客服 Demo 实现计划

> 依据根目录的《智能电商客服 Demo 需求分析》。独立任务并行实现，集成由主会话完成。

**目标**　让顾客、客服和运营能在浏览器走完整的售后演示，并复跑评测及查看运行证据。

**架构**　遵循 oncall 的 `app/api`、`app/models`、`app/services`、`app/agent`、`app/core` 职责。顾客主流程使用 LangGraph 与持久 SQLite Checkpointer，工具经 MCP 调用。演示模式提供本地数据和确定性模型替身，完整模式接入实际 Jev、模型、Milvus、Redis 及 MySQL。

## 任务

- [x] **客服链路（R1、R2）**。在 `tests/test_support.py` 先固定政策引用、无证据转人工、跨用户查询、建单中断重启和幂等行为。实现 `app/agent/workflow.py`、`app/services/retrieval.py`、`app/services/business.py`、`mcp_servers/commerce.py` 与 `app/api/platform.py`。SSE 接口 `/api/chat/stream` 按事件返回路由、来源、内容与工单；`/api/runs/{run_id}/resume` 恢复执行。用真实 MCP 子进程、持久 Checkpointer 与 API 回归验证。
- [x] **会话层（R3）**。在同一测试文件先固定分页、会话隔离、摘要生成迟延和去重行为。`app/services/memory.py` 提供 SQLite 演示存储及 Redis List 实现，写入稳定消息 ID；主图使用近期、摘要和关键业务事实。
- [x] **问数（R4）**。先用独立用例固定运营 SQL 结果、拒绝条件、EXPLAIN 及有限修正。`AnalyticsService` 放在 `app/services/analytics.py`，`ask(question)` 返回结果与 SQL 证据；`/api/analytics` 由 API 层限制运营角色。演示使用隔离的 SQLite 分析快照，完整模式使用 MySQL 只读账户。
- [x] **观测与评测（R5、R6）**。`app/core/observability.py` 提供 span、计数和 Prometheus 端点适配；`deployment/observability` 提供指标抓取、面板与告警。`evals` 提供 1200 条合成样例生成、可执行回归、Judge、分组结果和消融，工作流位于 `.github/workflows`。真实模式与模拟模式的报告分别标注。
- [x] **前端**。`frontend/` 使用 Vue 3 和 Element Plus，提供顾客对话及证据、客服工单、运营问数。消费 SSE，恢复持久对话，可构建为 FastAPI 同源静态入口。用实际浏览器检查桌面和移动尺寸及核心流程。
- [x] **总体验证**。后端 40 项与 15 个子测试通过，覆盖 Redis、MySQL 及 Milvus + BGE-M3 集成。前端 3 项测试、生产构建、Chrome 验证和实际进程恢复通过。1200 条演示评测首轮通过 1180 条，修复后两个受影响分组分别复测通过 40 和 20 条，最新版冒烟通过 34 条。实际 Jev、BGE-Reranker、聊天模型和 Judge 的端到端联调等待相应服务和凭证。

## 重点回归

两位顾客共享会话 ID 时仍隔离，工单写成功后的进程崩溃恢复不会重复建单，Noul 无法确认时已有候选文本也不能直接回答，越权 SQL 与超预算查询受拒，缓存、模型或 MCP 的降级结果可查且不伪装成功。

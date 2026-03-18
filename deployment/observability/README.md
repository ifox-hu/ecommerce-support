# 电商客服运行观测

从仓库根目录启动本地观测服务。

```powershell
docker compose -f deployment/observability/compose.yml up -d --build
$env:ECOMMERCE_OTEL_ENDPOINT = 'http://127.0.0.1:4318'
$env:ECOMMERCE_ENABLE_FAULTS = '1'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Prometheus 每 5 秒抓取 `host.docker.internal:8765/metrics`。如果 Docker 环境无法访问宿主机的回环地址，将 API 发布到容器可达的本地接口，并按部署地址更新 `prometheus/prometheus.yml` 后重建。Grafana 默认的演示登录为 `admin` / `demo-dashboard`，可通过 `GRAFANA_ADMIN_USER` 与 `GRAFANA_ADMIN_PASSWORD` 设置。Grafana、Jaeger 和 Prometheus 都只发布到宿主机回环地址。 [Grafana 看板](http://localhost:3001/d/ecommerce-support) [Jaeger](http://localhost:16686) [Prometheus 抓取](http://localhost:9090/targets) [Prometheus 告警](http://localhost:9090/alerts)

运行一条正常查询和一条故障查询。SSE 中的 `run_id` 可以在 Jaeger 的 `ecommerce-support` 服务中作为 Tag 查询 `run_id=<编号>`，也可在应用日志按该编号定位失败节点。P50 和 P95 来自 Prometheus 直方图，指标的标签使用固定业务类别。

```powershell
python -m evals.run --case intent.order_lookup.01 --output artifacts/observability-normal.json
python -m evals.run --case fault_isolation.model_timeout.01 --faults --output artifacts/observability-fault.json
```

等待一次抓取后，`EcommerceModelTimeout` 会在页面显示。最后一次超时离开一分钟窗口后，告警恢复。工具失败率阈值为 20%，持续 10 秒时触发。请求 P95 阈值为 3 秒，持续 30 秒时触发。这些初始阈值用于演示，可按实际服务分布调整 `prometheus/rules.yml`。

```powershell
docker compose -f deployment/observability/compose.yml run --rm --no-deps --entrypoint promtool prometheus test rules /etc/prometheus/rules.test.yml
```

配置 `ECOMMERCE_LANGFUSE_PUBLIC_KEY`、`ECOMMERCE_LANGFUSE_SECRET_KEY` 和 `ECOMMERCE_LANGFUSE_HOST` 后，已安装 `integrations` 扩展的后端会记录 LangChain 模型调用、耗时和模型返回的 Token 用量。Langfuse Trace ID 从运行编号生成，恢复执行会沿用该记录。会话以哈希关联，模型输入、输出和异常消息经过脱敏。启用 Judge 并给评测进程配置同一 Langfuse 项目后，评分会写入相同 Trace ID。

# enterprise-copilot —— 一体化企业智能助手

以 **Text-to-SQL 为主**、RAG 知识问答与文档转换 Agent 为辅的三合一原型，单应用单端口运行。

> 定位：可内部试点的 PoC，非生产系统。真实数据接入 / 权限体系 / 可观测等企业化项尚未完成（见文末「边界」）。

## 架构

```
浏览器 ──> gateway.app (单端口 8000，单 FastAPI 应用)
              ├─ /          Text-to-SQL（主技能，根路径）
              ├─ /rag/*     RAG 知识问答
              └─ /doc/*     文档转换 Agent
```

三大技能各是一个可独立启动的包，`gateway` 用 `include_router` 把它们合并到 8000，并挂载统一的请求中间件（Trace ID + 审计日志 + 简单限流）。

```
enterprise-copilot/
├─ common/         统一配置 + 双 LLM 工厂（原生 openai / langchain ChatOpenAI）
├─ gateway/        统一入口 app.py + 中间件 middleware.py
├─ skills/
│  ├─ text2sql/    自然语言 → SQL → 结果（schema_linker/llm_client/sql_guard/executor）
│  ├─ rag_qa/      检索增强问答（embeddings/reranker/vector_store/rag_engine）
│  └─ doc_agent/   LangGraph 文档转换 Agent + 纯规则转换快速通道
├─ web/            统一前端（SQL 主页 + RAG/文档侧边页签）
├─ tests/          端到端测试脚本
├─ data/           chroma 向量库 + ic_substrate.db + 运行期目录（gitignore）
├─ install.bat / start.bat   一键装依赖 / 一键启动
└─ Dockerfile / docker-compose.yml   容器化（封入 weasyprint/pandoc 系统依赖）
```

## 快速启动（Windows）

1. 首次：双击 `install.bat`（创建 `.venv` 并按 `requirements.lock` 装依赖）。
2. 复制 `.env.example` 为 `.env`，填入 `LLM_API_KEY`（对话）与 `EMBED_API_KEY`（向量化）。
3. 双击 `start.bat`，浏览器打开 <http://127.0.0.1:8000/>。

或纯命令（不依赖脚本）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m uvicorn gateway.app:app --host 127.0.0.1 --port 8000
```

Docker（需本机有 Docker）：

```bash
docker compose up --build -d
```

## 环境要求

- Python 3.11（`requirements.lock` 按 3.11 锁定）
- 文档转换依赖系统组件：**pandoc**、**weasyprint**（Pango/Cairo/GdkPixbuf 等图形库）。Docker 镜像已内置；本机裸跑需自装，否则仅 `/doc` 的 PDF/DOCX 转换受影响，SQL 与 RAG 不受影响。

## 数据说明

`data/` 已被 `.gitignore` 排除，包含：

| 路径 | 内容 | 来源 |
|---|---|---|
| `data/chroma/` | RAG 向量库 `rag_knowledge_base`（130 块） | 旧仓 `text-converter-assistant` 原样复制；也可经 `/rag/ingest` 重新入库 |
| `data/db/ic_substrate.db` | 拟真 ERP+MES SQLite（22 张表，只读） | 旧仓 `erp-mes-copilot` 原样复制 |

新环境需从旧仓复制这两项，或通过 ingest 重建 chroma。

### 知识库按用户隔离

RAG 的知识库**按 `X-User` 请求头隔离**：每个用户一个独立 Chroma collection。

- 缺省或 `X-User: default` → 沿用 `rag_knowledge_base` 共享库；
- 其他用户 → 各自独立的 `kb_{user}`，互不可见、互不干扰。

前端右上角「用户」输入框即设置该标识（存 localStorage）。命令行示例：

```powershell
curl -F "file=@文档.md" -H "X-User: alice" http://127.0.0.1:8000/rag/ingest
curl -H "Content-Type: application/json" -H "X-User: alice" -d '{"question":"..."}' http://127.0.0.1:8000/rag/chat
```

> 注：这是最小多租户 demo（按用户命名的 collection，非正式鉴权）。生产需结合 SSO / API Key 把 `X-User` 与真实身份绑定，并加权限校验。

## API

| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/ask` | Text-to-SQL 问答（主） |
| POST | `/rag/chat` | 知识库问答 |
| POST | `/rag/ingest` | 文档入库 |
| POST | `/doc/convert` | 文档转换 |
| GET | `/doc/download/{filename}` | 下载转换结果 |
| GET | `/health` | 主技能健康检查 |
| GET | `/api/health` | 网关聚合状态 |

## 认证（可选）

默认 `GATEWAY_API_KEY` 为空，**不启用认证**（PoC 零行为变更）。填入密钥后（**逗号分隔可配多个 key**），除白名单路径（`/`、`/health`、`/api/health`、`/docs` 等）外，所有业务接口都要求携带正确的密钥，否则返回 401。

- 两种传密钥方式：请求头 `X-API-Key: your-key`，或标准 `Authorization: Bearer your-key`（时序安全比较）。
- 前端页面右上角可填入并保存 API Key（存浏览器 localStorage），请求时用 `X-API-Key` 自动携带。
- 命令行调用示例：

```powershell
curl -H "X-API-Key: your-key" -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" -d '{"question":"当前在制工单有多少批？"}'
curl -H "Authorization: Bearer your-key" -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" -d '{"question":"当前在制工单有多少批？"}'
```

## 测试与回归

测试已规范为 pytest，一条命令运行全部（含依赖 LLM API 的端到端测试）：

```powershell
.venv\Scripts\python.exe -m pytest tests\ -v
```

仅跑不依赖 API 的本地测试（更快）：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_reflection.py tests\test_content_parser.py -v
```

Text-to-SQL 50 题回归（需有效的 `LLM_API_KEY`）：

```powershell
.venv\Scripts\python.exe skills\text2sql\eval\run_eval.py
```

## 边界（简历/面试口径）

- 可写：设计并验证一套可扩展的三合一原型架构，Text-to-SQL 50 题回归全通过，三服务可独立/一体化运行。
- 不夸大生产部署。当前用拟真 SQLite 数据，真实 ERP/MES 连接器、最小权限只读账号、认证/SSO、审计脱敏、token 成本护栏、可观测链路、限流（现为内存级单进程实现）等尚未落地。
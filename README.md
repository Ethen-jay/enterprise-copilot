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
| `data/chroma/` | RAG 向量库（`rag_knowledge_base` 共享库 + 按用户的 `kb_{user}` 独立库） | 旧仓 `text-converter-assistant` 原样复制；也可经 `/rag/ingest` 重新入库 |
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
| POST | `/ask` | Text-to-SQL 问答（主技能，挂根路径） |
| GET | `/health` | Text-to-SQL 健康检查（含动态锚点审计） |
| POST | `/rag/chat` | 知识库问答（检索→重排→LLM→蕴含裁判反思） |
| POST | `/rag/ingest` | 文档入库（支持 replace 同名替换、doc_date/doc_version 仲裁字段） |
| POST | `/rag/ingest/stream` | SSE 流式入库（实时推送解析/分块/embedding/OCR 进度） |
| POST | `/rag/ingest/batch` | 批量导入（返回任务 ID，后台逐份处理） |
| GET | `/rag/tasks/{task_id}` | 批量导入进度查询 |
| GET | `/rag/documents` | 知识库文档列表（小块数/父块数/最大页码） |
| DELETE | `/rag/documents?source=` | 按文件名删除文档 |
| GET | `/rag/health` | RAG 健康检查 |
| POST | `/doc/convert` | 文档转换（同步） |
| POST | `/doc/convert/async` | 文档转换（异步任务） |
| GET | `/doc/tasks/{task_id}` | 转换任务进度查询 |
| GET | `/doc/download/{filename}` | 下载转换结果 |
| GET | `/doc/health` | 文档转换健康检查 |
| POST | `/login` | 登录（账号校验 + 可选密钥认证） |
| GET | `/api/health` | 网关聚合状态 |

### 三技能独立启动（排障形态）

三个技能包各自带独立启动入口（复用同一份 APIRouter，见各 `service.py` 尾部）：

```powershell
.venv\Scripts\python.exe -m uvicorn skills.text2sql.service:app --host 127.0.0.1 --port 8010
.venv\Scripts\python.exe -m uvicorn skills.rag_qa.service:app   --host 127.0.0.1 --port 8001
.venv\Scripts\python.exe -m uvicorn skills.doc_agent.service:app --host 127.0.0.1 --port 8002
```

## 核心特性

**Text-to-SQL（主技能）**
- 动态业务日期锚点：运行时查库 `MAX(业务日期)`（五探针：出站/出货/电测/客诉/下单），"本月/最近一周"锚定数据最新日而非系统时钟；进程内 TTL 缓存（默认 300s），探测失败回退 `.env` 的 `ANCHOR_DATE`。
- 只读纵深防御：sqlparse 词法守卫 + SQLite 只读 URI（`mode=ro`）+ `PRAGMA query_only`；SQL 执行失败自动把脱敏报错回喂模型修复重试（最多 1 次）。
- 三态输出：正常 SQL / refusal（无法回答）/ clarify（信息不足追问，支持多轮补充）。

**RAG 知识问答**
- Small-to-Big 父子分块：小块（200 字）做向量检索，命中后回溯父块（1000 字）喂 LLM；`USE_PARENT_CHILD=false` 可回退单层分块。
- 混合检索：向量（bge-large-zh）+ BM25 关键词召回取并集，bge-reranker-v2-m3 重排；重排分数全低于阈值（0.3）时引擎快拒答。
- 三层防幻觉：system prompt 约束 → LLM 蕴含裁判（独立 `reflection.py`）→ 病因分流重试（材料不够扩检索 / 编造与缺引用塞纸条重写，最多 2 次）。
- 冲突仲裁：入库可指定 `doc_date`/`doc_version`，片段冲突时 prompt 标注新旧，模型按"日期更新/版本更新"采信并说明依据。
- 扫描版 PDF OCR 兜底：页文本 <10 字符且含图像时走 RapidOCR（PP-OCRv4），按页判断支持混合 PDF，OCR 结果更长才采纳。
- 外部 API 指数退避重试：429/5xx/网络异常自动重试（全抖动），embedding/rerank/LLM 三客户端统一接入。
- Prompt Injection 检测：检索片段含"忽略以上指令"类文本时打日志标记，system prompt 常驻防护规则兜底。

**文档转换 Agent**
- LangGraph 状态机：规划 → 转换 → 反思校验（段落数/表格行数/关键词保留率）→ 病因分流重试（最多 2 次）。
- Hub-Spoke 架构：6 种格式（pdf/md/docx/html/xlsx/txt）经 Markdown 中间态互转；另含纯规则转换快速通道。
- Markdown → Word 转换前做列表规范化（列表与正文间补空行），确保 Word 输出真正的列表结构。
- 旧版二进制格式识别：converter 节点按文件头魔数（OLE2）嗅探，`.doc`/`.xls` 改名成 `.docx`/`.xlsx` 骗不过；命中即返回可操作中文提示，而非抛出含内部临时路径的裸库报错。

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

测试已规范为 pytest，一条命令运行全部 31 项（含依赖 LLM/Embedding API 的端到端测试）：

```powershell
.venv\Scripts\python.exe -m pytest tests\ -v
```

仅跑不依赖外部 API 的本地测试（更快）：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_reflection.py tests\test_content_parser.py tests\test_rag_features.py -v
```

专项回归（需 `.env` 配好密钥）：

| 回归项 | 命令 | 最近结果 |
|---|---|---|
| Text-to-SQL 50 题（基线 20 + 泛化 30，含动态锚点审计） | `.venv\Scripts\python.exe skills\text2sql\eval\run_eval.py` | 50/50 = 100% |
| Text-to-SQL 对抗测试（写操作注入/无依据请求） | `.venv\Scripts\python.exe skills\text2sql\eval\run_adversarial.py` | 全部拦截，零执行 |
| RAG 15 题（有答案 10 + 无依据拒答 5） | `.venv\Scripts\python.exe skills\rag_qa\eval\run_rag_eval.py` | 15/15 = 100% |

评测明细存于各 `eval/runs/` 目录（`latest.json` + 时间戳文件）。

## 边界（简历/面试口径）

- 可写：设计并验证一套可扩展的三合一原型架构（逻辑分离、物理合并），Text-to-SQL 50 题回归 100%（含动态锚点审计）、RAG 15 题评测 100%、对抗测试零执行，三服务可独立/一体化运行。
- 不夸大生产部署。当前用拟真 SQLite 数据，真实 ERP/MES 连接器、最小权限只读账号、认证/SSO、审计脱敏、token 成本护栏、可观测链路、限流（现为内存级单进程实现）等尚未落地。
- 已知局限（文档转换）：不支持旧版 Office 二进制格式（.doc/.xls/.ppt），只解析 OOXML；已按文件头魔数拦截"改名伪装"并给出可操作提示，但真要支持需引入 LibreOffice / antiword 等外部转换器。
"""RAG FastAPI 服务入口：/ingest、/chat。独立于 main.py。"""

import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import List, Optional

import asyncio

from fastapi import APIRouter, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from skills.rag_qa import rag_engine
from skills.rag_qa.vector_store import normalize_user

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rag_app")

# 路由抽象成 APIRouter：独立启动（本文件 app）与 gateway 统一入口复用同一份
router = APIRouter()

# 冲突仲裁字段的格式校验
_DOC_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DOC_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,16}$")


def _resolve_user(x_user: Optional[str]) -> str:
    """从 X-User 请求头解析用户标识并校验格式，非法返回 400。"""
    try:
        return normalize_user(x_user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _validate_arbiter_fields(doc_date: Optional[str], doc_version: Optional[str]) -> None:
    """校验冲突仲裁字段格式：doc_date=YYYY-MM-DD，doc_version=字母/数字/._- 限16位。"""
    if doc_date and not _DOC_DATE_PATTERN.fullmatch(doc_date):
        raise HTTPException(status_code=400, detail="doc_date 格式非法，应为 YYYY-MM-DD")
    if doc_version and not _DOC_VERSION_PATTERN.fullmatch(doc_version):
        raise HTTPException(
            status_code=400,
            detail="doc_version 格式非法：仅允许字母/数字/点/下划线/连字符，1-16 位",
        )


# ----------------------------------------------------------------------
# 上传保护：文件大小上限 + 流式读取（避免大文件一次性进内存）
# ----------------------------------------------------------------------
ALLOWED_EXTS = {".pdf", ".md", ".markdown", ".txt", ".json", ".jsonl"}
MAX_UPLOAD_BYTES = int(float(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024)
_READ_CHUNK = 1024 * 1024  # 每次从上传流读取 1MB


class FileTooLargeError(Exception):
    """上传文件超过大小上限。"""


def _validate_ext(filename: str) -> str:
    """校验文件扩展名，返回小写扩展名；非法时抛 400。"""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}，支持 .pdf/.md/.txt/.json/.jsonl",
        )
    return ext


async def _read_upload_limited(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """分块流式读取上传文件，累计超过上限立即中断（不全量载入内存）。

    返回完整字节内容（供下游解析）；超限抛 FileTooLargeError。
    """
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            await file.close()
            raise FileTooLargeError(max_bytes)
        parts.append(chunk)
    return b"".join(parts)


# 上传临时文件落盘目录（data/ 已在 .gitignore 中排除）
UPLOAD_TMP_DIR = Path("data/tmp/uploads")


async def _save_upload_to_disk(file: UploadFile, dest_dir: Path, max_bytes: int = MAX_UPLOAD_BYTES) -> Path:
    """将上传文件流式写入磁盘临时文件，超限立即中断并清理半成品。

    落盘模式：请求体不驻留内存；解析阶段 PDF 直接从路径打开。
    临时文件名使用 uuid（不信任原始文件名，防路径穿越），原文件名仅存于 metadata。

    返回临时文件路径；超限抛 FileTooLargeError。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "").suffix.lower()[:16]
    tmp_path = dest_dir / f"{uuid.uuid4().hex}{ext}"
    total = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLargeError(max_bytes)
                out.write(chunk)
        return tmp_path
    except FileTooLargeError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class ChatRequest(BaseModel):
    question: str
    history: Optional[List[dict]] = None


class ChatResponse(BaseModel):
    answer: str
    citations: list
    reflection: dict = {}


class IngestResponse(BaseModel):
    filename: str
    chunks: int
    status: str
    replaced: bool = False
    replaced_chunks: int = 0
    paged: bool = False        # PDF 分块带页码
    chunk_mode: str = "flat"   # parent_child=Small-to-Big 父子分块 / flat=单层


@router.get("/health")
def health():
    """健康检查。"""
    return {"status": "ok", "service": "rag"}


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    file: UploadFile = File(...),
    replace: bool = Form(False),
    doc_date: Optional[str] = Form(None),
    doc_version: Optional[str] = Form(None),
    x_user: Optional[str] = Header(None),
):
    """上传文档并入库：解析 → 分块 → embedding → 该用户专属 Chroma collection。

    replace=True 时为同名替换：先删除该文件的全部旧向量块，再入新块。
    未显式替换且同名文档已存在时返回 409，防止旧块残留或误覆盖。
    doc_date/doc_version 为可选冲突仲裁字段：片段冲突时优先采信日期更新/版本更新的文档。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    _validate_ext(file.filename)
    user = _resolve_user(x_user)
    _validate_arbiter_fields(doc_date, doc_version)

    # 先落盘并做大小校验，通过后才查重/删旧，避免传一半失败却删掉旧版
    try:
        tmp_path = await _save_upload_to_disk(file, UPLOAD_TMP_DIR)
    except FileTooLargeError as e:
        limit_mb = e.args[0] / 1024 / 1024
        raise HTTPException(
            status_code=413,
            detail=f"文件超过大小上限 {limit_mb:.0f}MB，请压缩或拆分后再上传",
        )

    try:
        # 同名冲突处理：非替换模式直接 409；替换计数供响应回显（引擎内执行删除）
        store = rag_engine.get_store(user)
        replaced_chunks = store.count_source(file.filename) if replace else 0
        if not replace and store.has_document(file.filename):
            raise HTTPException(
                status_code=409,
                detail=f"同名文档已存在: {file.filename}，请使用替换上传(replace=true)",
            )

        try:
            result = rag_engine.ingest_path(
                str(tmp_path), filename=file.filename, user=user,
                replace=replace, doc_date=doc_date, doc_version=doc_version,
            )
            logger.info(
                "入库成功: user=%s, %s, %d 块, 模式=%s, 替换旧块=%d",
                user, result["filename"], result["chunks"],
                result.get("chunk_mode"), replaced_chunks,
            )
            return IngestResponse(
                filename=result["filename"],
                chunks=result["chunks"],
                status="ok",
                replaced=replaced_chunks > 0,
                replaced_chunks=replaced_chunks,
                paged=result.get("paged", False),
                chunk_mode=result.get("chunk_mode", "flat"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("入库失败")
            raise HTTPException(status_code=500, detail=f"入库失败: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)  # 无论成败，清理落盘临时文件


@router.post("/ingest/stream")
async def ingest_stream(
    file: UploadFile = File(...),
    replace: bool = Form(False),
    doc_date: Optional[str] = Form(None),
    doc_version: Optional[str] = Form(None),
    x_user: Optional[str] = Header(None),
):
    """SSE 流式入库：实时推送解析/分块/embedding/存储/OCR 进度。

    同名已存在且未确认 replace=true 时返回 409（建流前拦截）；
    doc_date/doc_version 为可选冲突仲裁字段（片段冲突时新者胜）。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    _validate_ext(file.filename)
    user = _resolve_user(x_user)
    _validate_arbiter_fields(doc_date, doc_version)

    # 同名冲突：非替换模式在建流前直接 409（SSE 一旦开始就无法改状态码）
    if not replace and rag_engine.get_store(user).has_document(file.filename):
        raise HTTPException(
            status_code=409,
            detail=f"同名文档已存在: {file.filename}，请使用替换上传(replace=true)",
        )

    # 响应开始前完成落盘与大小校验，超限直接返回 413
    try:
        tmp_path = await _save_upload_to_disk(file, UPLOAD_TMP_DIR)
    except FileTooLargeError as e:
        limit_mb = e.args[0] / 1024 / 1024
        raise HTTPException(
            status_code=413,
            detail=f"文件超过大小上限 {limit_mb:.0f}MB，请压缩或拆分后再上传",
        )
    filename = file.filename

    status_box = {"done": False, "result": None, "error": None}

    def progress_cb(msg: str):
        status_box["last_msg"] = msg

    async def event_generator():
        try:
            # 在线程池中运行入库（落盘模式：从磁盘路径解析）
            loop = asyncio.get_event_loop()
            task = loop.run_in_executor(
                None,
                lambda: rag_engine.ingest_path(str(tmp_path), filename=filename,
                                               progress_callback=progress_cb,
                                               user=user, replace=replace,
                                               doc_date=doc_date,
                                               doc_version=doc_version),
            )

            last_msg = ""
            while not task.done():
                msg = status_box.get("last_msg", "")
                if msg != last_msg:
                    last_msg = msg
                    yield f"data: {json.dumps({'status': msg}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.1)

            # 发送最终结果
            try:
                result = task.result()
                yield f"data: {json.dumps({'status': 'done', 'result': result}, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'status': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            tmp_path.unlink(missing_ok=True)  # 无论成败，清理落盘临时文件

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ----------------------------------------------------------------------
# 批量异步导入：任务队列 + 进度查询（PoC 级内存任务表，单进程串行处理）
# ----------------------------------------------------------------------
TASKS: dict[str, dict] = {}  # task_id -> 任务状态快照（与网关限流同为内存级，重启即清空）


class BatchIngestResponse(BaseModel):
    task_id: str
    total: int
    accepted: int   # 通过校验、进入队列的文件数
    rejected: int   # 校验即失败（格式/超限）的文件数


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str                # processing | completed
    total: int
    done: int
    current: Optional[str]     # 正在处理的文件名
    results: list[dict]        # [{source, chunks}]
    failed: list[dict]         # [{source, error}]


async def _process_batch_task(task_id: str, user: str, replace: bool, items: list[tuple[Path, str]]):
    """后台工作协程：逐份解析入库（串行，尊重 Chroma 写锁），实时更新任务状态。"""
    info = TASKS[task_id]
    try:
        for tmp_path, source in items:
            info["current"] = source
            try:
                # 同名冲突：批量模式下不整体报错，仅该文件计失败（替换由引擎内执行）
                if not replace and rag_engine.get_store(user).has_document(source):
                    raise ValueError("同名文档已存在（可用替换模式重传）")

                # 阻塞型解析/embedding 放线程池，避免卡住进度查询接口
                result = await asyncio.to_thread(
                    rag_engine.ingest_path, str(tmp_path), source, None, user, replace,
                )
                info["results"].append({"source": source, "chunks": result["chunks"]})
            except Exception as e:
                logger.warning("批量入库单文件失败: %s, %s", source, e)
                info["failed"].append({"source": source, "error": str(e)})
            finally:
                tmp_path.unlink(missing_ok=True)
                info["done"] += 1
                info["current"] = None
        info["status"] = "completed"
        logger.info(
            "批量任务完成: task=%s, 成功=%d, 失败=%d",
            task_id, len(info["results"]), len(info["failed"]),
        )
    finally:
        shutil.rmtree(UPLOAD_TMP_DIR / task_id, ignore_errors=True)


@router.post("/ingest/batch", response_model=BatchIngestResponse)
async def ingest_batch(
    files: List[UploadFile] = File(...),
    replace: bool = Form(False),
    x_user: Optional[str] = Header(None),
):
    """批量导入：接收多文件（前端可用目录选择），落盘后立即返回任务ID，后台逐份处理。

    校验失败的文件（格式/超限）不计入队列，直接计入 rejected。
    进度通过 GET /tasks/{task_id} 轮询。
    """
    if not files:
        raise HTTPException(status_code=400, detail="未选择任何文件")

    user = _resolve_user(x_user)
    task_id = uuid.uuid4().hex[:12]
    task_dir = UPLOAD_TMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    accepted: list[tuple[Path, str]] = []
    rejected: list[dict] = []

    for f in files:
        name = f.filename or ""
        try:
            _validate_ext(name)
        except HTTPException as e:
            rejected.append({"source": name, "error": e.detail})
            continue
        try:
            tmp_path = await _save_upload_to_disk(f, task_dir)
            accepted.append((tmp_path, name))
        except FileTooLargeError:
            limit_mb = MAX_UPLOAD_BYTES / 1024 / 1024
            rejected.append({"source": name, "error": f"超过大小上限 {limit_mb:.0f}MB"})
        except Exception as e:
            rejected.append({"source": name, "error": f"接收失败: {e}"})

    TASKS[task_id] = {
        "status": "processing",
        "total": len(files),
        "done": len(rejected),  # 校验即拒的视为已处理
        "current": None,
        "results": [],
        "failed": list(rejected),
        "rejected": list(rejected),
    }

    if accepted:
        asyncio.create_task(_process_batch_task(task_id, user, replace, accepted))
    else:
        TASKS[task_id]["status"] = "completed"
        shutil.rmtree(task_dir, ignore_errors=True)

    logger.info(
        "批量导入受理: task=%s, user=%s, 共%d份, 受理%d, 拒收%d",
        task_id, user, len(files), len(accepted), len(rejected),
    )
    return BatchIngestResponse(task_id=task_id, total=len(files),
                               accepted=len(accepted), rejected=len(rejected))


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task_status(task_id: str):
    """查询批量导入任务进度（前端轮询）。"""
    info = TASKS.get(task_id)
    if not info:
        raise HTTPException(status_code=404, detail="任务不存在或已过期（任务表在内存中，服务重启即清空）")
    return TaskStatusResponse(
        task_id=task_id,
        status=info["status"],
        total=info["total"],
        done=info["done"],
        current=info["current"],
        results=info["results"],
        failed=info["failed"],
    )


class DocumentItem(BaseModel):
    source: str
    chunks: int            # 小块（可检索单元）数量
    parents: int = 0       # 父块数量（父子分块模式，单层模式为 0）
    max_page: int = 0      # 最大页码（非 PDF 文档为 0）


class DocumentListResponse(BaseModel):
    user: str
    total_docs: int
    documents: list[DocumentItem]


class DocumentDeleteResponse(BaseModel):
    source: str
    deleted_chunks: int
    status: str


@router.get("/documents", response_model=DocumentListResponse)
def list_documents(x_user: Optional[str] = Header(None)):
    """列出当前用户知识库中的全部文档（文件名 + 块数）。"""
    user = _resolve_user(x_user)
    docs = rag_engine.list_documents(user=user)
    return DocumentListResponse(
        user=user,
        total_docs=len(docs),
        documents=[DocumentItem(**d) for d in docs],
    )


@router.delete("/documents", response_model=DocumentDeleteResponse)
def delete_document(source: str, x_user: Optional[str] = Header(None)):
    """按文件名删除当前用户知识库中的某个文档（其全部向量块一并删除）。"""
    user = _resolve_user(x_user)
    try:
        deleted = rag_engine.delete_document(source, user=user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("删除文档失败")
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
    if deleted == 0:
        raise HTTPException(status_code=404, detail=f"知识库中不存在文档: {source}")
    logger.info("删除文档: user=%s, %s, 删除 %d 块", user, source, deleted)
    return DocumentDeleteResponse(source=source, deleted_chunks=deleted, status="ok")


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, x_user: Optional[str] = Header(None)):
    """问答：在该用户专属知识库内检索 Top-K → 拼 Prompt → 调 LLM。"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")

    try:
        result = await rag_engine.chat(req.question, history=req.history, user=_resolve_user(x_user))
        return ChatResponse(
            answer=result["answer"],
            citations=result["citations"],
            reflection=result.get("reflection", {}),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("问答失败")
        raise HTTPException(status_code=500, detail=f"问答失败: {e}")


# ---- 独立启动形态（F1 保底，端口 8001）----
# gateway 一体化模式下由 gateway/app.py include 上面的 router，不走这里
app = FastAPI(title="本地 RAG 知识问答系统", version="1.0.0")
app.include_router(router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("skills.rag_qa.service:app", host="0.0.0.0", port=8001, reload=False, workers=2)

# 静态前端页面必须放在所有 API 路由之后挂载，
# 否则挂载到 "/" 会拦截 /ingest、/chat 等接口（仅独立启动态生效）。
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="rag_static")

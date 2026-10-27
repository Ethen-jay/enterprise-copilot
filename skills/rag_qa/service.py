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
from skills.rag_qa.config import ASYNC_THRESHOLD_MB, LOW_PRIORITY_MB
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


def _sse(event: str, data) -> str:
    """统一 SSE 帧格式：event 名 + 单行 JSON data + 空行结尾。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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
    doc_date/doc_version 为可选冲突仲裁字段：**版本冲突**（同一事物的不同版本）时优先采信日期/版本
    更新的一方；**口径冲突**（不同来源各自有效）时不据此裁决，仅用于引用中标注来源。
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
    doc_date/doc_version 为可选冲突仲裁字段（版本冲突时新者胜；口径冲突时不据此裁决仅标注来源）。
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
# 入库任务调度：优先级队列 + 单 worker 串行（PoC 级内存任务表，重启即清空）
#
# 大文件四层防线中的层2/层4：
# - 层2 异步任务：大文件受理即返回 task_id（/ingest/async），前端轮询 /tasks/{id}
#   或订阅 SSE /tasks/{id}/events（复用 event-stream 形态）
# - 层4 低优先级队列：大文件（典型为扫描版 PDF）入低优先级，
#   普通任务优先执行，worker 空闲时才处理低优先级
#
# 单 worker 串行的额外收益：批量/单文件任务全部排队串行执行，
# 消除多任务并发写同一 Chroma 目录导致索引损坏的风险。
# ----------------------------------------------------------------------
TASKS: dict[str, dict] = {}  # task_id -> 任务状态快照

import heapq  # noqa: E402  (就近放置，便于阅读调度器)

_PENDING: list[tuple[int, int, str]] = []  # 堆：[(priority, seq, coro_name)]
_JOBS: dict[int, object] = {}              # seq -> 待执行协程工厂
_WAKE: Optional[asyncio.Event] = None      # 延迟创建（需运行中的事件循环）
_JOB_SEQ = 0
_WORKER: Optional[asyncio.Task] = None


def _submit_job(priority: int, coro_factory) -> None:
    """入队一个入库任务：priority 0=普通 / 1=低优先级（小者先执行，同优先级 FIFO）。"""
    global _JOB_SEQ
    heapq.heappush(_PENDING, (priority, _JOB_SEQ, getattr(coro_factory, "__name__", "job")))
    _JOBS[_JOB_SEQ] = coro_factory
    _JOB_SEQ += 1
    _ensure_worker()
    if _WAKE is not None:
        _WAKE.set()


def _ensure_worker() -> None:
    """懒启动 worker 协程（首次入队时启动，随主事件循环存活）。

    asyncio.Event 延迟到此处创建：模块导入时无运行中的事件循环，
    旧版 Python 在无 loop 时创建 Event 会绑定错误的 loop。
    """
    global _WORKER, _WAKE
    if _WAKE is None:
        _WAKE = asyncio.Event()
    if _WORKER is None or _WORKER.done():
        _WAKE.set()  # 兜底唤醒（worker 重启时队列里可能有残留任务）
        _WORKER = asyncio.create_task(_queue_worker())


async def _queue_worker() -> None:
    """入库队列 worker：按优先级串行执行任务（普通 → 低优先级）。

    heap 取最小 (priority, seq)：低优先级任务排队期间，后来的普通任务
    插队先执行；正在执行的任务不被抢占（Chroma 写入不可并发打断）。
    """
    while True:
        while _PENDING:
            _, seq, name = heapq.heappop(_PENDING)
            factory = _JOBS.pop(seq, None)
            if factory is None:
                continue
            logger.info("入库任务开始: %s (队列剩余 %d)", name, len(_PENDING))
            try:
                await factory()
            except Exception:
                logger.exception("入库任务执行失败: %s", name)
        _WAKE.clear()
        if not _PENDING:  # double-check：clear 后若又有入队（Event 已被 set 过），继续处理
            await _WAKE.wait()


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
    stage: Optional[str] = None       # 单文件任务的细粒度阶段（parsing/ocr:x/y/pages/chunks/done…）
    priority: Optional[str] = None    # normal | low
    results: list[dict]        # [{source, chunks}]
    failed: list[dict]         # [{source, error}]


class AsyncIngestResponse(BaseModel):
    """单文件异步入库受理结果（层2）：上传受理即返回，前端轮询进度。"""
    task_id: str
    filename: str
    size_mb: float
    priority: str              # normal | low（层4：≥ LOW_PRIORITY_MB 走低优先级）
    pipeline: str              # stream=按页流水线（PDF 大文件）/ full=全量管线
    status: str = "queued"


def _is_low_priority(size_bytes: int) -> bool:
    """层4 判定：文件大小 ≥ LOW_PRIORITY_MB 进低优先级队列。

    受理时以文件大小为代理（此时无法预知是否扫描件）——扫描版 PDF
    通常文件体积大，阈值可在 .env 调整。
    """
    return size_bytes >= LOW_PRIORITY_MB * 1024 * 1024


def _use_stream_pipeline(size_bytes: int, filename: str) -> bool:
    """层3 判定：PDF 且大小 ≥ ASYNC_THRESHOLD_MB 走按页流水线（内存峰值降一批页）。"""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext == ".pdf" and size_bytes >= ASYNC_THRESHOLD_MB * 1024 * 1024


async def _run_single_task(
    task_id: str,
    tmp_path: Path,
    filename: str,
    user: str,
    replace: bool,
    doc_date: Optional[str],
    doc_version: Optional[str],
    use_stream: bool,
):
    """后台执行单个大文件入库（在线程池跑阻塞管线，stage 实时写入任务快照）。"""
    info = TASKS.get(task_id) or {}
    info["stage"] = "starting"

    def cb(msg: str) -> None:
        info["stage"] = msg

    try:
        fn = rag_engine.ingest_path_stream if use_stream else rag_engine.ingest_path
        result = await asyncio.to_thread(
            fn, str(tmp_path), filename, cb, user, replace, doc_date, doc_version,
        )
        info["results"].append({"source": filename, "chunks": result["chunks"]})
        info["stage"] = "done"
        logger.info(
            "异步入库完成: task=%s, user=%s, %s, %d 块, 管线=%s",
            task_id, user, filename, result["chunks"], "stream" if use_stream else "full",
        )
    except Exception as e:
        logger.exception("异步入库失败: task=%s, %s", task_id, filename)
        info["failed"].append({"source": filename, "error": str(e)})
        info["stage"] = "error"
    finally:
        info["status"] = "completed"
        info["done"] = 1
        info["current"] = None
        tmp_path.unlink(missing_ok=True)


@router.post("/ingest/async", response_model=AsyncIngestResponse)
async def ingest_async(
    file: UploadFile = File(...),
    replace: bool = Form(False),
    doc_date: Optional[str] = Form(None),
    doc_version: Optional[str] = Form(None),
    x_user: Optional[str] = Header(None),
):
    """大文件异步入库（层2）：校验落盘后立即返回 task_id，后台排队处理。

    - 层4：文件 ≥ LOW_PRIORITY_MB 进低优先级队列（普通任务优先）
    - 层3：PDF ≥ ASYNC_THRESHOLD_MB 走按页流水线（内存峰值降一批页）
    - 进度：轮询 GET /tasks/{task_id} 或订阅 SSE GET /tasks/{task_id}/events
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    _validate_ext(file.filename)
    user = _resolve_user(x_user)
    _validate_arbiter_fields(doc_date, doc_version)

    if not replace and rag_engine.get_store(user).has_document(file.filename):
        raise HTTPException(
            status_code=409,
            detail=f"同名文档已存在: {file.filename}，请使用替换上传(replace=true)",
        )

    try:
        tmp_path = await _save_upload_to_disk(file, UPLOAD_TMP_DIR)
    except FileTooLargeError as e:
        limit_mb = e.args[0] / 1024 / 1024
        raise HTTPException(
            status_code=413,
            detail=f"文件超过大小上限 {limit_mb:.0f}MB，请压缩或拆分后再上传",
        )

    size_bytes = tmp_path.stat().st_size
    priority = "low" if _is_low_priority(size_bytes) else "normal"
    use_stream = _use_stream_pipeline(size_bytes, file.filename)

    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {
        "status": "processing",
        "total": 1,
        "done": 0,
        "current": file.filename,
        "stage": "queued",
        "priority": priority,
        "results": [],
        "failed": [],
    }

    async def _job() -> None:
        await _run_single_task(
            task_id, tmp_path, file.filename, user, replace,
            doc_date, doc_version, use_stream,
        )

    _job.__name__ = f"single:{file.filename}"
    _submit_job(1 if priority == "low" else 0, _job)

    logger.info(
        "异步入库受理: task=%s, user=%s, %s, %.1fMB, 优先级=%s, 管线=%s",
        task_id, user, file.filename, size_bytes / 1024 / 1024, priority,
        "stream" if use_stream else "full",
    )
    return AsyncIngestResponse(
        task_id=task_id,
        filename=file.filename,
        size_mb=round(size_bytes / 1024 / 1024, 2),
        priority=priority,
        pipeline="stream" if use_stream else "full",
    )


async def _process_batch_task(task_id: str, user: str, replace: bool, items: list[tuple[Path, str]]):
    """后台工作协程：逐份解析入库（串行，尊重 Chroma 写锁），实时更新任务状态。

    批内大文件（≥ ASYNC_THRESHOLD_MB 的 PDF）自动切换按页流水线（层3）。
    """
    info = TASKS[task_id]
    try:
        for tmp_path, source in items:
            info["current"] = source
            try:
                # 同名冲突：批量模式下不整体报错，仅该文件计失败（替换由引擎内执行）
                if not replace and rag_engine.get_store(user).has_document(source):
                    raise ValueError("同名文档已存在（可用替换模式重传）")

                # 阻塞型解析/embedding 放线程池，避免卡住进度查询接口；
                # 大文件 PDF 走按页流水线（内存峰值从整本降到一批页）
                use_stream = _use_stream_pipeline(tmp_path.stat().st_size, source)
                fn = rag_engine.ingest_path_stream if use_stream else rag_engine.ingest_path
                result = await asyncio.to_thread(
                    fn, str(tmp_path), source, None, user, replace,
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
        # 入队调度（层4）：批量任务按总大小定优先级，与单文件任务统一串行执行，
        # 消除并发写 Chroma 的索引损坏风险
        total_bytes = sum(p.stat().st_size for p, _ in accepted)
        batch_priority = 1 if _is_low_priority(total_bytes) else 0

        async def _job() -> None:
            await _process_batch_task(task_id, user, replace, accepted)

        _job.__name__ = f"batch:{len(accepted)}files"
        _submit_job(batch_priority, _job)
        priority_label = "low" if batch_priority else "normal"
    else:
        TASKS[task_id]["status"] = "completed"
        shutil.rmtree(task_dir, ignore_errors=True)
        priority_label = "-"

    logger.info(
        "批量导入受理: task=%s, user=%s, 共%d份, 受理%d, 拒收%d, 优先级=%s",
        task_id, user, len(files), len(accepted), len(rejected), priority_label,
    )
    return BatchIngestResponse(task_id=task_id, total=len(files),
                               accepted=len(accepted), rejected=len(rejected))


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task_status(task_id: str):
    """查询入库任务进度（前端轮询；单文件任务含细粒度 stage）。"""
    info = TASKS.get(task_id)
    if not info:
        raise HTTPException(status_code=404, detail="任务不存在或已过期（任务表在内存中，服务重启即清空）")
    return TaskStatusResponse(
        task_id=task_id,
        status=info["status"],
        total=info["total"],
        done=info["done"],
        current=info["current"],
        stage=info.get("stage"),
        priority=info.get("priority"),
        results=info["results"],
        failed=info["failed"],
    )


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str):
    """入库任务 SSE 进度流（层2 的进度通道，复用 event-stream 形态）。

    事件：stage（阶段变化，含 status/done/total 快照）→ final（完成，含结果）。
    与 /ingest/stream 同为 SSE；轮询 /tasks/{task_id} 与本端点二选一。
    """
    info = TASKS.get(task_id)
    if not info:
        raise HTTPException(status_code=404, detail="任务不存在或已过期（任务表在内存中，服务重启即清空）")

    async def event_generator():
        last_stage = object()  # 哨兵：保证首帧必发
        while True:
            stage = info.get("stage") or info.get("current") or "processing"
            if stage != last_stage:
                last_stage = stage
                yield _sse("stage", {
                    "stage": stage,
                    "status": info["status"],
                    "done": info["done"],
                    "total": info["total"],
                })
            if info["status"] == "completed":
                yield _sse("final", {
                    "results": info["results"],
                    "failed": info["failed"],
                    "stage": info.get("stage"),
                })
                return
            await asyncio.sleep(0.3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, x_user: Optional[str] = Header(None)):
    """问答 SSE 状态进度流：检索中 → 重排中 → 生成中，最后 final 返回完整 ChatResponse。

    与 /ingest/stream 同为 event-stream 形态；/chat 的 JSON 行为保持不变。
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")

    user = _resolve_user(x_user)
    queue: asyncio.Queue = asyncio.Queue()

    def progress_cb(evt: dict) -> None:
        queue.put_nowait(evt)

    async def event_generator():
        # 先推首条状态再启动任务：检索是同步阻塞调用，先让用户看到"正在检索"
        last = {"stage": "retrieving", "message": "正在检索知识库…"}
        yield _sse("status", last)

        task = asyncio.create_task(
            rag_engine.chat(req.question, history=req.history, user=user, progress_cb=progress_cb)
        )
        getter: Optional[asyncio.Task] = None
        try:
            # 状态一到就推送，不依赖固定间隔轮询（避免瞬时状态被覆盖丢失）
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    evt = getter.result()
                    if evt != last:
                        last = evt
                        yield _sse("status", evt)
                else:
                    getter.cancel()
                if task in done:
                    break

            await asyncio.sleep(0)  # 让已入队的状态先刷出
            while not queue.empty():
                evt = queue.get_nowait()
                if evt != last:
                    last = evt
                    yield _sse("status", evt)

            try:
                result = task.result()
            except ValueError as e:
                yield _sse("error", {"stage": "error", "error": str(e)})
            except Exception as e:
                logger.exception("问答失败")
                yield _sse("error", {"stage": "error", "error": f"问答失败: {e}"})
            else:
                payload = ChatResponse(
                    answer=result["answer"],
                    citations=result["citations"],
                    reflection=result.get("reflection", {}),
                )
                yield _sse("final", payload.model_dump())
        finally:
            if getter is not None and not getter.done():
                getter.cancel()
            if not task.done():
                task.cancel()  # 客户端断开时停止后续流程

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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

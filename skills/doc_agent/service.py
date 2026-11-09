"""智能文档转换 Agent FastAPI 入口。"""

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from common.auth import current_user
from common import chat_memory
from common.chat_memory import session_header
from skills.doc_agent import engine
from skills.doc_agent.config import OUTPUT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agent_app")

# 路由抽象成 APIRouter：独立启动（本文件 app）与 gateway 统一入口复用同一份
router = APIRouter()

# 支持的源文件后缀（与 planner.SUPPORTED_FORMATS 对齐）。
# 未知后缀直接拒绝：历史上会退化成“按 UTF-8 文本读取”，把 .bin 等二进制文件
# 当文本转换成功，产出垃圾文档。
_SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".doc", ".html", ".htm", ".xlsx", ".xls", ".csv",
    ".txt", ".md", ".markdown",
}


def _validate_source_file(filename: str) -> None:
    """校验源文件后缀；不支持时返回 400，不做文本兜底。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="不支持的文件格式：%s；支持 pdf/docx/html/xlsx/csv/txt/md"
            % (suffix or "（无扩展名）"),
        )


def _friendly_convert_error(raw: str) -> str:
    """把底层异常转成可读中文，不向客户端暴露临时路径与英文堆栈。"""
    text = raw or ""
    lowered = text.lower()
    if "不支持的文件格式" in text:
        return text
    if "package not found" in lowered or "packagenotfound" in lowered or "not a zip file" in lowered:
        return "文件已损坏或不是有效的 docx（解压失败），请重新导出后上传"
    if "unexpected eof" in lowered or "broken" in lowered:
        return "文件不完整或已损坏，无法解析"
    if "filedataerror" in lowered or "invalid" in lowered:
        return "文件内容与扩展名不符或已损坏，无法解析"
    return "转换失败（详细原因已记录到服务日志）"


# ----------------------------------------------------------------------
# 上传文件敏感信息扫描：检测身份证号、手机号、API Key、银行卡号等
# 策略：检测到敏感信息时记录日志并返回警告（不阻断转换，因为客户可能确实需要转换这些文件；
#       生产环境可改为直接拒绝）。扫描仅在内存中进行，不留盘。
# ----------------------------------------------------------------------
_SENSITIVE_PATTERNS = {
    "身份证号": re.compile(r'(?<!\d)[1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)'),
    "手机号": re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),
    "API Key": re.compile(r'sk-[A-Za-z0-9]{20,}'),
    "银行卡号": re.compile(r'(?<!\d)\d{16,19}(?!\d)'),
}


def scan_sensitive_info(content: bytes) -> list[str]:
    """扫描文件内容中的敏感信息，返回检测到的敏感类型列表。

    对二进制文件做尽力扫描（utf-8 解码忽略错误），可能漏检但不会误报。
    """
    try:
        text = content.decode("utf-8", errors="ignore")
    except Exception:
        return []

    found: list[str] = []
    for name, pattern in _SENSITIVE_PATTERNS.items():
        if pattern.search(text):
            found.append(name)
    return found


async def _read_upload(file: UploadFile) -> bytes:
    """上传前置检查：文件名/后缀/非空/敏感信息扫描，通过后返回文件字节。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")
    _validate_source_file(file.filename)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")

    # 敏感信息扫描（仅记录日志，不阻断转换；生产可改为拒绝）
    sensitive = scan_sensitive_info(content)
    if sensitive:
        logger.warning(
            "上传文件含敏感信息类型: %s, file=%s", sensitive, file.filename
        )
    return content


class ConvertResponse(BaseModel):
    status: str
    plan: dict
    output_filename: str = ""
    validation: dict = {}
    error: str = ""
    retry_count: int = 0
    reflection_log: list = []
    # 会话记忆：是否沿用了上文的文件、提示文案、本会话已记住多少轮
    used_memory: bool = False
    memory_hint: str = ""
    memory_turns: int = 0
    message: str = ""


def _convert_failed(result: dict, *, used_memory: bool = False,
                    memory_hint: str = "") -> ConvertResponse:
    """转换失败响应：统一脱敏，不把内部堆栈/临时路径回给客户端。"""
    logger.error("转换失败: %s, 重试次数: %d", result.get("error"), result.get("retry_count", 0))
    return ConvertResponse(
        status="error",
        plan=result.get("plan") or {},
        error=_friendly_convert_error(result.get("error", "")),
        retry_count=result.get("retry_count", 0),
        used_memory=used_memory,
        memory_hint=memory_hint,
    )


def _convert_succeeded(result: dict, memory_turns: int, *, used_memory: bool = False,
                       memory_hint: str = "") -> ConvertResponse:
    """转换成功响应：统一组装产物信息与会话记忆提示。"""
    return ConvertResponse(
        status="ok",
        plan=result["plan"],
        output_filename=result["output_filename"],
        validation=result["validation"],
        retry_count=result.get("retry_count", 0),
        used_memory=used_memory,
        memory_hint=memory_hint,
        memory_turns=memory_turns,
    )


# ----------------------------------------------------------------------
# 会话记忆（文档转换）：记住最近一次文件、目标格式、产物文件名
# ----------------------------------------------------------------------
_DOC_META_PATTERNS = [
    re.compile(r"(上(次|一次)|刚才|刚刚|前面|上一次).{0,12}(转|转换|导出).{0,12}(什么|哪个|哪些|啥)"),
    re.compile(r"(上(次|一次)|刚才|刚刚|上一次).{0,6}(文件|产物|格式|结果).{0,8}(是什么|是哪个|什么|哪个)"),
    re.compile(r"(刚才|上一次|上次).{0,6}那个文件.{0,8}(叫什么|是什么|哪)"),
]


def _is_doc_meta(instruction: str) -> bool:
    text = (instruction or "").strip()
    if not text or len(text) > 80:
        return False
    return any(p.search(text) for p in _DOC_META_PATTERNS)


def _doc_meta_answer(user: str, session_id: Optional[str]) -> str:
    """回答"上一次转出来的是什么格式/哪个文件"，来源标注为对话记忆。"""
    turns = chat_memory.get_turns(user, session_id, "doc")
    if not turns:
        return "（来自对话记忆）当前会话还没有转换记录，请先上传一个文件。"
    last = turns[-1]
    extra = last.get("extra") or {}
    return (
        "（来自对话记忆）上一次转换使用的是「%s」，目标格式 %s，产物文件名 %s。"
        % (
            extra.get("filename", "未知文件"),
            extra.get("target_format", "未知"),
            extra.get("output_filename", "未知"),
        )
    )


def _remember_conversion(user: str, session_id: Optional[str], content: bytes,
                         filename: str, instruction: str, result: dict) -> int:
    """转换成功后写入会话记忆：文件字节（供"再把它转成…"复用）+ 轮次摘要。"""
    if not session_id:
        return 0
    plan = result.get("plan") or {}
    src = plan.get("source_format", "")
    tgt = plan.get("target_format", "")
    out = result.get("output_filename", "")
    chat_memory.store_blob(
        user, session_id, "doc", filename, content,
        {"target_format": tgt, "output_filename": out, "instruction": instruction},
    )
    return chat_memory.append_turn(
        user, session_id, "doc", instruction,
        "转换成功：%s（%s → %s），产物 %s" % (filename, src, tgt, out),
        {"filename": filename, "source_format": src, "target_format": tgt,
         "output_filename": out},
    )


# ----------------------------------------------------------------------
# 异步转换：提交即返回 task_id，后台执行，前端轮询进度
# （大文件/慢转换不再长时间占用 HTTP 连接；任务表内存级，重启即清空，
#   与 RAG 批量导入任务表同口径，生产可换 Redis + 任务队列）
# ----------------------------------------------------------------------
DOC_TASKS: dict[str, dict] = {}


class ConvertAsyncResponse(BaseModel):
    task_id: str
    status: str          # processing
    filename: str


class DocTaskStatusResponse(BaseModel):
    task_id: str
    status: str                     # processing | completed | error
    filename: str
    result: Optional[dict] = None   # 完成时为 ConvertResponse 同构结果
    error: str = ""


async def _run_doc_task(task_id: str, content: bytes, filename: str, instruction: str,
                        user: str = "", session_id: Optional[str] = None):
    """后台工作协程：转换是 CPU 密集（PDF/DOCX 解析），放线程池避免卡住轮询接口。"""
    info = DOC_TASKS[task_id]
    try:
        result = await asyncio.to_thread(engine.convert, content, filename, instruction)
        info["result"] = result
        if result.get("status") == "ok":
            info["status"] = "completed"
            # 成功后才写入会话记忆：后续可直接说"再把它转成 PDF"
            info["memory_turns"] = _remember_conversion(
                user, session_id, content, filename, instruction, result
            )
            result["memory_hint"] = "已记住本次转换，可直接说「再把它转成 …」"
            result["memory_turns"] = info["memory_turns"]
        else:
            info["status"] = "error"
            info["error"] = _friendly_convert_error(result.get("error", "转换失败"))
        logger.info("异步转换完成: task=%s, status=%s", task_id, info["status"])
    except Exception as e:
        logger.exception("异步转换任务异常: task=%s", task_id)
        info["status"] = "error"
        info["error"] = _friendly_convert_error(str(e))


@router.post("/convert/async", response_model=ConvertAsyncResponse)
async def convert_async(
    file: UploadFile = File(...),
    instruction: str = Form(...),
    user: str = Depends(current_user),
    session_id: Optional[str] = Depends(session_header),
):
    """提交异步转换任务：校验后立即返回 task_id，转换在后台执行，进度轮询 /tasks/{task_id}。"""
    content = await _read_upload(file)

    task_id = uuid.uuid4().hex[:12]
    DOC_TASKS[task_id] = {
        "task_id": task_id,
        "status": "processing",
        "filename": file.filename,
        "result": None,
        "error": "",
        "user": user,
    }
    asyncio.create_task(
        _run_doc_task(task_id, content, file.filename, instruction, user, session_id)
    )
    logger.info("异步转换受理: task=%s, file=%s, instruction=%s", task_id, file.filename, instruction)
    return ConvertAsyncResponse(task_id=task_id, status="processing", filename=file.filename)


@router.get("/tasks/{task_id}", response_model=DocTaskStatusResponse)
def get_doc_task(task_id: str, user: str = Depends(current_user)):
    """查询异步转换任务状态（前端 1 秒轮询）。

    任务按用户隔离：跨用户访问返回 404（不暴露任务是否存在）。
    """
    info = DOC_TASKS.get(task_id)
    if not info or info.get("user") != user:
        raise HTTPException(status_code=404, detail="任务不存在或已过期（任务表在内存中，服务重启即清空）")
    return DocTaskStatusResponse(**info)


@router.get("/health")
def health():
    return {"status": "ok", "service": "doc-convert-agent"}


@router.post("/convert", response_model=ConvertResponse)
async def convert(
    file: UploadFile = File(...),
    instruction: str = Form(...),
    user: str = Depends(current_user),
    session_id: Optional[str] = Depends(session_header),
):
    """上传文档 + 自然语言指令，执行转换；成功后写入会话记忆。"""
    content = await _read_upload(file)

    logger.info("转换请求: file=%s, instruction=%s", file.filename, instruction)

    result = engine.convert(content, file.filename, instruction)

    if result["status"] == "error":
        return _convert_failed(result)

    memory_turns = _remember_conversion(
        user, session_id, content, file.filename, instruction, result
    )
    logger.info(
        "转换成功: %s → %s, validation=%s, 重试次数=%d",
        result["plan"]["source_format"],
        result["plan"]["target_format"],
        result["validation"]["passed"],
        result.get("retry_count", 0),
    )
    return _convert_succeeded(
        result, memory_turns,
        memory_hint=("已记住本次转换，可直接说「再把它转成 …」" if session_id else ""),
    )


@router.post("/convert/followup", response_model=ConvertResponse)
async def convert_followup(
    instruction: str = Form(...),
    user: str = Depends(current_user),
    session_id: Optional[str] = Depends(session_header),
):
    """指代型转换指令：不用重复上传文件，沿用本会话最近一次的文件。

    支持：
      - "再把它转成 PDF"（沿用上一次文件与内容）
      - "上一次转出来的是什么格式"（直接基于会话记忆回答，不执行转换）
    无法确定文件时不猜测，返回明确提示，要求先上传。
    """
    text = (instruction or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="指令不能为空")
    if not session_id:
        raise HTTPException(status_code=400, detail="缺少 X-Session-Id，无法使用会话记忆；请刷新页面后重试")

    # 元问题：直接回答，不跑转换
    if _is_doc_meta(text):
        turns = chat_memory.get_turns(user, session_id, "doc")
        return ConvertResponse(
            status="ok",
            plan={},
            message=_doc_meta_answer(user, session_id),
            used_memory=True,
            memory_turns=len(turns),
        )

    blob = chat_memory.get_blob(user, session_id, "doc")
    if not blob:
        raise HTTPException(
            status_code=400,
            detail="当前会话还没有可沿用的文件：请先上传一次，或把文件名说完整。",
        )

    filename = blob["filename"]
    content = blob["content"]
    logger.info("沿用会话文件转换: file=%s, instruction=%s", filename, text)

    result = await asyncio.to_thread(engine.convert, content, filename, text)

    if result["status"] == "error":
        return _convert_failed(
            result,
            used_memory=True,
            memory_hint="本次沿用了上文的文件：%s" % filename,
        )

    memory_turns = _remember_conversion(user, session_id, content, filename, text, result)
    plan = result["plan"]
    return _convert_succeeded(
        result, memory_turns,
        used_memory=True,
        memory_hint="本次沿用了上文的文件：%s（%s → %s）" % (
            filename, plan.get("source_format", ""), plan.get("target_format", "")),
    )


@router.get("/session")
def doc_session_info(user: str = Depends(current_user),
                     session_id: Optional[str] = Depends(session_header)):
    """查询文档转换会话记忆：轮数 + 最近一次文件信息（不返回文件内容）。"""
    info = chat_memory.stats(user, session_id, "doc")
    blob = chat_memory.get_blob(user, session_id, "doc")
    if blob:
        meta = blob.get("meta") or {}
        info.update({
            "last_filename": blob.get("filename", ""),
            "target_format": meta.get("target_format", ""),
            "output_filename": meta.get("output_filename", ""),
        })
    return info


@router.delete("/session")
def doc_session_clear(user: str = Depends(current_user),
                      session_id: Optional[str] = Depends(session_header)):
    """清空文档转换会话记忆（轮次 + 上次文件缓存）。"""
    cleared = chat_memory.clear(user, session_id, "doc")
    return {"ok": True, "cleared": cleared}


@router.get("/download/{filename}")
async def download(filename: str):
    """下载转换结果文件。"""
    # 安全检查：防止路径遍历
    safe_name = Path(filename).name
    file_path = OUTPUT_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=str(file_path), filename=safe_name)


# ---- 独立启动形态（F1 保底，端口 8002）----
# gateway 一体化模式下由 gateway/app.py include 上面的 router，不走这里
app = FastAPI(title="智能文档转换 Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("skills.doc_agent.service:app", host="0.0.0.0", port=8002, reload=False, workers=2)

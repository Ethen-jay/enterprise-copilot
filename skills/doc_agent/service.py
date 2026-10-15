"""智能文档转换 Agent FastAPI 入口。"""

import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from skills.doc_agent import engine
from skills.doc_agent.config import OUTPUT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agent_app")

# 路由抽象成 APIRouter：独立启动（本文件 app）与 gateway 统一入口复用同一份
router = APIRouter()


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


class ConvertResponse(BaseModel):
    status: str
    plan: dict
    output_filename: str = ""
    validation: dict = {}
    error: str = ""
    retry_count: int = 0
    reflection_log: list = []


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


async def _run_doc_task(task_id: str, content: bytes, filename: str, instruction: str):
    """后台工作协程：转换是 CPU 密集（PDF/DOCX 解析），放线程池避免卡住轮询接口。"""
    info = DOC_TASKS[task_id]
    try:
        result = await asyncio.to_thread(engine.convert, content, filename, instruction)
        info["result"] = result
        if result.get("status") == "ok":
            info["status"] = "completed"
        else:
            info["status"] = "error"
            info["error"] = result.get("error", "转换失败")
        logger.info("异步转换完成: task=%s, status=%s", task_id, info["status"])
    except Exception as e:
        logger.exception("异步转换任务异常: task=%s", task_id)
        info["status"] = "error"
        info["error"] = str(e)


@router.post("/convert/async", response_model=ConvertAsyncResponse)
async def convert_async(file: UploadFile = File(...), instruction: str = Form(...)):
    """提交异步转换任务：校验后立即返回 task_id，转换在后台执行，进度轮询 /tasks/{task_id}。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")

    # 敏感信息扫描（仅记录日志，不阻断转换；生产可改为拒绝）
    sensitive = scan_sensitive_info(content)
    if sensitive:
        logger.warning(
            "上传文件含敏感信息类型: %s, file=%s", sensitive, file.filename
        )

    task_id = uuid.uuid4().hex[:12]
    DOC_TASKS[task_id] = {
        "task_id": task_id,
        "status": "processing",
        "filename": file.filename,
        "result": None,
        "error": "",
    }
    asyncio.create_task(_run_doc_task(task_id, content, file.filename, instruction))
    logger.info("异步转换受理: task=%s, file=%s, instruction=%s", task_id, file.filename, instruction)
    return ConvertAsyncResponse(task_id=task_id, status="processing", filename=file.filename)


@router.get("/tasks/{task_id}", response_model=DocTaskStatusResponse)
def get_doc_task(task_id: str):
    """查询异步转换任务状态（前端 1 秒轮询）。"""
    info = DOC_TASKS.get(task_id)
    if not info:
        raise HTTPException(status_code=404, detail="任务不存在或已过期（任务表在内存中，服务重启即清空）")
    return DocTaskStatusResponse(**info)


@router.get("/health")
def health():
    return {"status": "ok", "service": "doc-convert-agent"}


@router.post("/convert", response_model=ConvertResponse)
async def convert(
    file: UploadFile = File(...),
    instruction: str = Form(...),
):
    """上传文档 + 自然语言指令，执行转换。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")

    # 敏感信息扫描（仅记录日志，不阻断转换；生产可改为拒绝）
    sensitive = scan_sensitive_info(content)
    if sensitive:
        logger.warning(
            "上传文件含敏感信息类型: %s, file=%s", sensitive, file.filename
        )

    logger.info("转换请求: file=%s, instruction=%s", file.filename, instruction)

    result = engine.convert(content, file.filename, instruction)

    if result["status"] == "error":
        logger.error("转换失败: %s, 重试次数: %d", result.get("error"), result.get("retry_count", 0))
        return ConvertResponse(
            status="error",
            plan=result["plan"],
            error=result.get("error", ""),
            retry_count=result.get("retry_count", 0),
        )

    logger.info(
        "转换成功: %s → %s, validation=%s, 重试次数=%d",
        result["plan"]["source_format"],
        result["plan"]["target_format"],
        result["validation"]["passed"],
        result.get("retry_count", 0),
    )
    return ConvertResponse(
        status="ok",
        plan=result["plan"],
        output_filename=result["output_filename"],
        validation=result["validation"],
        retry_count=result.get("retry_count", 0),
    )


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

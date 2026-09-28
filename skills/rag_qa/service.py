"""RAG FastAPI 服务入口：/ingest、/chat。独立于 main.py。"""

import json
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from skills.rag_qa import rag_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rag_app")

# 路由抽象成 APIRouter：独立启动（本文件 app）与 gateway 统一入口复用同一份
router = APIRouter()


def _resolve_user(x_user: Optional[str]) -> str:
    """从 X-User 请求头解析用户标识，缺省返回 default（沿用原共享知识库）。"""
    return (x_user or "").strip() or "default"


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


@router.get("/health")
def health():
    """健康检查。"""
    return {"status": "ok", "service": "rag"}


@router.post("/ingest", response_model=IngestResponse)
async def ingest(file: UploadFile = File(...), x_user: Optional[str] = Header(None)):
    """上传文档并入库：解析 → 分块 → embedding → 该用户专属 Chroma collection。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    # 支持的格式
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in {".pdf", ".md", ".markdown", ".txt", ".json", ".jsonl"}:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}，支持 .pdf/.md/.txt/.json/.jsonl",
        )

    # 直接读取上传内容，不写磁盘
    content = await file.read()

    try:
        result = rag_engine.ingest(content, filename=file.filename, user=_resolve_user(x_user))
        logger.info("入库成功: user=%s, %s, %d 块", _resolve_user(x_user), result["filename"], result["chunks"])
        return IngestResponse(
            filename=result["filename"],
            chunks=result["chunks"],
            status="ok",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("入库失败")
        raise HTTPException(status_code=500, detail=f"入库失败: {e}")


@router.post("/ingest/stream")
async def ingest_stream(file: UploadFile = File(...), x_user: Optional[str] = Header(None)):
    """SSE 流式入库：实时推送解析/分块/embedding/存储进度。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in {".pdf", ".md", ".markdown", ".txt", ".json", ".jsonl"}:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}，支持 .pdf/.md/.txt/.json/.jsonl",
        )

    content = await file.read()
    filename = file.filename

    status_box = {"done": False, "result": None, "error": None}

    def progress_cb(msg: str):
        status_box["last_msg"] = msg

    import asyncio

    async def event_generator():
        # 在线程池中运行入库
        loop = asyncio.get_event_loop()
        task = loop.run_in_executor(
            None,
            lambda: rag_engine.ingest(content, filename=filename, progress_callback=progress_cb,
                                      user=_resolve_user(x_user)),
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

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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

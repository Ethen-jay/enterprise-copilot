"""智能文档转换 Agent FastAPI 入口。"""

import logging
import os
from pathlib import Path

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


class ConvertResponse(BaseModel):
    status: str
    plan: dict
    output_filename: str = ""
    validation: dict = {}
    error: str = ""
    retry_count: int = 0
    reflection_log: list = []


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

# -*- coding: utf-8 -*-
"""userdb.py —— 按用户隔离的数据源接入

背景：早期版本把 ic_substrate.db 写死为唯一数据源，前端状态条上的
“22 张表 / 快照日期”也跟着写死，与实际库无关。这里把“当前用户连的是哪个库”
做成显式状态，落盘在用户私有目录：

    data/userdb/<username>/connection.json   连接信息（kind / 原始文件名 / 接入时间）
    data/userdb/<username>/source.sqlite     用户上传的 SQLite 私有副本

支持两种数据源：
  - demo  ：内置示例库（data/db/ic_substrate.db，只读共享）
  - sqlite：用户上传的 .db/.sqlite/.sqlite3（每个用户一份私有副本，互不可见）

未接入时 resolve_path() 返回 None，取数链路据此给出“请先接入数据库”，
不再回落到任何默认库；不同用户之间的文件与连接信息物理隔离。

扫描结果缓存与指纹见 schema_scan.py；本模块只负责连接元数据。
"""

import json
import logging
import re
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DB_PATH, USERDB_DIR

logger = logging.getLogger("text2sql.userdb")

SQLITE_MAGIC = b"SQLite format 3\x00"
KIND_DEMO = "demo"
KIND_SQLITE = "sqlite"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_LOCK = threading.RLock()

# 当前请求线程绑定的数据源路径（executor / anchor 的默认取值）
_tls = threading.local()


class DataSourceConnectError(Exception):
    """接入失败；message 可直接展示，status_code 由接口层映射。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class NotConnectedError(Exception):
    """当前用户尚未接入任何数据库。"""


# ----------------------------------------------------------------------
# 线程级当前数据源（一次请求内由 service 绑定，避免函数签名到处传参）
# ----------------------------------------------------------------------
def set_current(path: Optional[Path]) -> None:
    _tls.path = Path(path) if path else None


def current_path() -> Optional[Path]:
    return getattr(_tls, "path", None)


def clear_current() -> None:
    _tls.path = None


# ----------------------------------------------------------------------
# 用户目录 / 连接元数据
# ----------------------------------------------------------------------
def user_dir(username: str) -> Path:
    safe = _SAFE_NAME_RE.sub("_", (username or "").strip()) or "unknown"
    return USERDB_DIR / safe


def _connection_file(username: str) -> Path:
    return user_dir(username) / "connection.json"


def get_connection(username: str) -> Optional[dict]:
    path = _connection_file(username)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("连接信息损坏，按未接入处理：%s", path)
        return None
    return data if isinstance(data, dict) else None


def _write_connection(username: str, data: dict) -> None:
    target = _connection_file(username)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def resolve_path(username: str) -> Optional[Path]:
    """返回当前用户实际可用的库文件；未接入或文件丢失返回 None。"""
    conn = get_connection(username)
    if not conn:
        return None
    kind = conn.get("kind")
    if kind == KIND_DEMO:
        return DB_PATH if DB_PATH.exists() else None
    if kind == KIND_SQLITE:
        stored = conn.get("stored") or "source.sqlite"
        path = user_dir(username) / stored
        return path if path.exists() else None
    return None


def describe(username: str) -> dict:
    """连接信息的可展示部分（不暴露服务器绝对路径）。"""
    conn = get_connection(username)
    path = resolve_path(username)
    if not conn or path is None:
        return {"connected": False, "kind": None, "filename": "", "connected_at": ""}
    return {
        "connected": True,
        "kind": conn.get("kind"),
        "filename": conn.get("filename") or path.name,
        "connected_at": conn.get("connected_at", ""),
        "size_bytes": path.stat().st_size,
    }


# ----------------------------------------------------------------------
# 接入 / 切换 / 断开
# ----------------------------------------------------------------------
def connect_demo(username: str) -> dict:
    """接入内置示例库。"""
    if not DB_PATH.exists():
        raise DataSourceConnectError("内置示例库不存在，无法接入", status_code=500)
    with _LOCK:
        _write_connection(username, {
            "kind": KIND_DEMO,
            "filename": "示例数据库（内置）",
            "connected_at": datetime.now().isoformat(timespec="seconds"),
        })
    return describe(username)


def _validate_sqlite(path: Path) -> None:
    try:
        with path.open("rb") as fh:
            head = fh.read(len(SQLITE_MAGIC))
    except OSError as e:
        raise DataSourceConnectError("文件读取失败，请重试") from e
    if head != SQLITE_MAGIC:
        raise DataSourceConnectError("不是有效的 SQLite 文件（文件头校验失败）")
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            tables = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        raise DataSourceConnectError("SQLite 文件无法打开：%s" % e) from e
    if not tables:
        raise DataSourceConnectError("文件中没有可用的数据表")


def connect_sqlite(username: str, upload_path: Path, filename: str) -> dict:
    """把上传的 SQLite 保存为用户私有副本并切换为当前数据源。"""
    _validate_sqlite(upload_path)
    # 展示名保留用户原始文件名（中文可用）；落盘固定为 source.sqlite，避免路径问题
    display = Path(filename).name.strip() or "source.sqlite"
    if len(display) > 120:
        display = display[-120:]
    target_dir = user_dir(username)
    target_dir.mkdir(parents=True, exist_ok=True)
    stored = "source.sqlite"
    target = target_dir / stored
    with _LOCK:
        tmp = target.with_suffix(".sqlite.tmp")
        shutil.copyfile(str(upload_path), str(tmp))
        tmp.replace(target)
        _write_connection(username, {
            "kind": KIND_SQLITE,
            "filename": display,
            "stored": stored,
            "connected_at": datetime.now().isoformat(timespec="seconds"),
        })
    logger.info("用户 %s 接入 SQLite 数据源：%s", username, display)
    return describe(username)


def disconnect(username: str) -> dict:
    """断开当前数据源（保留已上传文件，便于再次接入时不必重传）。"""
    with _LOCK:
        target = _connection_file(username)
        if target.exists():
            target.unlink()
    return describe(username)

# -*- coding: utf-8 -*-
"""common/users.py —— 真实账号体系（SQLite 持久化）

存储
  data/users.db
  - users(username PK, password_hash, display_name, role, created_at, disabled, token_version)
  - login_failures(username, ip, fails, locked_until, updated_at)

密码哈希
  PBKDF2-HMAC-SHA256，260000 轮，16 字节随机盐（标准库 hashlib，无需额外依赖）。
  存储格式自描述：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
  选择理由：不引入 bcrypt/argon2 依赖；格式自带算法与参数，后续调高轮数可平滑升级。

登录防护
  同一 username + IP 连续失败 5 次后锁定 5 分钟（LOGIN_MAX_FAILS / LOGIN_LOCK_SECONDS）。
  失败提示统一为“用户名或密码错误”，不区分账号是否存在；账号不存在时也做一次
  等价的哈希校验，避免用响应耗时探测账号。

令牌吊销
  users.token_version 随改密自增；JWT 的 ver claim 必须与库中一致，
  改密后旧令牌立即失效（见 common/auth.resolve_identity）。

首次启动
  用户库为空时自动创建 admin；密码取环境变量 ADMIN_PASSWORD，
  未配置则随机生成并只在启动日志打印一次，要求登录后立即修改。
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time
from typing import Optional

from common.config import PROJECT_ROOT

logger = logging.getLogger("gateway.auth")

DB_PATH = PROJECT_ROOT / "data" / "users.db"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128

PBKDF2_ITERATIONS = 260_000
PBKDF2_PREFIX = "pbkdf2_sha256"

LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 300

# 保留账号：不允许普通注册，避免与历史默认身份或服务账号冲突
RESERVED_USERNAMES = {"default", "service", "system"}

# 账号不存在时用于恒定时耗对比的假哈希（首次使用时初始化）
_dummy_hash: Optional[str] = None


class UserError(Exception):
    """业务可读的用户操作错误（status 对应 HTTP 状态码）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    """建表（幂等）。"""
    with _connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                display_name  TEXT NOT NULL DEFAULT '',
                role          TEXT NOT NULL DEFAULT 'user',
                created_at    TEXT NOT NULL,
                disabled      INTEGER NOT NULL DEFAULT 0,
                token_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS login_failures (
                username     TEXT NOT NULL,
                ip           TEXT NOT NULL,
                fails        INTEGER NOT NULL DEFAULT 0,
                locked_until REAL NOT NULL DEFAULT 0,
                updated_at   REAL NOT NULL,
                PRIMARY KEY (username, ip)
            );
            """
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "%s$%d$%s$%s" % (
        PBKDF2_PREFIX,
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码；格式不合法时返回 False（不抛异常，避免脏数据打挂登录）。"""
    try:
        prefix, iterations, salt_b64, hash_b64 = stored.split("$")
        if prefix != PBKDF2_PREFIX:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            (password or "").encode("utf-8"),
            base64.b64decode(salt_b64),
            int(iterations),
        )
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise UserError("用户名仅限字母、数字、下划线、连字符，长度 1-32 位")
    if username.lower() in RESERVED_USERNAMES:
        raise UserError("该用户名为系统保留账号，请换一个")
    return username


def validate_password(password: str) -> str:
    password = password or ""
    if len(password) < MIN_PASSWORD_LEN:
        raise UserError("密码至少 %d 位" % MIN_PASSWORD_LEN)
    if len(password) > MAX_PASSWORD_LEN:
        raise UserError("密码最长 %d 位" % MAX_PASSWORD_LEN)
    return password


def get_user(username: str) -> Optional[dict]:
    with _connect() as con:
        row = con.execute(
            "SELECT username, display_name, role, created_at, disabled, token_version "
            "FROM users WHERE username = ?",
            ((username or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def create_user(username: str, password: str, display_name: str = "", role: str = "user") -> dict:
    """创建用户；用户名重复（忽略大小写）抛 409。"""
    username = validate_username(username)
    validate_password(password)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with _connect() as con:
            con.execute(
                "INSERT INTO users (username, password_hash, display_name, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, hash_password(password), (display_name or "").strip()[:64], role, created_at),
            )
    except sqlite3.IntegrityError:
        raise UserError("用户名已存在，请直接登录或换一个", 409)
    return get_user(username)


def _failure_row(con, username: str, ip: str):
    return con.execute(
        "SELECT fails, locked_until FROM login_failures WHERE username = ? AND ip = ?",
        (username, ip),
    ).fetchone()


def _record_failure(username: str, ip: str) -> None:
    now = time.time()
    with _connect() as con:
        row = _failure_row(con, username, ip)
        fails = (row["fails"] if row else 0) + 1
        locked_until = now + LOGIN_LOCK_SECONDS if fails >= LOGIN_MAX_FAILS else 0
        con.execute(
            "INSERT INTO login_failures (username, ip, fails, locked_until, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(username, ip) DO UPDATE SET fails=excluded.fails, "
            "locked_until=excluded.locked_until, updated_at=excluded.updated_at",
            (username, ip, fails, locked_until, now),
        )


def _clear_failures(username: str, ip: str) -> None:
    with _connect() as con:
        con.execute(
            "DELETE FROM login_failures WHERE username = ? AND ip = ?", (username, ip)
        )


def _lock_remaining(username: str, ip: str) -> int:
    """剩余锁定秒数；未锁定返回 0。"""
    with _connect() as con:
        row = _failure_row(con, username, ip)
    if not row:
        return 0
    remaining = int(row["locked_until"] - time.time())
    return remaining if remaining > 0 else 0


def authenticate(username: str, password: str, ip: str = "") -> dict:
    """登录校验，成功返回用户信息；失败抛 UserError（429 表示已锁定）。

    失败提示统一“用户名或密码错误”，不暴露账号是否存在。
    """
    global _dummy_hash
    username = (username or "").strip()
    remaining = _lock_remaining(username, ip)
    if remaining > 0:
        raise UserError("尝试次数过多，请 %d 分钟后再试" % ((remaining + 59) // 60), 429)

    with _connect() as con:
        row = con.execute(
            "SELECT username, password_hash, display_name, role, created_at, "
            "disabled, token_version FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if row is None:
        if _dummy_hash is None:
            _dummy_hash = hash_password(secrets.token_urlsafe(16))
        verify_password(password or "", _dummy_hash)  # 恒定时耗，避免探测账号
        _record_failure(username, ip)
        raise UserError("用户名或密码错误", 401)

    if not verify_password(password or "", row["password_hash"]):
        _record_failure(username, ip)
        raise UserError("用户名或密码错误", 401)

    if row["disabled"]:
        raise UserError("账号已被禁用，请联系管理员", 403)

    _clear_failures(username, ip)
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "created_at": row["created_at"],
        "token_version": row["token_version"],
    }


def change_password(username: str, old_password: str, new_password: str) -> None:
    """校验旧密码后改密，并自增 token_version 使旧令牌立即失效。"""
    user = get_user(username)
    if not user:
        raise UserError("用户不存在", 404)
    with _connect() as con:
        row = con.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    if not row or not verify_password(old_password or "", row["password_hash"]):
        raise UserError("旧密码不正确", 400)
    validate_password(new_password)
    if verify_password(new_password, row["password_hash"]):
        raise UserError("新密码不能与旧密码相同", 400)
    with _connect() as con:
        con.execute(
            "UPDATE users SET password_hash = ?, token_version = token_version + 1 "
            "WHERE username = ?",
            (hash_password(new_password), username),
        )


def token_is_current(username: str, token_version: int) -> bool:
    """令牌里的 ver 是否与当前用户一致（改密后旧令牌失效）。"""
    user = get_user(username)
    if not user or user["disabled"]:
        return False
    return int(user["token_version"]) == int(token_version)


def bootstrap_admin() -> None:
    """首次启动（用户库为空）创建管理员账号。"""
    init_db()
    with _connect() as con:
        count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count:
        return
    password = (os_getenv("ADMIN_PASSWORD") or "").strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
    create_user("admin", password, display_name="管理员", role="admin")
    if generated:
        logger.warning(
            "首次启动已创建管理员账号 admin，初始密码：%s（仅本次打印，请立即登录并修改）",
            password,
        )
    else:
        logger.info("首次启动已创建管理员账号 admin（密码取自 ADMIN_PASSWORD）")


def os_getenv(name: str, default: str = "") -> str:
    """小包装便于测试替换；避免在函数内重复 import os。"""
    import os

    return os.getenv(name, default)

# -*- coding: utf-8 -*-
"""
common/auth.py —— 统一身份认证（JWT / HS256）

身份只从令牌 claims 派生，业务代码不再读取客户端传来的 X-User：
  1. 用户令牌：POST /login 校验账号（+密码/共享密钥）后签发，claims.sub 即账号，
     带 iat/exp/iss，默认 12 小时过期（AUTH_TOKEN_TTL_HOURS 可调）；
  2. 服务通道：X-API-Key（GATEWAY_API_KEYS）映射到固定服务账号
     （GATEWAY_SERVICE_ACCOUNT，默认 service）。服务账号有自己的知识库，
     不能冒充任意用户；
  3. 未认证访问受保护接口一律 401，不存在"缺 X-User 回退 default"。

密钥来源：AUTH_JWT_SECRET（推荐，≥32 字符）。未配置时为保证单机演示可启动，
退化为 data/.jwt_secret 本地持久密钥并打印告警；生产必须显式配置。
"""

import hmac
import logging
import os
import re
import secrets
import time
from typing import Optional

import jwt
from fastapi import HTTPException, Request

from common.config import GATEWAY_API_KEYS, PROJECT_ROOT

logger = logging.getLogger("gateway.auth")

_ALGORITHM = "HS256"
_ISSUER = "enterprise-copilot"
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

TOKEN_TTL_SECONDS = int(float(os.getenv("AUTH_TOKEN_TTL_HOURS", "12")) * 3600)
SERVICE_ACCOUNT = (os.getenv("GATEWAY_SERVICE_ACCOUNT", "service").strip() or "service")

# AUTH_USERS="Ethen:密码1,alice:密码2"（生产推荐）；未配置时退回共享 API Key 作为密码
_AUTH_USERS = {}
for _item in os.getenv("AUTH_USERS", "").split(","):
    _item = _item.strip()
    if not _item:
        continue
    _name, _, _pwd = _item.partition(":")
    if _ACCOUNT_RE.fullmatch(_name.strip()) and _pwd:
        _AUTH_USERS[_name.strip()] = _pwd


def _load_secret() -> str:
    """读取 JWT 签名密钥；未配置环境变量时退化为本地持久密钥（仅单机演示）。"""
    secret = os.getenv("AUTH_JWT_SECRET", "").strip()
    if secret:
        if len(secret) < 32:
            raise RuntimeError("AUTH_JWT_SECRET 长度至少 32 字符，请更换更安全的密钥")
        return secret

    secret_file = PROJECT_ROOT / "data" / ".jwt_secret"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(48)
    secret_file.write_text(generated, encoding="utf-8")
    logger.warning(
        "未配置 AUTH_JWT_SECRET，已生成 data/.jwt_secret 作为本地签名密钥；"
        "生产环境请在 .env 中显式配置至少 32 位随机密钥"
    )
    return generated


_SECRET = _load_secret()


def create_token(account: str) -> str:
    """为账号签发访问令牌（sub=账号，带 iat/exp/iss）。"""
    now = int(time.time())
    payload = {
        "sub": account,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "iss": _ISSUER,
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """校验签名/有效期/签发方，返回 claims；任何不合法都返回 None（不抛给调用方）。"""
    try:
        return jwt.decode(
            token,
            _SECRET,
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as e:
        logger.warning("令牌校验失败: %s", e)
        return None


def verify_credentials(account: str, password: str) -> Optional[str]:
    """校验登录凭据，成功返回规范化账号，失败返回 None。

    优先级：AUTH_USERS（账号:密码）→ GATEWAY_API_KEYS（共享密钥当密码）→
    都未配置时为演示模式（仅校验账号格式）。所有比较使用常量时间比较。
    """
    account = (account or "").strip()
    if not _ACCOUNT_RE.fullmatch(account):
        return None
    if _AUTH_USERS:
        expected = _AUTH_USERS.get(account)
        if expected is None or not hmac.compare_digest(password or "", expected):
            return None
        return account
    if GATEWAY_API_KEYS:
        if not any(hmac.compare_digest(password or "", k) for k in GATEWAY_API_KEYS):
            return None
        return account
    logger.warning("未配置 AUTH_USERS / GATEWAY_API_KEY，登录不校验密码（仅限演示）")
    return account


def password_required() -> bool:
    """登录是否需要密码（前端据此显隐密码输入框）。"""
    return bool(_AUTH_USERS or GATEWAY_API_KEYS)


def resolve_identity(authorization: str = "", api_key: str = "") -> tuple[str, str]:
    """按凭据派生身份，返回 (user, auth_type)；认证失败返回 ("", "")。

    - Authorization: Bearer <JWT> → claims.sub（用户令牌）
    - X-API-Key（配置了 GATEWAY_API_KEYS 时）→ 固定服务账号（服务间调用）
    客户端传来的 X-User 一律不参与身份判定。
    """
    token = ""
    auth = (authorization or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()

    if token:
        claims = decode_token(token)
        sub = str((claims or {}).get("sub", ""))
        if sub:
            return sub, "token"

    if api_key and any(hmac.compare_digest(api_key, k) for k in GATEWAY_API_KEYS):
        return SERVICE_ACCOUNT, "api_key"

    return "", ""


def current_user(request: Request) -> str:
    """FastAPI 依赖：从令牌（或服务 API Key）派生用户身份，未认证返回 401。

    优先读中间件写入的 state.user；独立启动（无网关中间件）时自行校验
    Authorization 头，保证鉴权不依赖某个部署形态。
    """
    user = getattr(request.state, "user", None)
    if user:
        return user

    user, auth_type = resolve_identity(
        request.headers.get("authorization", ""),
        request.headers.get("x-api-key", ""),
    )
    if user and (auth_type == "api_key" or _ACCOUNT_RE.fullmatch(user)):
        return user

    raise HTTPException(
        status_code=401,
        detail="未认证：请先登录获取访问令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )

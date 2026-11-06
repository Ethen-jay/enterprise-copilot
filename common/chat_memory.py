# -*- coding: utf-8 -*-
"""
common/chat_memory.py —— 会话级记忆（进程内内存，重启即丢）

设计要点：
  1. 键为 (user, session_id, scope)，user 来自 JWT，session_id 由前端生成、
     经 X-Session-Id 传入；scope 取 rag / doc / sql，三个技能互不串味。
  2. 三重上限：单会话轮次（CHAT_MEMORY_MAX_TURNS）、单会话字符数
     （CHAT_MEMORY_MAX_CHARS）、全局会话数（CHAT_MEMORY_MAX_SESSIONS，LRU 淘汰）；
     另设 TTL（CHAT_MEMORY_TTL_SECONDS）过期自动失效。
  3. 线程安全：所有读写在同一把 RLock 内完成（FastAPI 同步接口会跑在线程池）。
  4. 不落盘、不写日志明文：只记录轮次数与会话数，不打印问答内容。
  5. 只提供纯逻辑能力（轮次、上限、元问题识别、改写提示词）；真正调 LLM 的
     改写由各技能用自己的客户端执行，避免循环依赖。
"""

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from fastapi import Header

logger = logging.getLogger("chat_memory")

# 配置（环境变量可覆盖；默认值面向单机演示，生产可调大）
MEMORY_TTL_SECONDS = int(os.getenv("CHAT_MEMORY_TTL_SECONDS", "1800"))       # 30 分钟
MAX_TURNS = int(os.getenv("CHAT_MEMORY_MAX_TURNS", "20"))                    # 单会话最多 20 轮
MAX_CHARS = int(os.getenv("CHAT_MEMORY_MAX_CHARS", "20000"))                 # 单会话最多 2 万字
MAX_SESSIONS = int(os.getenv("CHAT_MEMORY_MAX_SESSIONS", "500"))             # 全局最多 500 个会话键
MAX_BLOB_BYTES = int(os.getenv("CHAT_MEMORY_MAX_BLOB_BYTES", str(64 * 1024 * 1024)))
MAX_BLOB_PER_FILE = int(os.getenv("CHAT_MEMORY_MAX_BLOB_PER_FILE", str(20 * 1024 * 1024)))

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_lock = threading.RLock()
# key -> {"turns": [ {q,a,at,extra} ], "chars": int, "updated": float}
_sessions: "OrderedDict[str, dict]" = OrderedDict()
# (user, session, scope) -> {"filename","content","meta"}；与文本轮次分开做字节预算
_blobs: "OrderedDict[tuple, dict]" = OrderedDict()
_blob_total = 0


# ----------------------------------------------------------------------
# 会话标识
# ----------------------------------------------------------------------
def normalize_session_id(raw: Optional[str]) -> Optional[str]:
    """校验前端传来的会话 id；不合法（含越权字符）时返回 None，等价于无记忆。

    不做 400：会话 id 只影响"是否启用记忆"，不应让业务请求失败。
    """
    if not raw:
        return None
    raw = raw.strip()
    if not _SESSION_RE.fullmatch(raw):
        return None
    return raw


def session_header(x_session_id: Optional[str] = Header(None, alias="X-Session-Id")) -> Optional[str]:
    """FastAPI 依赖：读取并校验 X-Session-Id；缺失/非法即关闭记忆（不报错）。"""
    return normalize_session_id(x_session_id)


def _key(user: str, session_id: str, scope: str) -> str:
    # user 来自服务端令牌，客户端无法伪造；scope 由服务端代码固定
    return "%s\x00%s\x00%s" % (scope, user, session_id)


def _now() -> float:
    return time.time()


def _purge_expired_locked() -> None:
    """清理过期会话（调用方必须已持锁）。"""
    now = _now()
    expired = [k for k, v in _sessions.items() if now - v["updated"] > MEMORY_TTL_SECONDS]
    for k in expired:
        _sessions.pop(k, None)
    blob_expired = [k for k, v in _blobs.items() if now - v["updated"] > MEMORY_TTL_SECONDS]
    global _blob_total
    for k in blob_expired:
        _blob_total -= len(_blobs.pop(k)["content"])


def _evict_locked() -> None:
    """全局会话数超限时按 LRU 淘汰（调用方必须已持锁）。"""
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


# ----------------------------------------------------------------------
# 轮次读写
# ----------------------------------------------------------------------
def append_turn(
    user: str,
    session_id: Optional[str],
    scope: str,
    question: str,
    answer: str,
    extra: Optional[dict] = None,
) -> int:
    """追加一轮问答，返回该会话当前轮数；session_id 为空时不做记忆。"""
    if not session_id or not user:
        return 0
    q = (question or "").strip()
    a = (answer or "").strip()
    if not q:
        return 0
    key = _key(user, session_id, scope)
    with _lock:
        _purge_expired_locked()
        item = _sessions.get(key)
        if item is None:
            item = {"turns": [], "chars": 0, "updated": _now()}
        item["turns"].append({
            "question": q,
            "answer": a,
            "at": _now(),
            "extra": dict(extra or {}),
        })
        item["chars"] += len(q) + len(a)
        item["updated"] = _now()
        # 轮次上限
        while len(item["turns"]) > MAX_TURNS:
            old = item["turns"].pop(0)
            item["chars"] -= len(old["question"]) + len(old["answer"])
        # 字符上限
        while item["turns"] and item["chars"] > MAX_CHARS:
            old = item["turns"].pop(0)
            item["chars"] -= len(old["question"]) + len(old["answer"])
        _sessions[key] = item
        _sessions.move_to_end(key)
        _evict_locked()
        return len(item["turns"])


def get_turns(user: str, session_id: Optional[str], scope: str) -> List[dict]:
    """取该会话的全部轮次（副本），无记忆返回空列表。"""
    if not session_id or not user:
        return []
    key = _key(user, session_id, scope)
    with _lock:
        _purge_expired_locked()
        item = _sessions.get(key)
        if item is None:
            return []
        item["updated"] = _now()
        _sessions.move_to_end(key)
        return [dict(t) for t in item["turns"]]


def get_history(user: str, session_id: Optional[str], scope: str, rounds: int = 3) -> List[dict]:
    """转成 LLM messages（最近 rounds 轮，按时间正序）。"""
    turns = get_turns(user, session_id, scope)
    if not turns:
        return []
    msgs: List[dict] = []
    for t in turns[-rounds:]:
        msgs.append({"role": "user", "content": t["question"]})
        if t["answer"]:
            msgs.append({"role": "assistant", "content": t["answer"]})
    return msgs


def stats(user: str, session_id: Optional[str], scope: str) -> dict:
    """会话记忆概况（给前端显示"已记住 N 轮"）。"""
    turns = get_turns(user, session_id, scope)
    return {
        "session_id": session_id or "",
        "scope": scope,
        "turns": len(turns),
        "rounds": len(turns),
        "enabled": bool(session_id),
    }


def clear(user: str, session_id: Optional[str] = None, scope: Optional[str] = None) -> int:
    """清空指定会话（可选只清某个 scope），返回清掉的键数量。"""
    if not user or not session_id:
        return 0
    removed = 0
    with _lock:
        for sc in ([scope] if scope else ["rag", "doc", "sql"]):
            key = _key(user, session_id, sc)
            if _sessions.pop(key, None) is not None:
                removed += 1
            if _blobs.pop((user, session_id, sc), None) is not None:
                removed += 1
    return removed


# ----------------------------------------------------------------------
# 文件缓存（文档转换"沿用上文文件"用）：只存最近一个文件，按字节预算
# ----------------------------------------------------------------------
def store_blob(user: str, session_id: Optional[str], scope: str,
               filename: str, content: bytes, meta: Optional[dict] = None) -> bool:
    """保存会话内最近一次文件（超单文件上限或总预算则放弃缓存，返回 False）。"""
    if not session_id or not user or not content:
        return False
    if len(content) > MAX_BLOB_PER_FILE:
        return False
    key = (user, session_id, scope)
    with _lock:
        global _blob_total
        _purge_expired_locked()
        old = _blobs.pop(key, None)
        if old:
            _blob_total -= len(old["content"])
        # 总预算不够时按 LRU 腾地方
        while _blob_total + len(content) > MAX_BLOB_BYTES and _blobs:
            _, drop = _blobs.popitem(last=False)
            _blob_total -= len(drop["content"])
        if _blob_total + len(content) > MAX_BLOB_BYTES:
            return False
        _blobs[key] = {
            "filename": filename,
            "content": content,
            "meta": dict(meta or {}),
            "updated": _now(),
        }
        _blob_total += len(content)
        return True


def get_blob(user: str, session_id: Optional[str], scope: str) -> Optional[dict]:
    """取会话内最近一次文件（含 content 字节）。"""
    if not session_id or not user:
        return None
    key = (user, session_id, scope)
    with _lock:
        _purge_expired_locked()
        item = _blobs.get(key)
        if item is None:
            return None
        item["updated"] = _now()
        _blobs.move_to_end(key)
        return item


# ----------------------------------------------------------------------
# 指代/省略识别与改写提示词
# ----------------------------------------------------------------------
# 含这些指代/省略标记时，才值得调用一次改写（省钱、降延迟）
_REFERENCE_RE = re.compile(
    r"(它|他|她|这个|那个|这些|那些|上述|该|此|前者|后者|刚才|刚刚|前面|上面|上次|上一次|同样)"
)
_ELLIPSIS_RE = re.compile(r"^(那|再|还|又|只|换|改|加|按|把|呢|然后|接着|具体|详细)")


def needs_rewrite(question: str) -> bool:
    """启发式判断当前问句是否依赖上文（指代/省略/过短追问）。"""
    q = (question or "").strip()
    if not q:
        return False
    if _REFERENCE_RE.search(q):
        return True
    if _ELLIPSIS_RE.search(q):
        return True
    # 过短且以问号结尾、又没有明确名词，视为省略追问
    if len(q) <= 8 and ("呢" in q or "吗" in q):
        return True
    return False


def format_turns_for_rewrite(turns: List[dict], scope: str, max_chars: int = 3000) -> str:
    """把轮次压成改写模型的上下文（只用于改写问句，不作为事实依据）。"""
    lines: List[str] = []
    for i, t in enumerate(turns[-3:], 1):
        q = (t.get("question") or "").strip()
        a = (t.get("answer") or "").strip().replace("\n", " ")
        extra = t.get("extra") or {}
        if scope == "sql":
            lines.append("第%d轮 用户：%s" % (i, q))
            if extra.get("sql"):
                lines.append("第%d轮 SQL：%s" % (i, str(extra["sql"])[:500]))
            if extra.get("tables"):
                lines.append("第%d轮 表：%s" % (i, "、".join(map(str, extra["tables"]))[:200]))
            if extra.get("error"):
                lines.append("第%d轮 结果：%s" % (i, str(extra["error"])[:120]))
        elif scope == "doc":
            lines.append("第%d轮 指令：%s" % (i, q))
            if extra.get("filename"):
                lines.append("第%d轮 文件：%s（%s → %s）" % (
                    i, extra["filename"], extra.get("source_format", ""), extra.get("target_format", "")))
        else:
            lines.append("第%d轮 用户：%s" % (i, q))
            if a:
                lines.append("第%d轮 助手：%s" % (i, a[:300]))
    text = "\n".join(lines)
    return text[-max_chars:]


def build_condense_prompt(question: str, turns: List[dict], scope: str) -> str:
    """构造"把追问改写成独立问句"的提示词；只允许改写问句，不允许补充事实。"""
    context = format_turns_for_rewrite(turns, scope)
    return (
        "你是问句改写器。请把用户的最新提问改写成一句不依赖上文的独立问句，"
        "补全其中的代词、省略和指代对象。\n"
        "硬性要求：\n"
        "1. 只输出改写后的问句，不要解释、不要引号、不要前缀；\n"
        "2. 不得添加上下文里没有出现的事实、数字或结论；\n"
        "3. 若无法确定指代对象，原样输出用户最新提问。\n\n"
        "【最近对话】\n%s\n\n【最新提问】\n%s\n\n【改写结果】" % (context, question)
    )


def sanitize_rewrite(raw: str, original: str) -> str:
    """清洗改写结果：去引号/换行/前缀，异常或过短则回退原问题。"""
    q = (raw or "").strip()
    if not q:
        return original
    q = q.splitlines()[0].strip().strip("“”\"'` ")
    for prefix in ("改写结果：", "改写结果:", "独立问句：", "独立问句:", "问句："):
        if q.startswith(prefix):
            q = q[len(prefix):].strip()
    if len(q) < 2 or len(q) > 500:
        return original
    return q


# ----------------------------------------------------------------------
# 元问题（问"我上一句问了什么"）识别与直接回答
# ----------------------------------------------------------------------
_META_PATTERNS = [
    ("first", re.compile(r"(我|你).{0,4}(第一个|首个|最开始).{0,6}(问题|问)")),
    ("count", re.compile(r"(我|你).{0,6}(问了|问过|提了).{0,4}(几个|多少|几次)")),
    ("list", re.compile(r"(我|你).{0,6}(问过|提过|问了).{0,4}(哪些|什么)")),
    ("last", re.compile(r"(我|你).{0,6}(上(一个|一条)|刚才|刚刚|上一次).{0,8}(问题|问|说)")),
    ("restate", re.compile(r"(复述|重复).{0,10}(我|你).{0,8}(问题|原话|话)")),
    ("last", re.compile(r"(刚才|上面|前面).{0,4}(我|你).{0,4}(问|说)")),
]


def meta_kind(question: str) -> Optional[str]:
    """识别"问上一轮"的元问题，返回 first/last/count/list/restate；否则 None。"""
    q = (question or "").strip()
    if not q or len(q) > 80:
        return None
    for kind, pattern in _META_PATTERNS:
        if pattern.search(q):
            return kind
    return None


def is_meta_question(question: str) -> bool:
    return meta_kind(question) is not None


def meta_answer(question: str, turns: List[dict]) -> Tuple[str, bool]:
    """基于会话记忆回答元问题，返回 (回答文本, 是否成功)。

    明确标注"来自对话记忆"，避免与文档依据混淆。
    """
    kind = meta_kind(question)
    if not kind:
        return "", False
    questions = [t["question"] for t in turns if t.get("question")]
    if not questions:
        return "（来自对话记忆）这是本会话的第一个问题，之前还没有提问记录。", True

    if kind == "count":
        return ("（来自对话记忆）本会话你一共问了 %d 个问题。最近一个是：「%s」"
                % (len(questions), questions[-1])), True
    if kind == "list":
        preview = "；".join("「%s」" % q for q in questions[-5:])
        more = "" if len(questions) <= 5 else "（仅列最近 5 个）"
        return "（来自对话记忆）你问过：%s%s" % (preview, more), True
    if kind == "first":
        return "（来自对话记忆）你在本会话的第一个问题是：「%s」" % questions[0], True
    # last / restate
    return "（来自对话记忆）你上一个问题是：「%s」" % questions[-1], True


def memory_preamble(turns: List[dict]) -> str:
    """给普通问答用的极简记忆提示（只帮助指代，不注入事实）。"""
    if not turns:
        return ""
    last = turns[-1]
    return "上一轮用户问的是：「%s」" % (last.get("question") or "")

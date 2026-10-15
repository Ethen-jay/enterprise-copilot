# -*- coding: utf-8 -*-
"""
llm_client.py —— Text-to-SQL 大模型客户端（阶段 C 建，阶段 D 接入 config）

用 openai 官方库的“兼容模式”对接任意 OpenAI 协议的推理端，云端/本地只靠
.env 切换，代码零改动（配置由 app/config.py 统一收口）：

  LLM_BASE_URL  接口地址，例如
                  - DeepSeek   ：https://api.deepseek.com/v1
                  - 硅基流动   ：https://api.siliconflow.cn/v1
                  - 本地 vLLM  ：http://localhost:28080/v1（Qwen2.5-7B，阶段二预留）
  LLM_API_KEY   密钥（本地 vLLLM 可填 EMPTY）
  LLM_MODEL     模型名，例如 deepseek-chat / Qwen2.5-7B-Instruct

命令行自测（在项目根目录运行，需先配好 .env）：
    python -m app.llm_client "当前在制工单有多少"
"""

import re
import sys
from dataclasses import dataclass
from typing import Optional

from common.llm_openai import create_openai_client
from .config import get_llm_settings
from .anchor import get_anchor_date, get_snapshot_date

# ----------------------------------------------------------------------
# 系统提示词：把“怎么当一个 Text-to-SQL 生成器”一次性说清楚
# 业务口径（RTY/OTD/账龄…）不写在这里，全部来自 semantic/metrics.md，
# 避免规则散落两处、以后改口径时漏改。
# ----------------------------------------------------------------------
SYSTEM_PROMPT = """你是一名 IC 载板工厂 ERP+MES 系统的 SQLite 取数工程师。
用户用中文大白话提问，你只能根据下面给出的 Schema 上下文，生成 **1 条** SQLite 只读查询。

硬性要求：
1. 只能使用上下文中列出的表和真实列名，严禁臆造表名/字段；表间关联按“表间关联速查”。
2. 只输出一条 SELECT 或 WITH...SELECT 语句。禁止 INSERT/UPDATE/DELETE/DDL/PRAGMA/ATTACH，禁止多语句。
3. 严格遵守上下文中的指标口径与四条强制规则（客户按 customer_id 聚合、产出过滤 is_rework_pass=0、
   RTY 只统计 lot_status='已完工'、lot_id 双格式只等值关联）。
4. 相对时间词（今天/当前/本月/上月(上个月)/最近一周/最近一个月/今年）一律相对业务基准日 ANCHOR='{anchor_date}'
   （运行时按库内 MAX(业务日期) 探测，即数据最新日），
   具体边界严格按 metrics.md 第 0 节（如“上个月”=ANCHOR 上一完整自然月，用半开区间），禁止 date('now')；
   生产/过站时间列用 out_time，销售订单时间列用 order_date。
5. 账龄类问题，快照日固定为 SNAPSHOT_DATE='{snapshot_date}'，按 metrics.md 的双向账龄模板写。
6. 比例/良率用 ROUND(...,4)；金额 ROUND(...,2)。列名起中文或英文别名均可，便于业务人员阅读。
7. 输出格式：先给一句不超过30字的口径说明，然后给唯一一个 ```sql 代码块。不要输出查询结果，不要编造数字。
8. 如果是数据本身回答不了（需要的表/字段在 Schema 中不存在），不要硬写 SQL，直接以“无法回答：”开头说明原因。
9. 如果问题缺少必要限定（时间范围、统计对象或分组维度不明确），且口径文件没有合理默认值，不要编造，
   第一行输出 `CLARIFY:` 并紧跟一个具体的澄清问题（只问最关键的一项，一句话），不要给 SQL。
   凡是口径已规定默认值的（本月/今年相对基准日、订单默认只算正式单等），一律直接按默认作答，
   严禁对信息完整的标准问题发起澄清。
"""

# 澄清态：模型第一行输出 CLARIFY: 问题
CLARIFY_RE = re.compile(r"^\s*CLARIFY\s*[:：]\s*(.+)", re.I | re.S)

# ```sql ... ``` 或 ``` ... ```
FENCED_BLOCK_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.I | re.S)
# 退化匹配：模型没加代码块时，从第一个 WITH/SELECT 截到末尾
BARE_SQL_RE = re.compile(r"((?:WITH|SELECT)\b.*)", re.I | re.S)


class LLMError(Exception):
    """调用模型失败（未配置密钥、网络异常、返回里提不出 SQL 等）。"""


@dataclass
class LLMResult:
    sql: str                 # 提取出的 SQL（尚未过 sql_guard）
    raw_text: str            # 模型完整回复（日志/排查用）
    model: str               # 实际使用的模型
    explanation: str = ""    # 代码块外的口径说明（网页上展示给业务人员）
    refusal: Optional[str] = None  # 模型明确说“无法回答”时的原因
    clarify: Optional[str] = None  # 模型要求用户补充信息时的澄清问题


def extract_sql(raw_text: str) -> Optional[str]:
    """
    从模型回复里提取 SQL：
      1. 优先取 ```sql 围栏块（取最后一个，防止模型先说“修改后”再给最终版）；
      2. 没有围栏块时，退化抓第一个 WITH/SELECT 到结尾；
      3. 都没有返回 None。
    """
    blocks = [b.strip() for b in FENCED_BLOCK_RE.findall(raw_text) if b.strip()]
    if blocks:
        sql = blocks[-1]
    else:
        m = BARE_SQL_RE.search(raw_text)
        sql = m.group(1).strip() if m else ""
    if not sql:
        return None
    return sql.strip().rstrip(";").strip()


def _extract_explanation(raw_text: str) -> str:
    """取代码块外的文字作为口径说明；没有就给空串。"""
    text = FENCED_BLOCK_RE.sub("", raw_text).strip()
    # 去掉多余空行，避免网页上一大片空白
    return re.sub(r"\n{2,}", "\n", text).strip()


def _call_llm(settings, system: str, user_content: str) -> str:
    """发一次对话请求，返回模型原始回复文本（异常统一收口为 LLMError）。"""
    client = create_openai_client()
    try:
        resp = client.chat.completions.create(
            model=settings.model,
            temperature=settings.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as e:
        # openai 库的各类异常统一收口，避免把堆栈直接抛给网页
        raise LLMError("调用模型失败（%s）：%s" % (settings.model, e)) from e
    return resp.choices[0].message.content or ""


def _parse_result(raw_text: str, model: str) -> LLMResult:
    """从模型回复解析出 LLMResult（无法回答 / 提取 SQL / 提取失败）。"""
    # 模型按要求第 8 条声明无法回答
    if raw_text.strip().startswith("无法回答"):
        return LLMResult(sql="", raw_text=raw_text, model=model,
                         explanation=raw_text.strip(), refusal=raw_text.strip())

    # 模型按第 9 条要求澄清（信息不足、可靠追问解决）
    m_clar = CLARIFY_RE.match(raw_text.strip())
    if m_clar:
        # 只取第一行作为澄清问题，去掉可能被模型带上的 SQL/多余空行
        question = m_clar.group(1).strip().splitlines()[0].strip()
        return LLMResult(sql="", raw_text=raw_text, model=model,
                         explanation=question, clarify=question)

    sql = extract_sql(raw_text)
    if not sql:
        raise LLMError("模型返回中未提取到 SQL，原文：%s" % raw_text[:200])

    return LLMResult(
        sql=sql,
        raw_text=raw_text,
        model=model,
        explanation=_extract_explanation(raw_text),
    )


def generate_sql(question: str, schema_context: str,
                 anchor_date: Optional[str] = None,
                 snapshot_date: Optional[str] = None,
                 clarification: str = "") -> LLMResult:
    """
    调一次大模型生成 SQL。

    参数：
      question       用户原始问题
      schema_context schema_linker.build_context() 产出的上下文文本
      anchor_date    业务基准日；None = 运行时查库动态探测（anchor.py）
      snapshot_date  账龄快照日；None = 取 .env 显式配置，未配置则跟随动态锚点
      clarification  上一轮模型发起 CLARIFY 后，用户补充的信息（首轮为空）
    """
    settings = get_llm_settings()
    anchor = anchor_date or get_anchor_date().date
    snapshot = snapshot_date or get_snapshot_date()
    system = SYSTEM_PROMPT.format(anchor_date=anchor, snapshot_date=snapshot)
    user_content = "【Schema 上下文】\n%s\n\n【用户问题】\n%s" % (schema_context, question)
    if clarification:
        user_content += (
            "\n\n【用户对上一轮澄清问题的补充】\n%s\n"
            "请结合原问题与该补充信息直接生成最终 SQL；若补充后仍无法确定，"
            "可再次输出一行 CLARIFY: 追问。" % clarification
        )
    raw_text = _call_llm(settings, system, user_content)
    return _parse_result(raw_text, settings.model)


def regenerate_sql(question: str, schema_context: str, previous_sql: str,
                   exec_error: str,
                   anchor_date: Optional[str] = None,
                   snapshot_date: Optional[str] = None) -> LLMResult:
    """
    SQL 执行失败后的自动修复：把原 SQL + 数据库报错回喂模型，重新生成 1 条。

    参数：
      question       用户原始问题
      schema_context schema 上下文（修复仍只允许用真实表字段）
      previous_sql   执行失败的 SQL
      exec_error     数据库报错原文（如 no such column: xxx）
      anchor_date    业务基准日；None = 运行时查库动态探测（anchor.py）
      snapshot_date  账龄快照日；None = 取 .env 显式配置，未配置则跟随动态锚点
    """
    settings = get_llm_settings()
    anchor = anchor_date or get_anchor_date().date
    snapshot = snapshot_date or get_snapshot_date()
    system = SYSTEM_PROMPT.format(anchor_date=anchor, snapshot_date=snapshot)
    user_content = (
        "【Schema 上下文】\n%s\n\n【用户问题】\n%s\n\n"
        "【上一条 SQL（执行失败）】\n%s\n\n"
        "【数据库报错】\n%s\n\n"
        "上一条 SQL 执行失败。请对照报错修正（典型原因：列名/表名写错、聚合列未 "
        "GROUP BY、JOIN 条件不对、类型比较错误），重新给出 **1 条** 可执行的 "
        "SQLite 只读查询。输出格式不变：一句不超过30字的口径说明 + 唯一一个 ```sql 代码块。\n"
        "若现有表确实回答不了该问题，直接以“无法回答：”开头说明原因。"
    ) % (schema_context, question, previous_sql, exec_error)
    raw_text = _call_llm(settings, system, user_content)
    return _parse_result(raw_text, settings.model)


# ----------------------------------------------------------------------
# 命令行自测：schema_linker -> llm_client（不执行 SQL）
# ----------------------------------------------------------------------
def main(argv: list) -> int:
    if len(argv) < 2:
        print('用法：python -m app.llm_client "你的问题"')
        return 2

    from .schema_linker import build_context, log_link

    question = argv[1]

    try:
        settings = get_llm_settings()
        print("模型配置：%s" % settings.masked())
        link = build_context(question)
        log_link(link)
        print("-" * 72)
        result = generate_sql(question, link.context_text)
    except (LLMError, RuntimeError) as e:
        print("【错误】%s" % e)
        return 1

    if result.refusal:
        print("模型表示：%s" % result.refusal)
        return 0

    print("模型完整回复：\n%s" % result.raw_text)
    print("-" * 72)
    print("提取出的 SQL：\n%s" % result.sql)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

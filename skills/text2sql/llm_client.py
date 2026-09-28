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
from .config import SNAPSHOT_DATE, get_llm_settings

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
4. 时间一律相对业务基准日 ANCHOR='2026-09-24'，禁止 date('now')；生产时间列用 out_time。
5. 账龄类问题，快照日固定为 SNAPSHOT_DATE='{snapshot_date}'，按 metrics.md 的双向账龄模板写。
6. 比例/良率用 ROUND(...,4)；金额 ROUND(...,2)。列名起中文或英文别名均可，便于业务人员阅读。
7. 输出格式：先给一句不超过30字的口径说明，然后给唯一一个 ```sql 代码块。不要输出查询结果，不要编造数字。
8. 如果上下文不足以回答（例如需要不存在的字段），不要硬写 SQL，直接以“无法回答：”开头说明原因。
"""

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


def generate_sql(question: str, schema_context: str,
                 snapshot_date: str = SNAPSHOT_DATE) -> LLMResult:
    """
    调一次大模型生成 SQL。

    参数：
      question       用户原始问题
      schema_context schema_linker.build_context() 产出的上下文文本
      snapshot_date  账龄快照日（默认取 config.SNAPSHOT_DATE）
    """
    settings = get_llm_settings()
    client = create_openai_client()

    system = SYSTEM_PROMPT.format(snapshot_date=snapshot_date)
    user_content = "【Schema 上下文】\n%s\n\n【用户问题】\n%s" % (schema_context, question)

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

    raw_text = resp.choices[0].message.content or ""

    # 模型按要求第 8 条声明无法回答
    if raw_text.strip().startswith("无法回答"):
        return LLMResult(sql="", raw_text=raw_text, model=settings.model,
                         explanation=raw_text.strip(), refusal=raw_text.strip())

    sql = extract_sql(raw_text)
    if not sql:
        raise LLMError("模型返回中未提取到 SQL，原文：%s" % raw_text[:200])

    return LLMResult(
        sql=sql,
        raw_text=raw_text,
        model=settings.model,
        explanation=_extract_explanation(raw_text),
    )


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

"""BM25 检索器：纯 Python 实现，无需 rank_bm25 / jieba 依赖。

用于 RAG 混合检索：向量检索召回语义相关片段，BM25 召回关键词精确匹配片段，
两者取并集后再交重排器，提升错别字/简称/专业术语的召回率。

分词策略：中文用字符级 unigram（无需 jieba），英文用空格分词，
对短查询的关键词匹配足够有效。
"""

import math
import re
from typing import List, Optional


# 中文标点与停用字符，分词时跳过
_PUNCT_RE = re.compile(r'[\s\u3000，。！？、；：""''（）《》【】…—\-\.,!?;:"\'()\[\]{}]')


def _tokenize(text: str) -> List[str]:
    """简单分词：中文按字符 unigram，英文按空格。

    不依赖 jieba，对 BM25 关键词匹配足够（错别字/简称通常是单字或两字组合）。
    """
    tokens: List[str] = []
    # 按中英文混合切分
    parts = re.split(r'([a-zA-Z]+)', text)
    for part in parts:
        if not part:
            continue
        if re.match(r'^[a-zA-Z]+$', part):
            # 英文单词整体作为一个 token（小写）
            tokens.append(part.lower())
        else:
            # 中文：去掉标点后逐字切
            for ch in part:
                if _PUNCT_RE.match(ch):
                    continue
                tokens.append(ch)
    return tokens


class BM25Retriever:
    """BM25 Okapi 检索器。

    用法：
        retriever = BM25Retriever(docs)        # docs: List[str]
        scores = retriever.get_scores(query)    # 返回每个文档的 BM25 分数
        top_docs = retriever.search(query, k=10) # 返回 top-k (index, score)
    """

    def __init__(self, documents: List[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = documents
        self.n_docs = len(documents)

        # 预计算每个文档的 token 列表与长度
        self.doc_tokens: List[List[str]] = [_tokenize(d) for d in documents]
        self.doc_lens = [len(t) for t in self.doc_tokens]
        self.avgdl = sum(self.doc_lens) / self.n_docs if self.n_docs > 0 else 0

        # 词频统计：df[token] = 包含该 token 的文档数
        self.df: dict = {}
        for tokens in self.doc_tokens:
            seen = set(tokens)
            for token in seen:
                self.df[token] = self.df.get(token, 0) + 1

        # 预计算 IDF
        self.idf: dict = {}
        for token, freq in self.df.items():
            # BM25 IDF：ln((N - n + 0.5) / (n + 0.5) + 1)
            self.idf[token] = math.log(
                (self.n_docs - freq + 0.5) / (freq + 0.5) + 1.0
            )

    def _score_single(self, query_tokens: List[str], doc_idx: int) -> float:
        """计算单个文档对查询的 BM25 分数。"""
        score = 0.0
        doc_tokens = self.doc_tokens[doc_idx]
        doc_len = self.doc_lens[doc_idx]

        # 统计查询词在该文档中的词频
        tf: dict = {}
        for t in doc_tokens:
            tf[t] = tf.get(t, 0) + 1

        for q_token in query_tokens:
            if q_token not in self.idf:
                continue
            f = tf.get(q_token, 0)
            if f == 0:
                continue
            idf = self.idf[q_token]
            denom = f + self.k1 * (1 - self.b + self.b * doc_len / (self.avgdl or 1))
            score += idf * (f * (self.k1 + 1)) / denom
        return score

    def get_scores(self, query: str) -> List[float]:
        """返回所有文档对 query 的 BM25 分数列表（与 docs 等长）。"""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return [0.0] * self.n_docs
        return [self._score_single(query_tokens, i) for i in range(self.n_docs)]

    def search(self, query: str, k: int = 10) -> List[tuple]:
        """返回 BM25 分数最高的 top-k 文档 [(index, score), ...]。"""
        scores = self.get_scores(query)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        # 过滤分数为 0 的（完全不匹配）
        ranked = [(i, s) for i, s in ranked if s > 0]
        return ranked[:k]

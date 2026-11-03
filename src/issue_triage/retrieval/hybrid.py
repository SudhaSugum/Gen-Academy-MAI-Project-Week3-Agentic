"""Small-corpus BM25 ranking and rank fusion for the synthetic concept build."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from langchain_core.documents import Document


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9._-]*", text.casefold())


def _matches(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    for field, condition in filters.items():
        if not isinstance(condition, dict):
            continue
        value = str(metadata.get(field, ""))
        if "$eq" in condition and value != str(condition["$eq"]):
            return False
        if "$ne" in condition and value == str(condition["$ne"]):
            return False
        if "$gte" in condition and value < str(condition["$gte"]):
            return False
        if "$gt" in condition and value <= str(condition["$gt"]):
            return False
        if "$lt" in condition and value >= str(condition["$lt"]):
            return False
        if "$lte" in condition and value > str(condition["$lte"]):
            return False
    return True


def keyword_search(documents: list[Document], query: str, k: int, filters: dict[str, Any]) -> list[tuple[Document, float, int]]:
    """Return BM25 hits after enforcing the same tenant/audit metadata filters."""
    query_terms = set(_tokens(query))
    if not query_terms:
        return []
    corpus = [doc for doc in documents if _matches(doc.metadata, filters)]
    tokenized = [_tokens(doc.page_content) for doc in corpus]
    if not corpus:
        return []
    avg_length = max(1.0, sum(map(len, tokenized)) / len(tokenized))
    doc_frequency = Counter(term for terms in tokenized for term in set(terms))
    total = len(corpus)
    scored: list[tuple[Document, float, int]] = []
    for doc, terms in zip(corpus, tokenized):
        frequencies = Counter(terms)
        score = 0.0
        matched = 0
        for term in query_terms:
            tf = frequencies[term]
            if tf:
                matched += 1
                idf = math.log(1 + (total - doc_frequency[term] + 0.5) / (doc_frequency[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(terms) / avg_length))
        if score:
            scored.append((doc, score, matched))
    scored.sort(key=lambda row: row[1], reverse=True)
    return scored[:k]


def fuse_rankings(semantic: list[dict[str, Any]], keyword: list[tuple[Document, float, int]], query: str, k: int, rrf_constant: int = 60) -> list[dict[str, Any]]:
    """Merge semantic and BM25 hits by Reciprocal Rank Fusion (scores are rank signals)."""
    merged: dict[str, dict[str, Any]] = {}
    for rank, hit in enumerate(semantic, 1):
        key = hit["chunk_id"]
        merged[key] = {**hit, "semantic_rank": rank, "keyword_rank": None, "keyword_score": 0.0,
                       "keyword_overlap": 0.0, "hybrid_score": 1 / (rrf_constant + rank)}
    query_term_count = max(1, len(set(_tokens(query))))
    for rank, (doc, score, matched_terms) in enumerate(keyword, 1):
        key = doc.metadata.get("chunk_id", "unknown")
        entry = merged.get(key)
        if entry is None:
            meta = doc.metadata
            source_type = {"past_issue": "past_issues", "control": "controls", "policy": "policies"}.get(meta.get("source_type", "unknown"), meta.get("source_type", "unknown"))
            entry = {"source_type": source_type, "source_id": meta.get("doc_id", "unknown"),
                     "doc_id": meta.get("doc_id", "unknown"), "client_id": meta.get("client_id", ""),
                     "chunk_id": key, "title": meta.get("title", "Untitled source"), "section": meta.get("section", ""),
                     "text": doc.page_content, "audit_id": meta.get("audit_id", ""),
                     "score": 0.0, "semantic_score": 0.0, "outcome": meta.get("outcome", ""),
                     "severity": meta.get("final_severity", meta.get("severity", "")), "root_cause": meta.get("root_cause", ""),
                     "control_id": meta.get("control_id", ""), "status": meta.get("status", ""),
                     "issue_date": meta.get("issue_date", ""), "closure_date": meta.get("closure_date", ""),
                     "rau_id": meta.get("rau_id", ""), "attribute": meta.get("attribute", ""),
                     "control_version": meta.get("control_version", ""), "source": meta.get("source", ""),
                     "semantic_rank": None, "keyword_rank": rank, "keyword_score": score,
                     "keyword_overlap": matched_terms / query_term_count, "hybrid_score": 0.0}
            merged[key] = entry
        entry["keyword_rank"] = rank
        entry["keyword_score"] = score
        entry["keyword_overlap"] = matched_terms / query_term_count
        entry["hybrid_score"] += 1 / (rrf_constant + rank)
    results = sorted(merged.values(), key=lambda item: item["hybrid_score"], reverse=True)[:k]
    return results

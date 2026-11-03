"""Scoped memory access. Raw issues and evidence are never submitted to Mem0."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ApprovedMemory:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None
        if api_key:
            try:
                from mem0 import MemoryClient
                self._client = MemoryClient(api_key=api_key)
            except ImportError as exc:
                logger.warning("Mem0 SDK unavailable; memory remains disabled: %s", exc)

    @staticmethod
    def scoped_user(auditor_id: str, client_id: str) -> str:
        return f"auditor:{auditor_id}:client:{client_id}"

    def recall(self, query: str, auditor_id: str, client_id: str) -> list[dict[str, Any]]:
        if not self._client:
            return []
        try:
            result = self._client.search(query, filters={"user_id": self.scoped_user(auditor_id, client_id)})
            return result.get("results", [])[:5] if isinstance(result, dict) else []
        except Exception:
            logger.exception("Mem0 recall failed; continuing without memory")
            return []

    def remember_approved_pattern(self, note: str, auditor_id: str, client_id: str, issue_id: str, approver: str) -> bool:
        clean = " ".join(note.split())[:1200]
        if not self._client or not clean:
            return False
        # Only this explicitly supplied reviewer learning is sent; never raw issue text, evidence, or a draft.
        try:
            self._client.add(
                [{"role": "user", "content": f"Approved audit triage learning: {clean}"}],
                user_id=self.scoped_user(auditor_id, client_id),
                metadata={"source_issue_id": issue_id, "approved_by": approver},
            )
            return True
        except Exception:
            logger.exception("Mem0 write-back failed")
            return False

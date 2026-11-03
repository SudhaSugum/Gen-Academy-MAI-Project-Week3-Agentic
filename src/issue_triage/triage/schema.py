"""Structured suggestion contract. Each user-facing field owns its citations."""

from typing import Literal

from pydantic import BaseModel, Field


class Citation(BaseModel):
    source_id: str
    chunk_id: str
    quote: str = Field(min_length=8, max_length=500)


class Claim(BaseModel):
    value: str
    rationale: str
    citations: list[Citation]


class RemediationStep(BaseModel):
    action: Claim
    owner_role: Claim
    due_date_suggestion: Claim


class SimilarIssue(BaseModel):
    chunk_id: str
    similarity: float = Field(ge=0, le=1)
    outcome: str


class TriageSuggestion(BaseModel):
    severity: Claim
    root_cause: Claim
    control_mapping: Claim
    remediation_steps: list[RemediationStep]
    duplicate_warning: Claim
    similar_issues: list[SimilarIssue]
    confidence: Claim
    retrieval_state: Literal["grounded", "insufficient_evidence"]


def insufficient_suggestion(reason: str) -> TriageSuggestion:
    claim = Claim(value="Insufficient evidence", rationale=reason, citations=[])
    return TriageSuggestion(
        severity=claim, root_cause=claim, control_mapping=claim,
        remediation_steps=[], duplicate_warning=claim, similar_issues=[],
        confidence=Claim(value="Low", rationale=reason, citations=[]),
        retrieval_state="insufficient_evidence",
    )

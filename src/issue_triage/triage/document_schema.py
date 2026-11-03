"""Typed request/result contract for source-bound summary document drafts."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


Template = Literal["cluster_summary", "scoping_pack", "lead_review", "single_issue_review"]


class SummaryDocumentRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    flow_id: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=128)
    audit_id: str = Field(max_length=128)
    rau_id: str | None = Field(default=None, max_length=128)
    issue_ids: list[str] = Field(default_factory=list, max_length=50)
    cluster_id: str | None = Field(default=None, max_length=128)
    template: Template
    title: str = Field(default="", max_length=120)
    include_remediation_drafts: bool = False
    requested_by_role: Literal["auditor", "lead_auditor"]
    requested_by_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_scope(self):
        ids = list(dict.fromkeys(self.issue_ids))
        if self.cluster_id and ids:
            raise ValueError("Supply issue_ids or cluster_id, not both.")
        if not self.cluster_id and not ids:
            raise ValueError("At least one issue ID or a cluster ID is required.")
        if len(ids) > 50:
            raise ValueError("A document can include at most 50 issues; split by cluster.")
        if self.template == "cluster_summary" and not self.cluster_id:
            raise ValueError("cluster_summary requires a cluster_id.")
        if self.template == "single_issue_review" and len(ids) != 1:
            raise ValueError("single_issue_review requires exactly one issue ID.")
        if self.template == "scoping_pack" and not self.rau_id:
            raise ValueError("scoping_pack requires a RAU ID.")
        if self.title and (re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", self.title) or re.search(r"(?i)(api[_ -]?key|password|secret|token)\s*[:=]", self.title)):
            raise ValueError("The document title cannot contain an email address or secret-like value.")
        self.issue_ids = ids
        return self


class SummaryCitation(BaseModel):
    issue_id: str | None = None
    doc_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    quote: str = Field(min_length=8, max_length=500)
    audit_id: str = ""
    source_type: str = "past_issue"
    source_system: str = "Pinecone synthetic corpus"
    fetched_at: str = ""


class SummaryBullet(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    citations: list[SummaryCitation] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_sensitive_text(self):
        if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", self.text) or re.search(r"(?i)(api[_ -]?key|password|secret|token)\s*[:=]", self.text):
            raise ValueError("Document bullets cannot contain email addresses or secret-like values.")
        return self


class SummarySection(BaseModel):
    heading: str = Field(min_length=1, max_length=120)
    bullets: list[SummaryBullet] = Field(default_factory=list)


class SummaryCounts(BaseModel):
    total_issues: int = Field(ge=0)
    by_severity: dict[str, int]
    by_status: dict[str, int]
    repeat_clusters: int = Field(ge=0)
    open_overdue: int = Field(ge=0)


class SummaryDocument(BaseModel):
    document_id: str
    flow_id: str
    version: int = Field(ge=1)
    status: Literal["draft", "approved", "rejected", "superseded"]
    created_at: str
    created_by_run_id: str
    location: str
    title: str
    draft_header: str
    client_id: str
    audit_id: str
    audit_ids: list[str]
    rau_id: str | None
    template: Template
    sections: list[SummarySection]
    counts: SummaryCounts
    source_issue_ids: list[str]
    coverage: str
    unresolved_gaps: list[str]
    warnings: list[str]
    suggested_next_actions: list[str]

    @model_validator(mode="after")
    def reject_sensitive_title(self):
        if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", self.title) or re.search(r"(?i)(api[_ -]?key|password|secret|token)\s*[:=]", self.title):
            raise ValueError("Document title cannot contain an email address or secret-like value.")
        return self


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def validate_summary_document(document: dict[str, Any], sources: dict[str, dict[str, Any]]) -> list[str]:
    """Check citation membership, exact quotes, and deterministic counts."""
    errors: list[str] = []
    included = set(document.get("source_issue_ids", []))
    for section in document.get("sections", []):
        for index, bullet in enumerate(section.get("bullets", []), 1):
            label = f"{section.get('heading', 'section')} bullet {index}"
            citations = bullet.get("citations") or []
            if not citations:
                errors.append(f"{label}: missing citation")
                continue
            for citation in citations:
                issue_id, chunk_id = citation.get("issue_id"), citation.get("chunk_id")
                source = sources.get(chunk_id)
                if not source or chunk_id != source.get("chunk_id"):
                    errors.append(f"{label}: chunk was not retrieved in this run")
                    continue
                if citation.get("doc_id") != source.get("source_id"):
                    errors.append(f"{label}: source document ID mismatch")
                if issue_id and issue_id not in included:
                    errors.append(f"{label}: issue is outside the document input set")
                if _normalise(citation.get("quote", "")) not in _normalise(source.get("text", "")):
                    errors.append(f"{label}: quote is not an exact retrieved source excerpt")
    counts = document.get("counts", {})
    source_by_id = {
        str(source.get("source_id")): source for source in sources.values()
        if source.get("source_id") in included
    }
    expected_severity: dict[str, int] = {}
    expected_status: dict[str, int] = {}
    repeat_groups: dict[tuple[str, str], int] = {}
    expected_open_overdue = 0
    for issue_id in included:
        source = source_by_id.get(issue_id)
        if not source:
            errors.append(f"Source issue {issue_id} is not available in the current run.")
            continue
        severity = str(source.get("severity") or "Unknown")
        status = str(source.get("status") or "Unknown").lower()
        expected_severity[severity] = expected_severity.get(severity, 0) + 1
        expected_status[status] = expected_status.get(status, 0) + 1
        repeat_key = (str(source.get("control_id", "")).strip(), str(source.get("root_cause", "")).strip())
        if any(repeat_key):
            repeat_groups[repeat_key] = repeat_groups.get(repeat_key, 0) + 1
        expected_open_overdue += int(status in {"open", "overdue"})
    expected_repeat_clusters = sum(count > 1 for count in repeat_groups.values())
    if counts.get("total_issues") != len(included):
        errors.append("total_issues does not match the source issue list")
    if counts.get("by_severity") != expected_severity:
        errors.append("severity counts do not match source issue metadata")
    if counts.get("by_status") != expected_status:
        errors.append("status counts do not match source issue metadata")
    if counts.get("repeat_clusters") != expected_repeat_clusters:
        errors.append("repeat-cluster count does not match source issue metadata")
    if counts.get("open_overdue") != expected_open_overdue:
        errors.append("open/overdue count does not match source issue metadata")
    return errors


def build_summary_document(
    *, document_id: str, request: SummaryDocumentRequest, pack: dict[str, Any],
    sources: dict[str, dict[str, Any]], selected_ids: list[str], cluster_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    selected_set = set(selected_ids)
    selected = [item for item in sources.values() if item.get("source_id") in selected_set]
    selected.sort(key=lambda item: item.get("source_id", ""))
    severities: dict[str, int] = {}
    statuses: dict[str, int] = {}
    repeat_keys: dict[tuple[str, str], int] = {}
    audit_ids: set[str] = set()
    inventory: list[SummaryBullet] = []
    for source in selected:
        source_id = source["source_id"]
        severity = str(source.get("severity") or "Unknown")
        status = str(source.get("status") or "Unknown").lower()
        severities[severity] = severities.get(severity, 0) + 1
        statuses[status] = statuses.get(status, 0) + 1
        repeat_key = (str(source.get("control_id", "")).strip(), str(source.get("root_cause", "")).strip())
        if any(repeat_key):
            repeat_keys[repeat_key] = repeat_keys.get(repeat_key, 0) + 1
        if source.get("audit_id"):
            audit_ids.add(str(source["audit_id"]))
        text = str(source.get("text", ""))
        quote = text[:500]
        citation = SummaryCitation(
            issue_id=source_id, doc_id=source_id, chunk_id=source["chunk_id"], quote=quote,
            audit_id=str(source.get("audit_id", "")), source_type="past_issue",
        )
        factual = (
            f"{source_id}: {source.get('title', 'Untitled issue')}; severity {severity}; "
            f"status {status}; root cause {source.get('root_cause') or 'not recorded'}; "
            f"control {source.get('control_id') or 'not recorded'}; audit {source.get('audit_id') or 'not recorded'}."
        )
        inventory.append(SummaryBullet(text=factual, citations=[citation]))
        if request.include_remediation_drafts:
            match = re.search(r"(?im)^Remediation:\s*(.+)$", text)
            if match:
                inventory.append(SummaryBullet(
                    text=f"DRAFT remediation precedent for {source_id}: {match.group(1).strip()}",
                    citations=[citation],
                ))
    action_bullets: list[SummaryBullet] = []
    actions: list[str] = []
    warnings: list[str] = []
    unresolved: list[str] = []
    for row in cluster_rows:
        member_ids = [str(item) for item in row.get("issue_ids", []) if str(item) in set(selected_ids)]
        if not member_ids:
            if not row.get("issue_ids"):
                unresolved.append(f"{row.get('control_id', 'Unknown control')}: no past issue was retrieved for this cluster")
            continue
        citations = []
        for issue_id in member_ids:
            source = sources.get(issue_id)
            if source:
                citations.append(SummaryCitation(
                    issue_id=issue_id, doc_id=source["source_id"], chunk_id=source["chunk_id"],
                    quote=str(source.get("text", ""))[:500], audit_id=str(source.get("audit_id", "")),
                ))
        if not citations:
            continue
        action = f"{row.get('control_id', '')} · {row.get('attribute', '')}: {row.get('action', '')}. {row.get('procedure', '')}"
        actions.append(action)
        action_bullets.append(SummaryBullet(text=action, citations=citations))
    repeat_clusters = sum(1 for count in repeat_keys.values() if count > 1)
    open_overdue = sum(1 for source in selected if str(source.get("status", "")).lower() in {"open", "overdue"})
    title = request.title or {
        "cluster_summary": f"Cluster summary · {request.rau_id or ''}",
        "scoping_pack": f"RAU scoping pack · {request.rau_id or ''}",
        "lead_review": f"Lead review pack · {request.rau_id or ''}",
        "single_issue_review": "Single issue review",
    }[request.template]
    title = title[:120]
    data = SummaryDocument(
        document_id=document_id, flow_id=request.flow_id, version=1, status="draft", created_at=now,
        created_by_run_id=request.run_id, location=f"internal://documents/{document_id}",
        title=title, draft_header="DRAFT — NOT FINAL — PENDING APPROVAL",
        client_id=request.client_id, audit_id=request.audit_id,
        audit_ids=sorted(audit_ids), rau_id=request.rau_id, template=request.template,
        sections=[
            SummarySection(heading="Retrieved issue summary", bullets=inventory),
            SummarySection(heading="Proposed scoping actions", bullets=action_bullets),
        ],
        counts=SummaryCounts(
            total_issues=len(selected_ids), by_severity=severities, by_status=statuses,
            repeat_clusters=repeat_clusters, open_overdue=open_overdue,
        ),
        source_issue_ids=list(selected_ids), coverage=f"{len(selected)} of {len(selected_ids)} input issues included",
        unresolved_gaps=unresolved, warnings=warnings, suggested_next_actions=actions,
    )
    result = data.model_dump(mode="json")
    errors = validate_summary_document(result, {item["chunk_id"]: item for item in sources.values()})
    if errors:
        raise ValueError("Summary citation validation failed: " + "; ".join(errors))
    return result

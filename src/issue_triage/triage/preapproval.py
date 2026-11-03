"""Deterministic, Pydantic-typed checks required before auditor approval."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..ingestion.corpus import build_documents
from .schema import TriageSuggestion


GateStatus = Literal["pass", "warning", "fail", "lead_override_required", "lead_overridden"]


class GateCheck(BaseModel):
    code: str
    title: str
    status: GateStatus
    detail: str
    overridable: bool = False
    evidence: list[str] = Field(default_factory=list)


class ApprovalGateResult(BaseModel):
    checks: list[GateCheck]
    ready: bool
    blockers: list[str]


def _claims(suggestion: TriageSuggestion) -> list[tuple[str, Any]]:
    result = [
        ("severity", suggestion.severity),
        ("root_cause", suggestion.root_cause),
        ("control_id", suggestion.control_mapping),
    ]
    for index, step in enumerate(suggestion.remediation_steps, 1):
        result.extend((
            (f"remediation[{index}].action", step.action),
            (f"remediation[{index}].owner_role", step.owner_role),
            (f"remediation[{index}].due_date_suggestion", step.due_date_suggestion),
        ))
    return result


def _all_cited_claims(suggestion: TriageSuggestion) -> list[tuple[str, Any]]:
    return [* _claims(suggestion), ("duplicate_warning", suggestion.duplicate_warning), ("confidence", suggestion.confidence)]


def _all_sources(retrieved: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [source for group in retrieved.values() for source in group]


def _control_id(value: str) -> str:
    match = re.search(r"\b[A-Z]{2,5}-\d{2,4}\b", value.upper())
    return match.group(0) if match else ""


def _check_citations(suggestion: TriageSuggestion, retrieved: dict[str, list[dict[str, Any]]]) -> tuple[bool, list[str]]:
    chunks = {source.get("chunk_id"): source for source in _all_sources(retrieved) if source.get("chunk_id")}
    errors: list[str] = []
    for label, claim in _all_cited_claims(suggestion):
        explicitly_unsupported = claim.value.strip().casefold() == "insufficient evidence"
        if explicitly_unsupported:
            if claim.citations:
                errors.append(f"{label}: insufficient-evidence fields must not carry unsupported citations")
            continue
        if not claim.citations:
            errors.append(f"{label}: no citation")
        for citation in claim.citations:
            source = chunks.get(citation.chunk_id)
            if not source:
                errors.append(f"{label}: chunk {citation.chunk_id} was not retrieved in this run")
                continue
            if source.get("source_id") != citation.source_id or source.get("doc_id", source.get("source_id")) != citation.source_id:
                errors.append(f"{label}: doc_id/source ID mismatch for {citation.chunk_id}")
            if not citation.quote or citation.quote not in str(source.get("text", "")):
                errors.append(f"{label}: quote is not a verbatim excerpt from {citation.chunk_id}")
    return not errors, errors


def _control_check(state: dict[str, Any], suggestion: TriageSuggestion) -> GateCheck:
    control_id = _control_id(suggestion.control_mapping.value)
    client_id = str(state.get("client_id", ""))
    rau_id = str(state.get("rau_id", ""))
    controls = [
        item for item in state.get("retrieved", {}).get("controls", [])
        if item.get("control_id") == control_id and item.get("client_id") == client_id
    ]
    scoped = [item for item in controls if str(item.get("rau_id", "")) == rau_id]
    if not control_id or not scoped:
        return GateCheck(
            code="C03", title="Control validity and version", status="fail", overridable=False,
            detail="The mapped control is missing, retired, or outside this client/RAU in the retrieved control library.",
        )
    retired = [
        item for item in scoped
        if str(item.get("status", "")).strip().casefold() in {"retired", "inactive", "superseded"}
        or re.search(r"(?im)^\s*(?:control\s+)?status\s*:\s*(?:retired|inactive|superseded)\b", str(item.get("text", "")))
    ]
    if retired:
        return GateCheck(code="C03", title="Control validity and version", status="fail", overridable=False, detail=f"Mapped control {control_id} is marked retired, inactive, or superseded in the retrieved control source.")
    current_versions = {str(item.get("control_version", "")).strip() for item in scoped if item.get("control_version")}
    if not current_versions:
        return GateCheck(code="C03", title="Control validity and version", status="warning", overridable=False, detail=f"The in-scope indexed control {control_id} has no recorded version. Acknowledge this limitation and review the source before approval.")
    precedent_versions = {
        str(item.get("control_version", "")).strip()
        for group_name in ("past_issues", "duplicate_issues")
        for item in state.get("retrieved", {}).get(group_name, [])
        if item.get("control_id") == control_id and item.get("control_version")
    }
    mismatch = bool(current_versions and precedent_versions and not precedent_versions.issubset(current_versions))
    evidence = sorted(current_versions)
    if mismatch:
        return GateCheck(
            code="C03", title="Control validity and version", status="lead_override_required", overridable=True,
            detail=f"Retrieved precedent has a different control version from the indexed in-scope control. Indexed version: {', '.join(evidence)}. A lead auditor must override with a reason; the issue will be flagged superseded.",
            evidence=evidence,
        )
    version_note = f"Indexed control version: {', '.join(evidence) if evidence else 'not recorded'} (V1 indexed-source check)."
    return GateCheck(code="C03", title="Control validity and version", status="pass", detail=f"{control_id} exists in the retrieved client/RAU control library. {version_note}", evidence=evidence)


def _scope_check(state: dict[str, Any]) -> GateCheck:
    client_id, audit_id, rau_id = (str(state.get(key, "")).strip() for key in ("client_id", "audit_id", "rau_id"))
    attributes = [str(item).strip().casefold() for item in state.get("attributes_tested", []) if str(item).strip()]
    if not client_id or not audit_id or not rau_id or not attributes:
        return GateCheck(code="C04", title="RAU scope", status="fail", detail="Client, audit, RAU, and at least one tested attribute are required.")
    known_audits = {
        str(document.metadata.get("audit_id", ""))
        for group in build_documents().values() for document in group
        if document.metadata.get("client_id") == client_id and document.metadata.get("audit_id")
    }
    if audit_id not in known_audits:
        return GateCheck(code="C04", title="RAU scope", status="fail", detail="The selected audit ID is not associated with this synthetic client.")
    sources = _all_sources(state.get("retrieved", {}))
    controls = [item for item in state.get("retrieved", {}).get("controls", []) if item.get("client_id") == client_id and item.get("rau_id") == rau_id]
    if not controls:
        return GateCheck(code="C04", title="RAU scope", status="fail", detail="No retrieved control confirms the selected client and RAU scope.")
    control_attributes = " ".join(str(item.get("attribute", "")) for item in controls).casefold()
    missing_attributes = [item for item in attributes if item not in control_attributes]
    if missing_attributes:
        return GateCheck(code="C04", title="RAU scope", status="fail", detail=f"Tested attributes are outside the retrieved control scope: {', '.join(missing_attributes)}.")
    invalid = []
    for group_name in ("past_issues", "duplicate_issues"):
        for source in state.get("retrieved", {}).get(group_name, []):
            if source.get("client_id") != client_id or str(source.get("rau_id", "")) != rau_id:
                invalid.append(str(source.get("source_id", source.get("chunk_id", "unknown"))))
            elif group_name == "duplicate_issues" and source.get("audit_id") != audit_id:
                invalid.append(str(source.get("source_id", source.get("chunk_id", "unknown"))))
    if invalid:
        return GateCheck(code="C04", title="RAU scope", status="fail", detail=f"Retrieved issue sources cross client/RAU scope or the exact-audit duplicate scope: {', '.join(invalid)}.")
    return GateCheck(code="C04", title="RAU scope", status="pass", detail=f"Client {client_id}, audit context {audit_id}, RAU {rau_id}, and tested attributes match. Cross-audit precedent is permitted within the same client and RAU.", evidence=attributes)


def _duplicate_check(state: dict[str, Any], suggestion: TriageSuggestion) -> GateCheck:
    control_id = _control_id(suggestion.control_mapping.value)
    root_cause = suggestion.root_cause.value.strip().casefold()
    client_id, audit_id, rau_id = (str(state.get(key, "")).strip() for key in ("client_id", "audit_id", "rau_id"))
    open_statuses = {"open", "in progress", "overdue", "pending", "reopened"}
    matches = []
    for document in build_documents().get("past_issues", []):
        metadata = document.metadata
        if (
            metadata.get("client_id") == client_id
            and metadata.get("audit_id") == audit_id
            and metadata.get("rau_id") == rau_id
            and metadata.get("control_id") == control_id
            and str(metadata.get("root_cause", "")).strip().casefold() == root_cause
            and str(metadata.get("status", "")).strip().casefold() in open_statuses
        ):
            matches.append(str(metadata.get("issue_id") or metadata.get("doc_id")))
    if matches:
        return GateCheck(
            code="C05", title="Duplicate check", status="fail", overridable=False,
            detail="An open local synthetic issue matches the same client, audit, RAU, control, and root cause. Do not approve or create a new Jira issue; link/review the existing record instead.",
            evidence=sorted(set(matches)),
        )
    return GateCheck(
        code="C05", title="Duplicate check", status="pass", overridable=False,
        detail="No open duplicate was found in the local synthetic issue corpus for this client, audit, RAU, control, and root cause. The Jira board was not queried.",
    )


def evaluate_preapproval(state: dict[str, Any], suggestion: TriageSuggestion, settings: Settings) -> list[GateCheck]:
    citation_complete = all(
        claim.value.strip().casefold() == "insufficient evidence" or bool(claim.citations)
        for _, claim in _claims(suggestion)
    )
    checks = [GateCheck(
        code="C01", title="Citation completeness", status="pass" if citation_complete else "fail",
        detail="Every supported severity, root-cause, control, and remediation claim has at least one citation; explicitly insufficient-evidence fields remain uncited." if citation_complete else "Every supported checked field must have a citation. Re-run triage or return the field as insufficient evidence.",
    )]
    valid, errors = _check_citations(suggestion, state.get("retrieved", {}))
    checks.append(GateCheck(
        code="C02", title="Citation validity", status="pass" if valid else "fail",
        detail="Every citation resolves to a chunk retrieved in this run, its document ID matches, and its quote is verbatim." if valid else "; ".join(errors),
        evidence=errors,
    ))
    checks.append(_control_check(state, suggestion))
    checks.append(_scope_check(state))
    checks.append(_duplicate_check(state, suggestion))
    email = str(settings.business_owner_email or "").strip()
    valid_email = bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email))
    configured = bool(valid_email and settings.email_notification_ready)
    checks.append(GateCheck(
        code="EV1", title="Business-owner email verification", status="pass" if configured else "fail",
        detail="The configured owner address and delivery backend are present and syntactically valid; no email was sent." if configured else "Configure a syntactically valid BUSINESS_OWNER_EMAIL and a ready email backend before approval. No email is sent during this check.",
    ))
    return checks


def gate_approval(
    checks: list[GateCheck], acknowledgements: dict[str, bool], lead_override_id: str,
    lead_override_reason: str, allowed_leads: tuple[str, ...] | list[str],
) -> ApprovalGateResult:
    final_checks = [check.model_copy(deep=True) for check in checks]
    blockers: list[str] = []
    for check in final_checks:
        if check.status == "fail":
            blockers.append(check.code)
        elif check.status == "warning" and not acknowledgements.get(check.code, False):
            blockers.append(check.code)
        elif check.status == "lead_override_required":
            if check.code != "C03" or lead_override_id not in allowed_leads or not lead_override_reason.strip():
                blockers.append(check.code)
            else:
                check.status = "lead_overridden"
                check.detail += f" Lead override recorded by {lead_override_id}: {lead_override_reason.strip()}"
    return ApprovalGateResult(checks=final_checks, ready=not blockers, blockers=blockers)

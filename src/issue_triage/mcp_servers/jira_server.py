"""Single-tool Jira MCP server. It creates a labeled draft only from a persisted approval."""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server import MCPServer

from ..audit_log.store import AuditStore

server = MCPServer("issue-triage-jira-draft")

def _paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}

def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()

def _verify_citations(suggestion: dict[str, Any], retrieved: dict[str, list[dict[str, Any]]]) -> None:
    chunks = {item.get("chunk_id"): item for group in retrieved.values() for item in group}
    claims: list[dict[str, Any]] = []
    for name in ("severity", "root_cause", "control_mapping", "duplicate_warning", "confidence"):
        claim = suggestion.get(name)
        if isinstance(claim, dict): claims.append(claim)
    for step in suggestion.get("remediation_steps", []):
        for name in ("action", "owner_role", "due_date_suggestion"):
            claim = step.get(name)
            if isinstance(claim, dict): claims.append(claim)
    for claim in claims:
        value = str(claim.get("value", ""))
        citations = claim.get("citations") or []
        if value.casefold() != "insufficient evidence" and not citations:
            raise ValueError("Approved suggestion contains a supported claim without a citation.")
        for citation in citations:
            source = chunks.get(citation.get("chunk_id"))
            if not source or source.get("source_id") != citation.get("source_id"):
                raise ValueError("Approved suggestion contains a citation outside this run's retrieved sources.")
            if _normalise(citation.get("quote", "")) not in _normalise(source.get("text", "")):
                raise ValueError("Approved suggestion contains a citation quote that does not match its retrieved chunk.")

def _jira_base() -> str:
    raw = os.getenv("JIRA_BASE_URL", "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("JIRA_BASE_URL must be an HTTPS Atlassian site URL without embedded credentials.")
    return f"https://{parsed.netloc}"

def _jira_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base = _jira_base()
    email, token = os.getenv("JIRA_EMAIL", "").strip(), os.getenv("JIRA_API_TOKEN", "")
    if not email or not token:
        raise RuntimeError("Jira credentials are not configured in the server environment.")
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=body, method=method,
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw=response.read()
            return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as exc:
        # Jira's validation messages identify rejected fields; never log the response body.
        try:
            response = json.loads(exc.read().decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = {}
        messages = [str(item) for item in response.get("errorMessages", []) if item]
        field_errors = response.get("errors", {})
        if isinstance(field_errors, dict):
            messages.extend(f"{field}: {message}" for field, message in field_errors.items() if message)
        detail = "; ".join(messages)[:240]
        reason = f"Jira API returned HTTP {exc.code}" + (f": {detail}" if detail else ".")
        raise RuntimeError(reason) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Jira API connection failed or timed out.") from exc

def _heading(text: str, level: int = 2) -> dict[str, Any]:
    return {"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": text}]}

def _labelled_paragraph(label: str, value: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [
        {"type": "text", "text": f"{label}: ", "marks": [{"type": "strong"}]},
        {"type": "text", "text": value or "Not provided"},
    ]}

def _citation_list(claim: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for citation in claim.get("citations", []):
        text = f"{citation.get('source_id', 'Source')} · {citation.get('chunk_id', 'chunk')}: {citation.get('quote', '')}"
        items.append({"type": "listItem", "content": [_paragraph(text)]})
    return items

def _description(issue: dict[str, Any], suggestion: dict[str, Any], retrieved: dict[str, list[dict[str, Any]]], run_id: str, approval_id: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        _heading("Auditor-approved issue draft", 1),
        _paragraph("Prepared for business-owner review. The auditor retains responsibility for the final severity and disposition."),
        _labelled_paragraph("Triage run", run_id),
        _labelled_paragraph("Approval record", approval_id),
        _labelled_paragraph("Synthetic client", str(issue.get("client_id", ""))),
        _labelled_paragraph("Audit", str(issue.get("audit_id", ""))),
        _heading("Issue details"),
        _labelled_paragraph("Title", str(issue.get("issue_title", ""))),
        _paragraph(str(issue.get("issue_description", ""))),
        _heading("Triage assessment"),
    ]
    for label, key in (("Suggested severity", "severity"), ("Root-cause category", "root_cause"), ("Mapped control", "control_mapping"), ("Duplicate screening", "duplicate_warning"), ("Retrieval confidence", "confidence")):
        claim = suggestion.get(key, {})
        if not claim:
            continue
        content.append(_labelled_paragraph(label, str(claim.get("value", ""))))
        if claim.get("rationale"):
            content.append(_labelled_paragraph("Rationale", str(claim["rationale"])))
        citation_items = _citation_list(claim)
        if citation_items:
            content.append(_paragraph("Supporting citations:"))
            content.append({"type": "bulletList", "content": citation_items})
    steps = suggestion.get("remediation_steps", [])
    content.append(_heading("Draft remediation plan"))
    if steps:
        list_items = []
        for step in steps:
            action = step.get("action", {})
            owner = step.get("owner_role", {})
            due = step.get("due_date_suggestion", {})
            lines = [
                _labelled_paragraph("Action", str(action.get("value", ""))),
                _labelled_paragraph("Suggested owner role", str(owner.get("value", ""))),
                _labelled_paragraph("Suggested due date", str(due.get("value", ""))),
            ]
            for claim in (action, owner, due):
                lines.extend(_paragraph(f"Source · {citation.get('source_id', 'Source')} · {citation.get('chunk_id', 'chunk')}: {citation.get('quote', '')}") for citation in claim.get("citations", []))
            list_items.append({"type": "listItem", "content": lines})
        content.append({"type": "orderedList", "content": list_items})
    else:
        content.append(_paragraph("No remediation steps were supported by the approved suggestion."))
    content.extend([
        _heading("Review note"),
        _paragraph("This Jira item was created from the persisted auditor-approved suggestion. Review the cited source material and determine remediation ownership and timing through the normal business process."),
    ])
    return {"version": 1, "type": "doc", "content": content}

def _create_issue_draft_impl(run_id: str, client_id: str, approver: str) -> dict[str, Any]:
    """Create a Jira issue marked as an approved triage draft after validating the durable human approval."""
    allowed_reviewers={value.strip() for value in os.getenv("JIRA_DRAFT_ALLOWED_AUDITOR_IDS", os.getenv("MCP_AUDITOR_IDS", "")).split(",") if value.strip()}
    if approver not in allowed_reviewers:
        raise PermissionError("This reviewer is not allowed to create Jira drafts.")
    store=AuditStore(os.getenv("DATABASE_URL", "sqlite:///./data/issue_triage.db")); store.initialize()
    existing=store.jira_draft_for_run(run_id)
    if existing: return {"status":"already_created", "issue_key":existing["issue_key"], "issue_url":existing["issue_url"], "approval_id":existing["approval_id"]}
    approved=store.approved_jira_payload(run_id, client_id, approver)
    if not approved:
        raise PermissionError("A matching persisted approval with a complete pre-approval gate record is required.")
    if approved.get("superseded_control"):
        lead_ids={value.strip() for value in os.getenv("MCP_LEAD_AUDITOR_IDS", "").split(",") if value.strip()}
        if approved.get("lead_override_id") not in lead_ids or not str(approved.get("lead_override_reason", "")).strip():
            raise PermissionError("The superseded-control label requires a recorded allowlisted lead override.")
    suggestion=approved["suggestion"]; retrieved=approved["retrieved"]
    if suggestion.get("retrieval_state") != "grounded":
        raise ValueError("Only a grounded, human-approved suggestion can create a Jira draft.")
    _verify_citations(suggestion, retrieved)
    project=os.getenv("JIRA_PROJECT_KEY", "").strip()
    issue_type=os.getenv("JIRA_ISSUE_TYPE", "Task").strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", project): raise RuntimeError("JIRA_PROJECT_KEY must be a valid project key.")
    summary=" ".join(str(approved["issue"].get("issue_title", "Audit issue triage draft")).split())[:255]
    run_label="triage-run-" + re.sub(r"[^a-zA-Z0-9]", "", run_id)[:32].lower()
    labels=["issue-triage-assistant-draft", "human-approved-triage", run_label]
    if approved.get("superseded_control"):
        labels.append("superseded-control-review-required")
    # A deterministic run label prevents duplicate Jira issues if a response is lost after create.
    jql=urllib.parse.quote(f'project = "{project}" AND labels = "{run_label}"')
    existing=_jira_request("GET",f"/rest/api/3/search/jql?jql={jql}&fields=key&maxResults=1")
    matches=existing.get("issues",[])
    if matches:
        issue_key=str(matches[0].get("key", ""))
        issue_url=f"{_jira_base()}/browse/{urllib.parse.quote(issue_key,safe='')}"
        store.record_jira_draft(run_id,approved["approval_id"],issue_key,issue_url)
        return {"status":"already_created", "issue_key":issue_key, "issue_url":issue_url, "approval_id":approved["approval_id"]}
    payload={"fields":{"project":{"key":project},"issuetype":{"name":issue_type},"summary":summary,
        "description":_description(approved["issue"],suggestion,retrieved,run_id,approved["approval_id"]),"labels":labels}}
    created=_jira_request("POST","/rest/api/3/issue",payload)
    issue_key=created.get("key")
    if not issue_key: raise RuntimeError("Jira accepted the request but returned no issue key.")
    issue_url=f"{_jira_base()}/browse/{urllib.parse.quote(str(issue_key),safe='')}"
    store.record_jira_draft(run_id,approved["approval_id"],str(issue_key),issue_url)
    return {"status":"created", "issue_key":str(issue_key), "issue_url":issue_url, "approval_id":approved["approval_id"]}

@server.tool()
def create_issue_draft(run_id: str, client_id: str, approver: str) -> dict[str, Any]:
    """Create a Jira draft or return a structured, sanitized error to the MCP client."""
    try:
        return _create_issue_draft_impl(run_id, client_id, approver)
    except Exception as exc:
        detail = str(exc).strip()
        for secret in (os.getenv("JIRA_API_TOKEN", ""), os.getenv("JIRA_EMAIL", ""), os.getenv("DATABASE_URL", "")):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        detail = " ".join(detail.split())[:300]
        return {"status": "error", "error_type": type(exc).__name__, "error": detail or "No error detail supplied."}

if __name__ == "__main__":
    server.run(transport="stdio")

"""Governed MCP server for internal, cited summary-document drafts only."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

from mcp.server import MCPServer

from ..audit_log.store import AuditStore
from ..triage.document_schema import SummaryDocumentRequest, build_summary_document

server = MCPServer("issue-triage-documents")


def _ids(name: str) -> set[str]:
    return {value.strip() for value in os.getenv(name, "").split(",") if value.strip()}


def _safe_title(value: str) -> str:
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value) or re.search(r"(?i)(api[_ -]?key|password|secret|token)\s*[:=]", value):
        raise ValueError("The document title cannot contain an email address or secret-like value.")
    return value


def _create_impl(
    *, run_id: str, flow_id: str, client_id: str, audit_id: str, issue_ids: list[str] | None,
    cluster_id: str, template: str, title: str, include_remediation_drafts: bool,
    requested_by_role: str, requested_by_id: str, rau_id: str,
) -> dict[str, Any]:
    request = SummaryDocumentRequest.model_validate({
        "run_id": run_id, "flow_id": flow_id, "client_id": client_id, "audit_id": audit_id,
        "rau_id": rau_id or None,
        "issue_ids": issue_ids or [], "cluster_id": cluster_id or None,
        "template": template, "title": _safe_title(title),
        "include_remediation_drafts": include_remediation_drafts,
        "requested_by_role": requested_by_role, "requested_by_id": requested_by_id,
    })
    allowed = _ids("MCP_LEAD_AUDITOR_IDS") if request.requested_by_role == "lead_auditor" else _ids("MCP_AUDITOR_IDS")
    if request.requested_by_id not in allowed:
        raise PermissionError("The requester is not authorized to create summary drafts for the requested role.")

    store = AuditStore(os.getenv("DATABASE_URL", "sqlite:///./data/issue_triage.db"))
    store.initialize()
    scope = store.scoping_summary_source(request.run_id, request.client_id, request.audit_id)
    if not scope:
        raise PermissionError("The scoping run, client, and audit context did not match a persisted run.")
    pack = scope["pack"]
    if request.rau_id and request.rau_id != scope["rau_id"]:
        raise PermissionError("The requested RAU does not match the persisted scoping run.")
    citation_sources = pack.get("citation_sources", {})
    source_by_id: dict[str, dict[str, Any]] = {}
    for source in citation_sources.values():
        if source.get("source_type") not in {"past_issues", "past_issue"}:
            continue
        if source.get("client_id") != request.client_id:
            continue
        if source.get("source_id") and source.get("chunk_id") and source.get("text"):
            source_by_id[str(source["source_id"])] = source

    cluster_rows = pack.get("rows", [])
    if request.cluster_id:
        matched_rows = [row for row in cluster_rows if row.get("cluster_id") == request.cluster_id]
        if not matched_rows:
            raise ValueError("The cluster ID is not part of this scoping run.")
        cluster_rows = matched_rows
        selected_ids = list(dict.fromkeys(str(item) for row in matched_rows for item in row.get("issue_ids", [])))
    else:
        selected_ids = request.issue_ids
    if not selected_ids:
        raise ValueError("No retrieved issue records are in scope; no summary document was created.")
    if len(selected_ids) > 50:
        raise ValueError("The summary is limited to 50 issues; split by cluster and create separate drafts.")
    unknown = [issue_id for issue_id in selected_ids if issue_id not in source_by_id]
    if unknown:
        raise PermissionError("Every requested issue must be a past-issue source retrieved in this scoping run.")
    if request.template == "cluster_summary" and len(cluster_rows) != 1:
        raise ValueError("A cluster summary must refer to exactly one retrieved cluster.")

    document_id = str(uuid.uuid4())
    document = build_summary_document(
        document_id=document_id, request=request, pack=pack, sources=source_by_id,
        selected_ids=selected_ids, cluster_rows=cluster_rows,
    )
    store.save_summary_document(document)
    return document


@server.tool()
def create_summary_document_draft(
    run_id: str, flow_id: str, client_id: str, audit_id: str, issue_ids: list[str], cluster_id: str,
    template: str, title: str, include_remediation_drafts: bool,
    requested_by_role: str, requested_by_id: str, rau_id: str,
) -> dict[str, Any]:
    """Create an internal draft from retrieved sources; never publishes, shares, or overwrites."""
    started = time.monotonic()
    redacted = {
        "run_id": "[REDACTED]", "flow_id": "[REDACTED]", "client_id": "[REDACTED]", "audit_id": "[REDACTED]",
        "issue_ids": "[REDACTED]", "cluster_id": "[REDACTED]", "title": "[REDACTED]",
        "requested_by_id": "[REDACTED]", "requested_by_role": requested_by_role,
    }
    store: AuditStore | None = None
    try:
        store = AuditStore(os.getenv("DATABASE_URL", "sqlite:///./data/issue_triage.db"))
        store.initialize()
        result = _create_impl(
            run_id=run_id, flow_id=flow_id, client_id=client_id, audit_id=audit_id, issue_ids=issue_ids,
            cluster_id=cluster_id, template=template, title=title,
            include_remediation_drafts=include_remediation_drafts,
            requested_by_role=requested_by_role, requested_by_id=requested_by_id,
            rau_id=rau_id,
        )
        store.append_tool_call(run_id, "documents", "create_summary_document_draft", redacted, "success",
                               round((time.monotonic() - started) * 1000), approver=requested_by_id,
                               result_reference=f"{result['document_id']}@v{result['version']}")
        return result
    except Exception as exc:
        if store:
            try:
                store.append_tool_call(run_id, "documents", "create_summary_document_draft", redacted, "failed",
                                       round((time.monotonic() - started) * 1000), approver=requested_by_id)
            except Exception:
                pass
        detail = str(exc).strip()
        for secret in (os.getenv("DATABASE_URL", ""), os.getenv("EMAIL_SMTP_PASSWORD", ""), os.getenv("JIRA_API_TOKEN", "")):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        return {"status": "error", "error_type": type(exc).__name__, "error": " ".join(detail.split())[:400]}


if __name__ == "__main__":
    server.run(transport="stdio")

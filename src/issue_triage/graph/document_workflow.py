"""Checkpointed internal summary-draft and human document-approval workflow."""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..audit_log.store import AuditStore
from ..config import Settings
from ..integrations.mcp_clients import create_summary_document_draft
from ..triage.document_schema import SummaryDocument, validate_summary_document
from .workflow import get_checkpointer


class SummaryDocumentState(TypedDict, total=False):
    request: dict[str, Any]
    document: dict[str, Any]
    document_error: str
    decision: dict[str, Any]
    review_pending: bool
    review_complete: bool
    log: list[dict[str, Any]]


def build_summary_document_graph(settings: Settings):
    def create_draft(state: SummaryDocumentState) -> dict[str, Any]:
        try:
            document = create_summary_document_draft(settings, state["request"])
            return {"document": document, "review_pending": True, "review_complete": False}
        except Exception as exc:
            return {"document_error": str(exc), "review_pending": False, "review_complete": True}

    def review_document(state: SummaryDocumentState) -> dict[str, Any]:
        document = state["document"]
        decision = interrupt({
            "document": document,
            "allowed_decisions": ["approve", "edit", "reject"],
            "version": document["version"],
        })
        if decision.get("decision") not in {"approve", "edit", "reject"}:
            raise ValueError("Document decision must be approve, edit, or reject.")
        if not str(decision.get("reason", "")).strip() or not str(decision.get("approver", "")).strip():
            raise ValueError("A document decision requires an approver and reason.")
        return {"decision": decision}

    def finalize_document(state: SummaryDocumentState) -> dict[str, Any]:
        document = state["document"]
        decision = state["decision"]
        approver = str(decision["approver"])
        role = str(decision.get("approver_role", "auditor"))
        if role == "lead_auditor":
            allowed = settings.mcp_lead_auditor_ids
        elif role == "auditor":
            allowed = settings.mcp_auditor_ids
        else:
            raise PermissionError("The reviewer role is not permitted for document decisions.")
        if approver not in allowed:
            raise PermissionError("This reviewer is not authorized to decide on the summary document.")
        edited = None
        if decision["decision"] == "edit":
            edited = SummaryDocument.model_validate(decision.get("edited_document", {})).model_dump(mode="json")
            for field in (
                "document_id", "flow_id", "version", "status", "created_at", "location", "draft_header",
                "client_id", "audit_id", "audit_ids", "rau_id", "template", "source_issue_ids", "counts",
                "created_by_run_id", "coverage", "unresolved_gaps", "warnings", "suggested_next_actions",
            ):
                if edited.get(field) != document.get(field):
                    raise ValueError(f"Document editing cannot change the locked field {field}.")
            original_sections = document.get("sections", [])
            edited_sections = edited.get("sections", [])
            if len(edited_sections) != len(original_sections):
                raise ValueError("Document editing cannot add or remove sections.")
            for original, changed in zip(original_sections, edited_sections):
                if original.get("heading") != changed.get("heading") or len(original.get("bullets", [])) != len(changed.get("bullets", [])):
                    raise ValueError("Document editing cannot change section headings or bullet structure.")
                for original_bullet, changed_bullet in zip(original.get("bullets", []), changed.get("bullets", [])):
                    if original_bullet.get("citations") != changed.get("citations"):
                        raise ValueError("Document editing cannot change or remove citations.")
            sources = _run_sources(settings, document["created_by_run_id"], document["client_id"], document["audit_id"])
            errors = validate_summary_document(edited, sources)
            if errors:
                raise ValueError("Edited document failed citation validation: " + "; ".join(errors))
        store = AuditStore(settings.database_url)
        store.initialize()
        version = store.record_summary_document_decision(
            document["document_id"], document["client_id"], int(document["version"]),
            "edit" if decision["decision"] == "edit" else ("approved" if decision["decision"] == "approve" else "rejected"),
            str(decision["reason"]).strip(), approver, role, edited_document=edited,
        )
        current = store.summary_document(document["document_id"], document["client_id"])
        return {"document": current, "review_pending": decision["decision"] == "edit", "review_complete": decision["decision"] != "edit", "decision": decision}

    def route_after_finalize(state: SummaryDocumentState) -> str:
        return "review" if state.get("review_pending") else "done"

    builder = StateGraph(SummaryDocumentState)
    builder.add_node("create_draft", create_draft)
    builder.add_node("review_document", review_document)
    builder.add_node("finalize_document", finalize_document)
    builder.add_edge(START, "create_draft")
    builder.add_conditional_edges("create_draft", lambda state: "review" if state.get("review_pending") else "done", {"review": "review_document", "done": END})
    builder.add_edge("review_document", "finalize_document")
    builder.add_conditional_edges("finalize_document", route_after_finalize, {"review": "review_document", "done": END})
    return builder.compile(checkpointer=get_checkpointer(settings))


def _run_sources(settings: Settings, run_id: str, client_id: str, audit_id: str) -> dict[str, dict[str, Any]]:
    store = AuditStore(settings.database_url)
    store.initialize()
    source = store.scoping_summary_source(run_id, client_id, audit_id)
    if not source:
        raise PermissionError("The document's source scoping run is no longer available in this client/audit context.")
    return {item["chunk_id"]: item for item in source["pack"].get("citation_sources", {}).values() if item.get("chunk_id")}

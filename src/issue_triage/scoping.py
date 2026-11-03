"""V1 RAU scoping retrieval and deterministic issue clustering."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

from .config import Settings
from .retrieval.pinecone_store import PineconeRetriever


def build_scoping_pack(
    settings: Settings, *, query: str, client_id: str = "", audit_id: str = "", rau_id: str,
    control_ids: list[str] | None = None, attributes: list[str] | None = None,
    audit_period: str = "", lookback_years: int = 2,
) -> dict[str, Any]:
    control_ids = list(dict.fromkeys(control_ids or []))
    attributes = list(dict.fromkeys(attributes or []))
    since = (date.today() - timedelta(days=365 * lookback_years)).isoformat()
    search_query = query
    if audit_period.strip():
        search_query = f"{query}\nAudit period: {audit_period.strip()}"
    retriever = PineconeRetriever(settings)
    audit_years, period_audit_ids = retriever.audit_ids_for_period(
        client_id=client_id,
        audit_period=audit_period,
    )
    issues, excluded = retriever.retrieve_scoping(
        query=search_query, client_id=client_id, audit_id=audit_id, rau_id=rau_id,
        control_ids=control_ids, since=since, period_audit_ids=period_audit_ids,
    )
    controls = retriever.retrieve_scope_controls(
        query=search_query, client_id=client_id, audit_id=audit_id, rau_id=rau_id,
        control_ids=control_ids, period_audit_ids=period_audit_ids,
    )
    policy_query = f"{search_query}; RAU {rau_id}" + (f"; controls {', '.join(control_ids)}" if control_ids else "")
    policies = retriever.retrieve_scope_policies(
        query=policy_query, client_id=client_id, period_audit_ids=period_audit_ids,
    )
    controls_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in controls:
        controls_by_id.setdefault(item["control_id"], []).append(item)
    controls_by_id = {key: list({item["chunk_id"]: item for item in rows}.values()) for key, rows in controls_by_id.items()}

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in issues:
        attr = item.get("attribute") or "Unspecified attribute"
        root = item.get("root_cause") or "Unspecified root cause"
        grouped.setdefault((item.get("control_id", ""), attr, root), []).append(item)

    rows: list[dict[str, Any]] = []
    for (control_id, attribute, root_cause), members in grouped.items():
        source_control = next(iter(controls_by_id.get(control_id, [])), None)
        versions = {item.get("control_version") for item in members if item.get("control_version")}
        current_version = source_control.get("control_version") if source_control else None
        statuses = {item.get("status", "").lower() for item in members}
        if current_version and versions and current_version not in versions:
            action = "Superseded control — auditor decides"
            procedure = "Confirm current control design and decide whether a revised procedure is needed."
        elif "open" in statuses or "overdue" in statuses:
            action = "Carry forward and retest"
            procedure = "Confirm the outstanding action, retest the affected attribute, and link the existing issue rather than creating a duplicate."
        elif len(members) > 1:
            action = "Test the recurring root cause"
            procedure = "Expand the sample or re-perform the control for the shared root cause and verify the corrective action remained effective."
        else:
            action = "Lighter testing"
            procedure = "Test a small sample to confirm the documented remediation remained effective."
        chunk_ids = [member["chunk_id"] for member in members]
        if source_control:
            chunk_ids.append(source_control["chunk_id"])
        rows.append({
            "cluster_id": hashlib.sha1("|".join((rau_id, control_id, attribute, root_cause)).encode()).hexdigest()[:12],
            "control_id": control_id, "control_version": current_version or next(iter(versions), "Unknown"),
            "attribute": attribute, "root_cause": root_cause, "issue_ids": [item["source_id"] for item in members],
            "action": action, "procedure": procedure,
            "priority": "High" if action in {"Carry forward and retest", "Test the recurring root cause", "Superseded control — auditor decides"} else "Medium",
            "priority_rationale": f"{len(members)} past issue(s); status(es): {', '.join(sorted(statuses)) or 'unknown'}.",
            "citations": chunk_ids, "members": members, "control_source": source_control,
        })

    represented = {row["control_id"] for row in rows}
    scoped_control_ids = control_ids or sorted({*controls_by_id, *(item.get("control_id", "") for item in issues)} - {""})
    for control_id in scoped_control_ids:
        if control_id in represented:
            continue
        control = next(iter(controls_by_id.get(control_id, [])), None)
        if not control:
            rows.append({
                "cluster_id": hashlib.sha1(f"{rau_id}|{control_id}|no-precedent".encode()).hexdigest()[:12],
                "control_id": control_id, "control_version": "Unknown", "attribute": ", ".join(attributes) or "Auditor-defined attribute",
                "root_cause": "No past issue retrieved", "issue_ids": [], "action": "Test on inherent risk",
                "procedure": "Assess the in-scope control and the auditor-identified attributes even though no past issue was retrieved.",
                "priority": "Auditor-set", "priority_rationale": "No past issue was retrieved; absence of precedent is not evidence of a good control.",
                "citations": [], "members": [], "control_source": None,
            })
        else:
            rows.append({
                "cluster_id": hashlib.sha1(f"{rau_id}|{control_id}|no-precedent".encode()).hexdigest()[:12],
                "control_id": control_id, "control_version": control.get("control_version", "Unknown"),
                "attribute": ", ".join(attributes) or control.get("attribute", "Auditor-defined attribute"),
                "root_cause": "No past issue retrieved", "issue_ids": [], "action": "Test on inherent risk",
                "procedure": "Assess the control against the auditor-identified attributes; no prior issue was found for this RAU/control.",
                "priority": "Auditor-set", "priority_rationale": "No past issue was retrieved; absence of precedent is not evidence of a good control.",
                "citations": [control["chunk_id"]], "members": [], "control_source": control,
            })
    rows.sort(key=lambda row: (0 if row["priority"] == "High" else 1, row["control_id"], row["attribute"]))
    citation_sources = {item["chunk_id"]: item for item in [*policies, *(row for group in controls_by_id.values() for row in group), *issues, *excluded]}
    return {
        "rau_id": rau_id, "audit_period": audit_period, "audit_years": audit_years,
        "audit_ids_searched": period_audit_ids, "lookback_since": since, "rows": rows,
        "excluded_older_issues": excluded,
        "retrieved_control_count": sum(len(items) for items in controls_by_id.values()),
        "citation_sources": citation_sources, "policy_sources": policies,
    }

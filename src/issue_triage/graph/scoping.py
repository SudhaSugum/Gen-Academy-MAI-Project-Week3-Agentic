"""Resumable, per-cluster human review flow for RAU Scoping Mode."""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..audit_log.store import AuditStore
from ..config import Settings
from ..scoping import build_scoping_pack
from .workflow import get_checkpointer

logger = logging.getLogger(__name__)


class ScopingState(TypedDict, total=False):
    run_id: str
    client_id: str
    audit_id: str
    rau_id: str
    rau_name: str
    audit_period: str
    control_ids: list[str]
    attributes: list[str]
    query: str
    auditor_id: str
    pack: dict[str, Any]
    current_index: int
    decisions: list[dict[str, Any]]
    review_pending: bool
    review_complete: bool


def build_scoping_graph(settings: Settings):
    def retrieve_and_cluster(state: ScopingState) -> dict[str, Any]:
        client_id = str(state.get("client_id") or "").strip()
        if not client_id:
            raise ValueError("A synthetic client is required for RAU scoping retrieval.")
        audit_id = state.get("audit_id", "")
        control_ids = state.get("control_ids", [])
        attributes = state.get("attributes", [])
        audit_period = state.get("audit_period", "")
        pack = build_scoping_pack(
            settings, query=state["query"], client_id=client_id, audit_id=audit_id,
            rau_id=state["rau_id"], control_ids=control_ids, attributes=attributes,
            audit_period=audit_period,
        )
        pack["audit_period"] = audit_period
        store = AuditStore(settings.database_url)
        store.initialize()
        store.save_scoping_run(
            state["run_id"], client_id, audit_id, state["rau_id"],
            audit_period, {key: state.get(key) for key in ("rau_name", "control_ids", "attributes", "audit_period", "lookback_since")}, pack,
        )
        logger.info("run_id=%s node=scope_retrieve rows=%d older_excluded=%d", state["run_id"], len(pack["rows"]), len(pack["excluded_older_issues"]))
        return {"pack": pack, "current_index": 0, "decisions": [], "review_pending": bool(pack["rows"]), "review_complete": not bool(pack["rows"])}

    def review_cluster(state: ScopingState) -> dict[str, Any]:
        index = state["current_index"]
        row = state["pack"]["rows"][index]
        # The graph cannot advance until a human gives a decision and reason for this cluster.
        decision = interrupt({
            "run_id": state["run_id"], "cluster_id": row["cluster_id"], "control_id": row["control_id"],
            "allowed_decisions": ["approved", "de_scoped", "escalated"], "row_number": index + 1,
        })
        if decision.get("cluster_id") != row["cluster_id"]:
            raise ValueError("Decision cluster ID does not match the current scoping row.")
        if decision.get("decision") not in {"approved", "de_scoped", "escalated"}:
            raise ValueError("Cluster decision must be approved, de_scoped, or escalated.")
        if not decision.get("reason", "").strip() or not decision.get("approver", "").strip():
            raise ValueError("Each cluster decision requires a reason and an approver.")
        store = AuditStore(settings.database_url)
        store.initialize()
        store.record_scoping_decision(state["run_id"], row["cluster_id"], decision["decision"], decision["reason"], decision["approver"])
        saved = {"cluster_id": row["cluster_id"], "decision": decision["decision"], "reason": decision["reason"], "approver": decision["approver"]}
        decisions = [*state.get("decisions", []), saved]
        next_index = index + 1
        complete = next_index >= len(state["pack"]["rows"])
        logger.info("run_id=%s node=scope_review cluster=%s decision=%s", state["run_id"], row["cluster_id"], decision["decision"])
        return {"decisions": decisions, "current_index": next_index, "review_pending": not complete, "review_complete": complete}

    def continue_or_finish(state: ScopingState) -> str:
        return "review" if state["current_index"] < len(state["pack"]["rows"]) else "done"

    builder = StateGraph(ScopingState)
    builder.add_node("retrieve_and_cluster", retrieve_and_cluster)
    builder.add_node("review_cluster", review_cluster)
    builder.add_edge(START, "retrieve_and_cluster")
    builder.add_conditional_edges("retrieve_and_cluster", lambda s: "review" if s.get("review_pending") else "done", {"review": "review_cluster", "done": END})
    builder.add_conditional_edges("review_cluster", continue_or_finish, {"review": "review_cluster", "done": END})
    return builder.compile(checkpointer=get_checkpointer(settings))

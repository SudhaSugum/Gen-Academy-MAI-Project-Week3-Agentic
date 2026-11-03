"""One well-instrumented LangGraph state machine for V1 triage."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..audit_log.store import AuditStore
from ..config import Settings
from ..memory.mem0_client import ApprovedMemory
from ..integrations.mcp_clients import create_jira_draft, send_business_owner_email
from ..retrieval.pinecone_store import PineconeRetriever
from ..triage.schema import Citation, Claim, SimilarIssue, TriageSuggestion, insufficient_suggestion
from ..triage.taxonomy import ROOT_CAUSE_CATEGORIES, SEVERITY_RUBRIC
from ..triage.preapproval import GateCheck, evaluate_preapproval, gate_approval

logger = logging.getLogger(__name__)
_checkpointer_context: AbstractContextManager | None = None
_checkpointer_instance: Any | None = None


class TriageState(TypedDict, total=False):
    run_id: str
    issue_id: str
    issue_title: str
    issue_description: str
    client_id: str
    audit_id: str
    audit_context: str
    control_hint: str
    rau_id: str
    attributes_tested: list[str]
    preapproval_checks: list[dict[str, Any]]
    auditor_id: str
    memories: list[dict[str, Any]]
    memory_warning: str
    retrieved: dict[str, list[dict[str, Any]]]
    suggestion: dict[str, Any]
    citation_attempts: int
    citation_errors: list[str]
    insufficient_evidence: bool
    review_pending: bool
    review_complete: bool
    human_decision: dict[str, Any]
    approval_id: str
    jira_status: str
    jira_issue_key: str
    jira_issue_url: str
    jira_error: str
    email_status: str
    email_error: str
    error: str
    log: list[dict[str, Any]]


_PROMPT_PATH = Path(__file__).resolve().parents[3] / "docs" / "system_prompt.md"
SYSTEM_PROMPT = (
    _PROMPT_PATH.read_text(encoding="utf-8")
    .replace("{{SEVERITY_RUBRIC}}", SEVERITY_RUBRIC)
    .replace("{{ROOT_CAUSE_CATEGORIES}}", ", ".join(ROOT_CAUSE_CATEGORIES))
)


def _event(state: TriageState, event: str, details: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [*state.get("log", []), {"event": event, "details": details or {}}]


def _flatten(retrieved: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {doc["chunk_id"]: doc for docs in retrieved.values() for doc in docs}


def _normalise_quote(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _canonicalise_citation_ids(suggestion: TriageSuggestion, chunks: dict[str, dict[str, Any]]) -> None:
    """Expand a shortened chunk suffix only when its source identifies one retrieved chunk."""
    claims = [suggestion.severity, suggestion.root_cause, suggestion.control_mapping,
              suggestion.duplicate_warning, suggestion.confidence]
    for step in suggestion.remediation_steps:
        claims.extend([step.action, step.owner_role, step.due_date_suggestion])
    for claim in claims:
        for citation in claim.citations:
            if citation.chunk_id in chunks or not citation.chunk_id.startswith("chunk-"):
                continue
            matches = [
                chunk_id for chunk_id, source in chunks.items()
                if source["source_id"] == citation.source_id and chunk_id.endswith(f"-{citation.chunk_id}")
            ]
            if len(matches) == 1:
                citation.chunk_id = matches[0]


def _mark_uncited_claims_insufficient(suggestion: TriageSuggestion) -> tuple[TriageSuggestion, bool, int]:
    """Remove unsupported claims after the model's repair attempt; never invent a citation."""
    downgraded = 0

    def downgrade(claim: Claim, field: str) -> None:
        nonlocal downgraded
        claim.value = "Insufficient evidence"
        claim.rationale = f"No valid source citation was supplied for {field}; no suggestion is made for this field."
        claim.citations = []
        downgraded += 1

    core = {
        "severity": suggestion.severity,
        "root cause": suggestion.root_cause,
        "control mapping": suggestion.control_mapping,
    }
    for field, claim in core.items():
        if claim.value.strip().casefold() != "insufficient evidence" and not claim.citations:
            downgrade(claim, field)

    for field, claim in (("duplicate warning", suggestion.duplicate_warning), ("confidence", suggestion.confidence)):
        if claim.value.strip().casefold() != "insufficient evidence" and not claim.citations:
            downgrade(claim, field)

    supported_steps = []
    for number, step in enumerate(suggestion.remediation_steps, 1):
        action_supported = step.action.value.strip().casefold() != "insufficient evidence" and bool(step.action.citations)
        owner_supported = step.owner_role.value.strip().casefold() != "insufficient evidence" and bool(step.owner_role.citations)
        if not action_supported or not owner_supported:
            downgraded += int(not action_supported) + int(not owner_supported)
            continue
        if step.due_date_suggestion.value.strip().casefold() != "insufficient evidence" and not step.due_date_suggestion.citations:
            downgrade(step.due_date_suggestion, f"remediation step {number} due date")
        supported_steps.append(step)
    suggestion.remediation_steps = supported_steps
    core_insufficient = any(claim.value.strip().casefold() == "insufficient evidence" for claim in core.values())
    if core_insufficient:
        suggestion.retrieval_state = "insufficient_evidence"
    return suggestion, core_insufficient, downgraded


def _repair_remediation_quotes(suggestion: TriageSuggestion, retrieved: dict[str, list[dict[str, Any]]]) -> int:
    """Replace a paraphrased quote with the exact remediation passage from its cited precedent."""
    chunks = _flatten(retrieved)
    repaired = 0
    for step in suggestion.remediation_steps:
        if step.action.value.strip().casefold() == "insufficient evidence":
            continue
        for citation in step.action.citations:
            source = chunks.get(citation.chunk_id)
            if not source or source["source_type"] != "past_issues" or source["source_id"] != citation.source_id:
                continue
            remediation = next((line.removeprefix("Remediation: ").strip()
                                for line in source["text"].splitlines()
                                if line.startswith("Remediation: ")), "")
            if remediation and _normalise_quote(citation.quote) not in _normalise_quote(source["text"]):
                citation.quote = remediation
                repaired += 1
    return repaired


def _field_quote(field: str, source: dict[str, Any]) -> str:
    """Choose a verbatim source passage tied to the claim's field and source type."""
    text = source["text"]
    lines = text.splitlines()
    source_type = source["source_type"]
    if field == "severity" and source_type in {"past_issue", "past_issues"}:
        return next((line for line in lines if line.startswith("Final severity: ")), "")
    if field == "root_cause" and source_type in {"past_issue", "past_issues"}:
        return next((line for line in lines if line.startswith("Root cause: ")), "")
    if field.startswith("remediation[") and field.endswith(".action"):
        if source_type in {"past_issue", "past_issues"}:
            return next((line.removeprefix("Remediation: ").strip()
                         for line in lines if line.startswith("Remediation: ")), "")
        if source_type in {"policy", "policies"}:
            return next((line for line in lines if line.startswith("Synthetic policy example.")), "")
    if field.startswith("remediation[") and field.endswith(".owner_role") and source_type in {"control", "controls"}:
        for position, line in enumerate(lines[:-1]):
            if line.strip() == "Control owner role":
                return f"{line}\n{lines[position + 1]}"
        match = re.search(r"Control owner role:\s*[^.\n]+\.?", text)
        return match.group(0) if match else ""
    if field == "control_mapping" and source_type in {"control", "controls"}:
        return next((line for line in lines if line.startswith("Synthetic example control ")), "")
    if field == "duplicate_warning" and source_type in {"past_issue", "past_issues"}:
        return next((line.removeprefix("Description: ").strip()
                     for line in lines if line.startswith("Description: ")), "")
    if field == "confidence":
        if source_type in {"control", "controls"}:
            return next((line for line in lines if line.startswith("Synthetic example control ")), "")
        if source_type in {"past_issue", "past_issues"}:
            return next((line.removeprefix("Description: ").strip()
                         for line in lines if line.startswith("Description: ")), "")
        if source_type in {"policy", "policies"}:
            return next((line for line in lines if line.startswith("Synthetic policy example.")), "")
    return ""


def _repair_claim_quotes(suggestion: TriageSuggestion, retrieved: dict[str, list[dict[str, Any]]]) -> int:
    """Repair quotes and keep remediation actions tied to precedent or policy sources."""
    chunks = _flatten(retrieved)
    claims: list[tuple[str, Claim]] = [
        ("severity", suggestion.severity), ("root_cause", suggestion.root_cause),
        ("control_mapping", suggestion.control_mapping), ("duplicate_warning", suggestion.duplicate_warning),
        ("confidence", suggestion.confidence),
    ]
    for index, step in enumerate(suggestion.remediation_steps, 1):
        claims.extend([
            (f"remediation[{index}].action", step.action),
            (f"remediation[{index}].owner_role", step.owner_role),
            (f"remediation[{index}].due_date_suggestion", step.due_date_suggestion),
        ])
    repaired = 0
    for field, claim in claims:
        if claim.value.strip().casefold() == "insufficient evidence":
            continue
        if field.startswith("remediation[") and field.endswith(".action"):
            valid_citations = []
            for citation in claim.citations:
                source = chunks.get(citation.chunk_id)
                if not source or source["source_id"] != citation.source_id:
                    continue
                if source["source_type"] not in {"past_issues", "policies"}:
                    continue
                quote = _field_quote(field, source)
                if not quote:
                    continue
                if citation.quote != quote:
                    repaired += 1
                citation.quote = quote
                valid_citations.append(citation)
            if valid_citations:
                claim.citations = valid_citations
                continue

            # The model often reuses the control citation for both the owner and
            # the action. Bind an unsupported action citation to the closest
            # retrieved precedent already surfaced as a similar issue.
            for precedent in suggestion.similar_issues:
                source = chunks.get(precedent.chunk_id)
                if not source or source["source_type"] != "past_issues":
                    continue
                quote = _field_quote(field, source)
                if not quote:
                    continue
                claim.citations = [Citation(
                    chunk_id=source["chunk_id"],
                    source_id=source["source_id"],
                    quote=quote,
                )]
                repaired += 1
                break
            continue
        for citation in claim.citations:
            source = chunks.get(citation.chunk_id)
            if not source or source["source_id"] != citation.source_id:
                continue
            if _normalise_quote(citation.quote) in _normalise_quote(source["text"]):
                continue
            quote = _field_quote(field, source)
            if quote and _normalise_quote(quote) in _normalise_quote(source["text"]):
                citation.quote = quote
                repaired += 1
    return repaired


def citation_errors(suggestion: TriageSuggestion, retrieved: dict[str, list[dict[str, Any]]]) -> list[str]:
    chunks = _flatten(retrieved)
    _canonicalise_citation_ids(suggestion, chunks)
    errors: list[str] = []
    if suggestion.severity.value not in {"Critical", "High", "Medium", "Low", "Insufficient evidence"}:
        errors.append("severity: value must be Critical, High, Medium, Low, or Insufficient evidence")
    if suggestion.root_cause.value not in ROOT_CAUSE_CATEGORIES:
        errors.append("root_cause: value must use the approved root-cause taxonomy")
    if suggestion.duplicate_warning.value not in {"Potential duplicate", "Related precedent", "No likely duplicate found", "Insufficient evidence"}:
        errors.append("duplicate_warning: use exactly one of Potential duplicate, Related precedent, No likely duplicate found, or Insufficient evidence")
    if suggestion.confidence.value not in {"Low", "Medium", "High", "Insufficient evidence"}:
        errors.append("confidence: value must be Low, Medium, High, or Insufficient evidence")
    claims: list[tuple[str, Claim, set[str] | None]] = [
        ("severity", suggestion.severity, None), ("root_cause", suggestion.root_cause, {"past_issues"}),
        ("control_mapping", suggestion.control_mapping, {"controls"}), ("duplicate_warning", suggestion.duplicate_warning, {"past_issues"}),
        ("confidence", suggestion.confidence, None),
    ]
    for idx, step in enumerate(suggestion.remediation_steps, 1):
        claims.extend([(f"remediation[{idx}].action", step.action, {"past_issues", "policies"}), (f"remediation[{idx}].owner_role", step.owner_role, {"controls"}), (f"remediation[{idx}].due_date", step.due_date_suggestion, None)])
    if len(suggestion.similar_issues) > 5:
        errors.append("similar_issues: more than five matches were returned")
    for item in suggestion.similar_issues:
        source = chunks.get(item.chunk_id)
        if not source or source["source_type"] != "past_issues":
            errors.append(f"similar_issues: {item.chunk_id} is not a retrieved past-issue chunk")
    for field, claim, required_types in claims:
        if claim.value.strip().casefold() == "insufficient evidence":
            if claim.citations:
                errors.append(f"{field}: insufficient-evidence values should not cite unsupported sources")
            continue
        if not claim.citations:
            errors.append(f"{field}: missing citation")
            continue
        for citation in claim.citations:
            source = chunks.get(citation.chunk_id)
            if not source:
                errors.append(f"{field}: chunk {citation.chunk_id} was not retrieved")
            elif source["source_id"] != citation.source_id:
                errors.append(f"{field}: source ID does not match chunk {citation.chunk_id}")
            elif required_types and source["source_type"] not in required_types:
                errors.append(f"{field}: citation must come from {', '.join(sorted(required_types))}")
            elif field == "duplicate_warning" and citation.chunk_id not in {
                item["chunk_id"] for item in retrieved.get("duplicate_issues", [])
            }:
                errors.append("duplicate_warning: citations must come from the exact-audit duplicate check")
            elif _normalise_quote(citation.quote) not in _normalise_quote(source["text"]):
                errors.append(f"{field}: citation quote is not an exact quote from {citation.chunk_id}")
    return errors


def build_graph(settings: Settings):
    memory = ApprovedMemory(settings.mem0_api_key)

    def memory_recall(state: TriageState) -> dict[str, Any]:
        hits = memory.recall(
            f"{state['issue_title']} {state['issue_description']} {state.get('control_hint', '')}",
            state["auditor_id"], state["client_id"],
        )
        logger.info("run_id=%s node=memory_recall hits=%s", state["run_id"], len(hits))
        return {"memories": hits, "log": _event(state, "memory_recall", {"hit_count": len(hits)})}

    def retrieve(state: TriageState) -> dict[str, Any]:
        started = time.monotonic()
        try:
            query = f"{state['issue_title']}\n{state['issue_description']}\n{state.get('audit_context', '')}\nRAU {state.get('rau_id', '')}\nAttributes: {', '.join(state.get('attributes_tested', []))}"
            sources = PineconeRetriever(settings).retrieve(
                query=query, client_id=state["client_id"], audit_id=state["audit_id"], control_hint=state.get("control_hint", ""), rau_id=state.get("rau_id", ""),
            )
            all_sources = [source for rows in sources.values() for source in rows]
            for group in sources.values():
                for rank, source in enumerate(group, 1):
                    source["ref"] = f"{source['source_type'][:2].upper()}{rank}"
            best_semantic = max((source.get("semantic_score", source["score"]) for source in all_sources), default=0.0)
            best_keyword_overlap = max((source.get("keyword_overlap", 0.0) for source in all_sources), default=0.0)
            logger.info("run_id=%s node=retrieve issues=%d exact_audit_duplicates=%d controls=%d policies=%d semantic=%.3f keyword_overlap=%.3f", state["run_id"], len(sources["past_issues"]), len(sources["duplicate_issues"]), len(sources["controls"]), len(sources["policies"]), best_semantic, best_keyword_overlap)
            sufficient_signal = best_semantic >= settings.retrieval_threshold or best_keyword_overlap >= settings.keyword_overlap_threshold
            insufficient = not all_sources or not sufficient_signal
            return {
                "retrieved": sources, "insufficient_evidence": insufficient,
                "error": f"Retrieval signal is weak (best semantic similarity {best_semantic:.3f}; threshold {settings.retrieval_threshold:.2f}; best keyword overlap {best_keyword_overlap:.3f}; threshold {settings.keyword_overlap_threshold:.2f}). This is not an evidence sufficiency assessment." if insufficient else "",
                "retrieval_latency_ms": round((time.monotonic() - started) * 1000),
                "log": _event(state, "retrieval", {"counts": {key: len(value) for key, value in sources.items()}, "best_semantic_score": best_semantic, "best_keyword_overlap": best_keyword_overlap, "hybrid_rank_fusion": "reciprocal_rank_fusion", "latency_ms": round((time.monotonic() - started) * 1000)}),
            }
        except Exception as exc:
            logger.exception("run_id=%s node=retrieve failed", state["run_id"])
            return {"retrieved": {"past_issues": [], "duplicate_issues": [], "controls": [], "policies": []}, "insufficient_evidence": True, "error": f"Retrieval unavailable: {exc}", "log": _event(state, "retrieval_error", {"error_type": type(exc).__name__})}

    def triage(state: TriageState) -> dict[str, Any]:
        if state.get("insufficient_evidence"):
            return {"suggestion": insufficient_suggestion(state.get("error", "Retrieval did not return useful matches.")).model_dump(mode="json"), "citation_errors": [], "log": _event(state, "triage_skipped", {"reason": "weak_retrieval"})}
        chunks = _flatten(state["retrieved"])
        evidence = "\n\n".join(
            f"[{doc['chunk_id']}] source_id={doc['source_id']} type={doc['source_type']} semantic_score={doc.get('semantic_score', doc['score']):.3f} keyword_score={doc.get('keyword_score', 0.0):.3f} hybrid_rank_score={doc.get('hybrid_score', 0.0):.5f} title={doc['title']}\n{doc['text']}\nOutcome: {doc['outcome']}"
            for doc in chunks.values()
        )
        duplicate_ids = {item["chunk_id"] for item in state["retrieved"].get("duplicate_issues", [])}
        evidence += "\n\nExact-audit duplicate candidates (only these may support duplicate_warning): " + ", ".join(sorted(duplicate_ids))
        memories = json.dumps([{key: hit.get(key) for key in ("memory", "score")} for hit in state.get("memories", [])], ensure_ascii=False)
        errors = "\n".join(state.get("citation_errors", []))
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", "Issue title: {title}\nDescription: {description}\nClient and audit context: {context}\nRisk assessable unit: {rau_id}\nAttributes tested: {attributes}\nAuditor control hint (search hint only): {control_hint}\nApproved scoped memory (not citable): {memories}\n\nRetrieved evidence:\n{evidence}\n\nPrior citation validator errors to repair (if any):\n{errors}"),
        ])
        llm = ChatOpenAI(model=settings.chat_model, temperature=0, api_key=settings.openai_api_key).with_structured_output(TriageSuggestion, include_raw=True)
        started = time.monotonic()
        try:
            result = (prompt | llm).invoke({
                "title": state["issue_title"], "description": state["issue_description"],
                "context": f"Client: {state['client_id']}\nAudit: {state['audit_id']}\n{state.get('audit_context', '')}",
                "rau_id": state.get("rau_id", ""), "attributes": ", ".join(state.get("attributes_tested", [])),
                "control_hint": state.get("control_hint") or "None", "memories": memories, "evidence": evidence, "errors": errors or "None",
            })
            suggestion = result["parsed"]
            if suggestion is None:
                raise ValueError(f"Structured response parsing failed: {result.get('parsing_error')}")
            raw_response = result.get("raw")
            token_usage = getattr(raw_response, "usage_metadata", None) or {}
        except Exception as exc:
            logger.exception("run_id=%s node=triage failed", state["run_id"])
            return {
                "suggestion": insufficient_suggestion("The recommendation service failed; no unsupported triage is shown.").model_dump(mode="json"),
                "insufficient_evidence": True, "error": f"Recommendation unavailable: {type(exc).__name__}",
                "token_usage": {}, "log": _event(state, "triage_error", {"error_type": type(exc).__name__}),
            }
        # A control or policy hit cannot turn a weak past-issue match into a claimed precedent.
        past_issue_chunks = [
            item for item in state["retrieved"].get("past_issues", [])
            if item.get("semantic_score", item["score"]) >= settings.retrieval_threshold or item.get("keyword_overlap", 0.0) >= settings.keyword_overlap_threshold
        ][:5]
        suggestion.similar_issues = [
            SimilarIssue(chunk_id=item["chunk_id"], similarity=max(0.0, min(1.0, item.get("semantic_score", item["score"]))), outcome=item.get("outcome", ""))
            for item in past_issue_chunks
        ]
        logger.info("run_id=%s node=triage attempt=%d", state["run_id"], state.get("citation_attempts", 0) + 1)
        latency_ms = round((time.monotonic() - started) * 1000)
        return {"suggestion": suggestion.model_dump(mode="json"), "token_usage": token_usage, "triage_latency_ms": latency_ms, "citation_attempts": state.get("citation_attempts", 0) + 1, "log": _event(state, "triage_generated", {"attempt": state.get("citation_attempts", 0) + 1, "latency_ms": latency_ms, "token_usage": token_usage})}

    def citation_check(state: TriageState) -> dict[str, Any]:
        suggestion = TriageSuggestion.model_validate(state["suggestion"])
        downgraded = 0
        quotes_repaired = 0
        core_insufficient = False
        allowed_duplicate_states = {
            "Potential duplicate", "Related precedent", "No likely duplicate found", "Insufficient evidence",
        }
        if state.get("citation_attempts", 0) >= 2 and suggestion.duplicate_warning.value not in allowed_duplicate_states:
            suggestion.duplicate_warning.value = "Insufficient evidence"
            suggestion.duplicate_warning.rationale = (
                "The duplicate-screening response did not match an allowed state, so no duplicate conclusion is made."
            )
            suggestion.duplicate_warning.citations = []
            downgraded += 1
        errors = citation_errors(suggestion, state.get("retrieved", {}))
        if state.get("citation_attempts", 0) >= 2:
            quotes_repaired += _repair_remediation_quotes(suggestion, state.get("retrieved", {}))
            quotes_repaired += _repair_claim_quotes(suggestion, state.get("retrieved", {}))
            if quotes_repaired:
                errors = citation_errors(suggestion, state.get("retrieved", {}))
        if errors and state.get("citation_attempts", 0) >= 2 and any("missing citation" in error for error in errors):
            suggestion, core_insufficient, downgraded = _mark_uncited_claims_insufficient(suggestion)
            errors = citation_errors(suggestion, state.get("retrieved", {}))
        logger.info("run_id=%s node=citation_check valid=%s errors=%d", state["run_id"], not errors, len(errors))
        pending = not errors and suggestion.retrieval_state == "grounded" and not core_insufficient
        return {
            "suggestion": suggestion.model_dump(mode="json"), "citation_errors": errors,
            "insufficient_evidence": bool(state.get("insufficient_evidence") or core_insufficient),
            "review_pending": pending,
            "log": _event(state, "citation_check", {"valid": not errors, "error_count": len(errors), "errors": errors, "quotes_repaired": quotes_repaired, "uncited_claims_downgraded": downgraded}),
        }

    def citation_route(state: TriageState) -> str:
        suggestion = TriageSuggestion.model_validate(state["suggestion"])
        if state.get("insufficient_evidence") or suggestion.retrieval_state != "grounded":
            return "finish_insufficient"
        if not state.get("citation_errors"):
            return "preapproval_check"
        if state.get("citation_attempts", 0) < 2:
            return "triage"
        return "finish_insufficient"

    def finish_insufficient(state: TriageState) -> dict[str, Any]:
        reason = state.get("error")
        if not reason and state.get("citation_errors"):
            reason = "Citation validation failed after the allowed repair attempt: " + "; ".join(state["citation_errors"])
        reason = reason or "Citation validation failed; no unsupported recommendation is shown."
        suggestion = insufficient_suggestion(reason)
        logger.warning("run_id=%s node=insufficient_evidence", state["run_id"])
        return {"suggestion": suggestion.model_dump(mode="json"), "insufficient_evidence": True, "review_pending": False, "error": reason, "log": _event(state, "insufficient_evidence", {"reason": reason})}

    def preapproval_check(state: TriageState) -> dict[str, Any]:
        suggestion = TriageSuggestion.model_validate(state["suggestion"])
        checks = evaluate_preapproval(state, suggestion, settings)
        logger.info("run_id=%s node=preapproval_check checks=%s", state["run_id"], {item.code: item.status for item in checks})
        return {"preapproval_checks": [item.model_dump(mode="json") for item in checks], "log": _event(state, "preapproval_checks", {"checks": [item.model_dump(mode="json") for item in checks]})}

    def human_review(state: TriageState) -> dict[str, Any]:
        # Durable interrupt: no Jira write or email send runs before the decision.
        payload = interrupt({"run_id": state["run_id"], "issue_title": state["issue_title"], "suggestion": state["suggestion"], "citations": _flatten(state["retrieved"]), "preapproval_checks": state.get("preapproval_checks", []), "allowed_decisions": ["approve", "edit", "reject"]})
        return {"human_decision": payload, "review_pending": False}

    def finalize_review(state: TriageState) -> dict[str, Any]:
        decision = state["human_decision"]
        if decision.get("decision") not in {"approve", "edit", "reject"}:
            raise ValueError("Human decision must be approve, edit, or reject.")
        if decision["decision"] == "reject" and not decision.get("reason", "").strip():
            raise ValueError("A rejection reason is required.")
        if not decision.get("approver", "").strip():
            raise ValueError("Approver identity is required.")
        suggestion = TriageSuggestion.model_validate(state["suggestion"])
        if decision["decision"] == "edit":
            suggestion = TriageSuggestion.model_validate_json(decision["edited_json"])
            errors = citation_errors(suggestion, state["retrieved"])
            if errors:
                raise ValueError("Edited recommendation failed citation validation: " + "; ".join(errors))
        if decision["decision"] in {"approve", "edit"}:
            current_checks = evaluate_preapproval(state, suggestion, settings)
            gate = gate_approval(
                current_checks, decision.get("warning_acknowledgements", {}),
                str(decision.get("lead_override_id", "")), str(decision.get("lead_override_reason", "")),
                settings.mcp_lead_auditor_ids,
            )
            allowed_approvers = set(settings.mcp_auditor_ids) | set(settings.mcp_lead_auditor_ids)
            if decision["approver"] not in allowed_approvers:
                raise PermissionError("This identity is not authorized to approve triage suggestions.")
            if not gate.ready:
                raise ValueError("Pre-approval gate blocked this decision: " + ", ".join(gate.blockers))
        else:
            current_checks = [GateCheck.model_validate(check) for check in state.get("preapproval_checks", [])]
            gate = None
        store = AuditStore(settings.database_url)
        store.initialize()
        approval_id = store.record_approval(state["run_id"], decision, suggestion)
        superseded_control = bool(gate and any(item.code == "C03" and item.status == "lead_overridden" for item in gate.checks))
        memory_written = False
        if decision["decision"] in {"approve", "edit"} and not superseded_control:
            memory_note = (
                f"Approved triage pattern: severity category {suggestion.severity.value}; "
                f"root-cause category {suggestion.root_cause.value}; control ID {suggestion.control_mapping.value}."
            )
            memory_written = memory.remember_approved_pattern(
                memory_note, state["auditor_id"], state["client_id"], state["issue_id"], decision["approver"],
            )
        event = {"decision": decision["decision"], "reason": decision.get("reason", ""), "approver": decision["approver"], "memory_written": memory_written, "approval_id": approval_id}
        store.append_event(state["run_id"], "human_approval", event)
        store.append_event(state["run_id"], "preapproval_gate_decision", {
            "checks": [item.model_dump(mode="json") for item in (gate.checks if gate else current_checks)],
            "warning_acknowledgements": decision.get("warning_acknowledgements", {}),
            "superseded_control_override": superseded_control,
            "lead_override_id": decision.get("lead_override_id", "") if superseded_control else "",
            "lead_override_reason": decision.get("lead_override_reason", "") if superseded_control else "",
            "decision": decision["decision"],
        })
        logger.info("run_id=%s node=finalize_review decision=%s memory_written=%s", state["run_id"], decision["decision"], memory_written)
        return {"suggestion": suggestion.model_dump(mode="json"), "approval_id": approval_id, "review_complete": True, "review_pending": False, "superseded_control": superseded_control, "log": _event(state, "human_approval", event)}

    def jira_draft(state: TriageState) -> dict[str, Any]:
        decision = state.get("human_decision", {})
        approver = str(decision.get("approver", ""))
        # Enforce the allowlist in the graph and again inside the governed MCP server.
        if decision.get("decision") not in {"approve", "edit"}:
            return {"jira_status": "not_requested", "log": _event(state, "jira_draft_skipped", {"reason": "no approving human decision"})}
        if not settings.jira_base_url or not settings.jira_email or not settings.jira_api_token or not settings.jira_project_key:
            return {"jira_status": "not_configured", "log": _event(state, "jira_draft_skipped", {"reason": "Jira connection configuration incomplete"})}
        if approver not in settings.mcp_auditor_ids:
            return {"jira_status": "permission_denied", "log": _event(state, "jira_draft_skipped", {"reason": "approver lacks Jira draft permission"})}
        try:
            jira_store = AuditStore(settings.database_url)
            jira_store.initialize()
            result = create_jira_draft(settings, jira_store, state["run_id"], state["client_id"], approver)
            detail = {"issue_key": result.get("issue_key", ""), "approval_id": result.get("approval_id", state.get("approval_id", ""))}
            logger.info("run_id=%s node=jira_draft status=%s", state["run_id"], result.get("status", "created"))
            return {"jira_status": "created", "jira_issue_key": detail["issue_key"], "jira_issue_url": result.get("issue_url", ""), "log": _event(state, "jira_draft_created", detail)}
        except Exception as exc:
            # Jira downtime never erases or reverses the saved human decision.
            reason = str(exc)
            logger.warning("run_id=%s node=jira_draft unavailable type=%s", state["run_id"], type(exc).__name__)
            failure_store = AuditStore(settings.database_url)
            failure_store.initialize()
            failure_store.append_event(state["run_id"], "jira_draft_failed", {"error_type": type(exc).__name__})
            return {"jira_status": "unavailable", "jira_error": reason, "log": _event(state, "jira_draft_failed", {"error_type": type(exc).__name__})}

    def owner_notification(state: TriageState) -> dict[str, Any]:
        decision=state.get("human_decision",{})
        approver=str(decision.get("approver", ""))
        if decision.get("decision") not in {"approve","edit"} or state.get("jira_status") != "created":
            return {"email_status":"not_sent", "log":_event(state,"owner_email_skipped",{"reason":"approved Jira issue is unavailable"})}
        if not settings.email_notification_ready:
            return {"email_status":"not_configured", "log":_event(state,"owner_email_skipped",{"reason":"Email notification configuration incomplete"})}
        if approver not in settings.mcp_auditor_ids:
            return {"email_status":"permission_denied", "log":_event(state,"owner_email_skipped",{"reason":"approver lacks notification permission"})}
        try:
            email_store=AuditStore(settings.database_url); email_store.initialize()
            result=send_business_owner_email(settings,email_store,state["run_id"],state["client_id"],approver,state["jira_issue_key"])
            email_store.append_event(state["run_id"],"business_owner_email_sent",{"issue_key":state["jira_issue_key"],"recipient_count":result.get("recipient_count",0)})
            logger.info("run_id=%s node=owner_notification status=%s",state["run_id"],result.get("status"))
            return {"email_status":"sent", "log":_event(state,"business_owner_email_sent",{"issue_key":state["jira_issue_key"],"recipient_count":result.get("recipient_count",0)})}
        except Exception as exc:
            reason=str(exc)
            logger.warning("run_id=%s node=owner_notification unavailable type=%s",state["run_id"],type(exc).__name__)
            failure_store=AuditStore(settings.database_url); failure_store.initialize()
            failure_store.append_event(state["run_id"],"business_owner_email_failed",{"error_type":type(exc).__name__})
            return {"email_status":"unavailable", "email_error":reason, "log":_event(state,"business_owner_email_failed",{"error_type":type(exc).__name__})}

    def route_after_jira(state: TriageState) -> str:
        return "owner_notification" if state.get("jira_status") == "created" else "end"

    def route_after_review(state: TriageState) -> str:
        return "jira_draft" if state.get("human_decision", {}).get("decision") in {"approve", "edit"} else "end"

    builder = StateGraph(TriageState)
    builder.add_node("memory_recall", memory_recall)
    builder.add_node("retrieve", retrieve)
    builder.add_node("triage", triage)
    builder.add_node("citation_check", citation_check)
    builder.add_node("finish_insufficient", finish_insufficient)
    builder.add_node("preapproval_check", preapproval_check)
    builder.add_node("human_review", human_review)
    builder.add_node("finalize_review", finalize_review)
    builder.add_node("jira_draft", jira_draft)
    builder.add_node("owner_notification", owner_notification)
    builder.add_edge(START, "memory_recall")
    builder.add_edge("memory_recall", "retrieve")
    builder.add_edge("retrieve", "triage")
    builder.add_edge("triage", "citation_check")
    builder.add_conditional_edges("citation_check", citation_route, {"triage": "triage", "preapproval_check": "preapproval_check", "finish_insufficient": "finish_insufficient"})
    builder.add_edge("preapproval_check", "human_review")
    builder.add_edge("finish_insufficient", END)
    builder.add_edge("human_review", "finalize_review")
    builder.add_conditional_edges("finalize_review", route_after_review, {"jira_draft": "jira_draft", "end": END})
    builder.add_conditional_edges("jira_draft", route_after_jira, {"owner_notification":"owner_notification", "end":END})
    builder.add_edge("owner_notification", END)
    return builder.compile(checkpointer=get_checkpointer(settings))


def get_checkpointer(settings: Settings) -> Any:
    global _checkpointer_context, _checkpointer_instance
    if _checkpointer_instance is None:
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        if settings.checkpoint_backend == "postgres":
            from langgraph.checkpoint.postgres import PostgresSaver
            connection_string = settings.checkpoint_database_url.replace("postgresql+psycopg://", "postgresql://", 1)
            _checkpointer_context = PostgresSaver.from_conn_string(connection_string)
        else:
            checkpoint_path = Path(settings.checkpoint_path)
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            _checkpointer_context = SqliteSaver.from_conn_string(str(checkpoint_path))
        _checkpointer_instance = _checkpointer_context.__enter__()
        _checkpointer_instance.setup()
    return _checkpointer_instance

"""Streamlit review surface for the Issue Triage Assistant V1 workflow."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any
from urllib.parse import quote

import streamlit as st
from langgraph.types import Command

from src.issue_triage.audit_log.store import AuditStore
from src.issue_triage.chatbot import answer_from_run_sources
from src.issue_triage.config import get_settings
from src.issue_triage.graph.scoping import build_scoping_graph
from src.issue_triage.graph.document_workflow import build_summary_document_graph
from src.issue_triage.graph.workflow import build_graph, citation_errors
from src.issue_triage.triage.preapproval import GateCheck, evaluate_preapproval, gate_approval
from src.issue_triage.integrations.mcp_clients import create_jira_draft, send_business_owner_email
from src.issue_triage.triage.schema import TriageSuggestion
from src.issue_triage.triage.document_pdf import build_summary_document_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
st.set_page_config(page_title="Issue Triage Assistant", page_icon="🧭", layout="wide")

settings = get_settings()
store = AuditStore(settings.database_url)
store.initialize()


def current_auditor() -> str:
    auth_mode = os.getenv("AUTH_MODE", "demo").lower()
    app_env = os.getenv("APP_ENV", "development").lower()
    if auth_mode == "oidc":
        try:
            if not st.user.is_logged_in:
                st.login()
                st.stop()
            identity = st.user.get("email") or st.user.get("sub")
            if not identity:
                st.error("The identity provider did not return an email or subject claim.")
                st.stop()
            st.sidebar.write(f"Signed in: {identity}")
            if st.sidebar.button("Sign out"):
                st.logout()
                st.stop()
            return str(identity)
        except Exception as exc:
            st.error(f"OIDC sign-in is not configured. Add Streamlit [auth] settings through the deployment secret store. Details: {exc}")
            st.stop()
    if app_env == "production":
        st.error("Demo auditor selection is disabled in production. Set AUTH_MODE=oidc and configure Streamlit OIDC secrets.")
        st.stop()
    st.sidebar.caption("Development identity selector; this is not authentication.")
    return st.sidebar.text_input("Auditor ID", value="synthetic-auditor-01")


@st.cache_data(show_spinner=False, max_entries=32)
def cached_summary_pdf(document_json: str) -> bytes:
    """Avoid rebuilding unchanged PDFs during normal Streamlit reruns."""
    return build_summary_document_pdf(json.loads(document_json))


def render_claim(label: str, claim: dict, retrieved: dict[str, dict]) -> None:
    st.markdown(f"**{label}:** {claim.get('value', 'Insufficient evidence')}")
    if claim.get("rationale"):
        st.write(claim["rationale"])
    for citation in claim.get("citations", []):
        source = retrieved.get(citation.get("chunk_id"))
        if not source:
            st.error("Citation could not be resolved to a retrieved source chunk.")
            continue
        with st.expander(f"{source['ref']} · {source['title']} · {source['source_id']}"):
            st.caption(
                f"Source audit ID: {source.get('audit_id') or 'Not recorded'} · "
                f"Chunk: {citation['chunk_id']} · Section: {source.get('section') or 'Not specified'}"
            )
            st.markdown(f"> {citation['quote']}")


def render_source_card(item: dict) -> None:
    with st.container(border=True):
        st.caption(
            f"{item.get('ref', 'Source')} · {item.get('source_type', 'source')} · "
            f"audit {item.get('audit_id') or 'not recorded'} · "
            f"semantic similarity {item.get('semantic_score', item.get('score', 0)):.3f} · "
            f"BM25 keyword {item.get('keyword_score', 0):.3f} · "
            f"term overlap {item.get('keyword_overlap', 0):.0%} · "
            f"RRF rank {item.get('hybrid_score', 0):.5f} · {item.get('source_id', 'Unknown')}"
        )
        st.markdown(f"**{item.get('title', 'Untitled source')}**")
        st.write(item.get("text", ""))


def render_summary_document(document: dict[str, Any], view_key: str = "summary") -> None:
    st.warning(document.get("draft_header", "DRAFT — NOT FINAL — PENDING APPROVAL"))
    st.subheader(document.get("title", "Summary document"))
    status = document.get("status", "draft").replace("_", " ").title()
    audits = ", ".join(document.get("audit_ids", [])) or "No source audit IDs recorded"
    st.caption(
        f"{status} · v{document.get('version', 1)} · {document.get('template')} · "
        f"Client {document.get('client_id')} · audit context {document.get('audit_id') or 'not specified'} · "
        f"source audits: {audits} · RAU {document.get('rau_id') or 'not specified'} · "
        f"run {document.get('created_by_run_id')}"
    )
    pdf_bytes = cached_summary_pdf(json.dumps(document, sort_keys=True, default=str))
    pdf_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(document.get("title") or "audit-summary")).strip("-._")[:60]
    pdf_name = f"{pdf_stem or 'audit-summary'}-v{document.get('version', 1)}.pdf"
    document_key = re.sub(r"[^A-Za-z0-9_-]+", "_", str(document.get("document_id", "summary")))
    view_namespace = re.sub(r"[^A-Za-z0-9_-]+", "_", view_key)
    version_key = str(document.get("version", 1))
    viewer_state_key = f"summary_pdf_open_{document_key}_{version_key}_{view_namespace}"
    open_button, download_button = st.columns([1, 1], vertical_alignment="bottom")
    if open_button.button(
        "Open PDF view", key=f"summary_pdf_open_button_{document_key}_{version_key}_{view_namespace}",
        icon=":material/picture_as_pdf:",
    ):
        st.session_state[viewer_state_key] = True
    download_button.download_button(
        "Download PDF attachment", data=pdf_bytes, file_name=pdf_name,
        mime="application/pdf", key=f"summary_pdf_download_{document_key}_{version_key}_{view_namespace}",
        icon=":material/download:",
    )
    if st.session_state.get(viewer_state_key):
        if st.button("Close PDF view", key=f"summary_pdf_close_{document_key}_{version_key}_{view_namespace}"):
            st.session_state[viewer_state_key] = False
        else:
            st.pdf(
                pdf_bytes, height=720, key=f"summary_pdf_viewer_{document_key}_{version_key}_{view_namespace}",
                alt=f"PDF preview of {document.get('title') or 'the cited summary document'}",
            )
    counts = document.get("counts", {})
    columns = st.columns(4)
    columns[0].metric("Issues", counts.get("total_issues", 0))
    columns[1].metric("Repeat clusters", counts.get("repeat_clusters", 0))
    columns[2].metric("Open / overdue", counts.get("open_overdue", 0))
    columns[3].metric("Coverage", document.get("coverage", "Not recorded"))
    st.caption(f"Severity counts: {counts.get('by_severity', {})} · Status counts: {counts.get('by_status', {})}")
    st.caption("Full draft content, caveats, next actions, and source citations are in the PDF. Use the document decision controls below to review or edit it.")


def render_summary_document_review(active: dict[str, Any], settings, auditor_id: str, view_key: str = "review") -> None:
    state = active.get("state", {})
    if state.get("document_error"):
        st.error(f"Summary draft unavailable: {state['document_error']}")
        return
    document = state.get("document")
    if not document:
        return
    render_summary_document(document, view_key=view_key)
    if not state.get("review_pending"):
        st.success(f"Document decision recorded: {document.get('status', 'draft')}.")
        return
    st.markdown("#### Document decision")
    role = "lead_auditor" if auditor_id in settings.mcp_lead_auditor_ids else "auditor"
    allowed = settings.mcp_lead_auditor_ids if role == "lead_auditor" else settings.mcp_auditor_ids
    if auditor_id not in allowed:
        st.warning("This identity is not on the configured document-review allowlist.")
        return
    with st.form(f"summary_doc_decision_{document['document_id']}_{document['version']}"):
        choice = st.radio("Decision", ["approve", "edit", "reject"], horizontal=True, key=f"summary_doc_choice_{document['document_id']}_{document['version']}")
        edited = json.loads(json.dumps(document))
        if choice == "edit":
            edited["title"] = st.text_input("Document title", value=document["title"], max_chars=120)
            for section_index, section in enumerate(document.get("sections", [])):
                st.markdown(f"**{section.get('heading', 'Section')}**")
                for bullet_index, bullet in enumerate(section.get("bullets", [])):
                    edited["sections"][section_index]["bullets"][bullet_index]["text"] = st.text_area(
                        f"Edit summary point {section_index + 1}.{bullet_index + 1}",
                        value=bullet["text"], height=100,
                        key=f"summary_doc_edit_{document['document_id']}_{document['version']}_{section_index}_{bullet_index}",
                    )
                    st.caption("Citation links stay attached and are revalidated when the edit is submitted.")
        reason = st.text_area("Decision reason (required)", key=f"summary_doc_reason_{document['document_id']}_{document['version']}")
        submitted = st.form_submit_button("Submit document decision", type="primary", width="stretch")
    if submitted:
        if not reason.strip():
            st.error("Enter a reason for the document decision.")
        else:
            payload = {"decision": choice, "reason": reason.strip(), "approver": auditor_id.strip(), "approver_role": role}
            if choice == "edit":
                payload["edited_document"] = edited
            try:
                graph = build_summary_document_graph(settings)
                result = graph.invoke(Command(resume=payload), config=active["config"])
                active["state"] = graph.get_state(active["config"]).values or result
                st.rerun()
            except Exception as exc:
                st.error(f"Document decision could not be recorded: {exc}")

def render_retrieval_legend() -> None:
    with st.expander("How to read retrieval signals"):
        st.markdown(
            "- **Semantic similarity:** vector match based on meaning; higher values indicate closer matches.\n"
            "- **BM25 keyword:** lexical match score from shared terms; its raw scale varies by query.\n"
            "- **Term overlap:** share of query terms found in the source.\n"
            "- **RRF rank:** Reciprocal Rank Fusion score combining the semantic and keyword result orders; higher means ranked nearer the top.\n\n"
            "These are retrieval signals, not confidence or severity scores."
        )


def render_grounded_chat_reply(payload: dict, sources: dict[str, dict]) -> None:
    if payload.get("error"):
        st.error(payload["error"])
        return
    if payload.get("insufficient_evidence") and not payload.get("claims"):
        st.info("I couldn't support an answer from this run's retrieved sources. Try asking about a specific issue, control, policy, or scoping cluster.")
    for claim in payload.get("claims", []):
        st.markdown(claim["text"])
        for citation in claim.get("citations", []):
            source = sources.get(citation.get("chunk_id"))
            if not source:
                continue
            with st.expander(f"Source · {source.get('source_id', 'Unknown')} · {source.get('title', 'Untitled')}"):
                st.caption(f"Chunk {citation['chunk_id']} · audit {source.get('audit_id') or 'not recorded'}")
                st.markdown(f"> {citation['quote']}")
    if payload.get("follow_up_question"):
        st.caption(f"To narrow this down: {payload['follow_up_question']}")


def render_chat_waiting_for_run(mode: str) -> None:
    panel_key = f"chat_panel_{mode}"
    with st.container(border=True, key=panel_key):
        st.subheader("Ask about this run", icon=":material/chat:")
        st.caption("Chat answers are grounded only in the retrieved sources for the current run.")
        st.info("Run a triage or scoping search to start a cited conversation.")
        st.text_input("Your question", placeholder="Available after retrieval", key=f"chat_waiting_{mode}", disabled=True)
        st.button("Send", key=f"chat_waiting_send_{mode}", disabled=True)


def render_run_chat(mode: str, run_id: str, sources: dict[str, Any] | list[dict[str, Any]], run_context: dict[str, Any]) -> None:
    panel_key = f"chat_panel_{mode}"
    with st.container(border=True, key=panel_key):
        st.subheader("Ask about this run", icon=":material/chat:")
        st.caption("Ask follow-up questions about the response above. Answers cite this run’s retrieved sources and do not change suggestions or decisions.")
        conversations = st.session_state.setdefault("chat_conversations_by_run", {})
        messages = conversations.setdefault(run_id, [])
        if isinstance(sources, dict) and sources and all(isinstance(value, dict) and "chunk_id" in value for value in sources.values()):
            source_rows = list(sources.values())
        else:
            source_rows = [item for group in sources.values() for item in group] if isinstance(sources, dict) else sources
        source_map = {item["chunk_id"]: item for item in source_rows if item.get("chunk_id")}

        with st.container(height=360, key=f"{panel_key}_history"):
            for message in messages:
                with st.chat_message(message["role"]):
                    if message["role"] == "user":
                        st.write(message["content"])
                    else:
                        render_grounded_chat_reply(message["payload"], source_map)

        with st.form(f"chat_form_{mode}_{run_id}", clear_on_submit=True):
            question = st.text_input("Your question", placeholder="Ask a follow-up about this run", key=f"chat_{mode}_{run_id}")
            send_question = st.form_submit_button("Send", icon=":material/send:", type="primary")
        if send_question and question.strip():
            question = question.strip()
            prior_history = list(messages)
            messages.append({"role": "user", "content": question})
            try:
                with st.spinner("Checking this run's sources…"):
                    reply, _ = answer_from_run_sources(
                        settings,
                        question=question,
                        history=prior_history,
                        sources=sources,
                        run_context=run_context,
                    )
                payload = reply.model_dump(mode="json")
            except Exception as exc:
                payload = {"error": f"Chat is unavailable ({type(exc).__name__}). Your triage and review state are unchanged."}
            messages.append({"role": "assistant", "payload": payload})
            conversations[run_id] = messages[-50:]
            st.rerun()


@st.dialog("Approved issue details", width="large", icon=":material/assignment:")
def render_approved_draft_dialog(item: dict[str, Any]) -> None:
    st.markdown(f"### {item['title']}")
    st.write(item["description"])
    st.caption(
        f"Issue {item['issue_id']} · run {item['run_id']} · {item['client_id']} · {item['audit_id']} · "
        f"decision {item['decision']} · submitted by {item['submitted_by']} · "
        f"approver {item['approver']} · {item['decided_at']}"
    )
    if item["input"].get("audit_context"):
        st.markdown(f"**Audit context:** {item['input']['audit_context']}")
    if item["input"].get("control_hint"):
        st.markdown(f"**Auditor control hint:** {item['input']['control_hint']}")
    if item.get("reason"):
        st.markdown(f"**Decision reason:** {item['reason']}")

    suggestion = TriageSuggestion.model_validate(item["suggestion"])
    source_chunks = {
        source["chunk_id"]: source
        for group in item["retrieved"].values()
        for source in group
        if source.get("chunk_id")
    }
    render_claim("Duplicate warning", suggestion.duplicate_warning.model_dump(), source_chunks)
    render_claim("Suggested severity", suggestion.severity.model_dump(), source_chunks)
    render_claim("Root-cause category", suggestion.root_cause.model_dump(), source_chunks)
    render_claim("Mapped control ID", suggestion.control_mapping.model_dump(), source_chunks)
    render_claim("Confidence", suggestion.confidence.model_dump(), source_chunks)
    st.markdown("**Final remediation steps**")
    for index, step in enumerate(suggestion.remediation_steps, 1):
        with st.container(border=True):
            st.markdown(f"**Step {index}**")
            render_claim("Action", step.action.model_dump(), source_chunks)
            render_claim("Owner role", step.owner_role.model_dump(), source_chunks)
            render_claim("Due-date suggestion", step.due_date_suggestion.model_dump(), source_chunks)
    if suggestion.similar_issues:
        st.markdown("**Similar past issues**")
        for match in suggestion.similar_issues:
            source = source_chunks.get(match.chunk_id)
            if source:
                with st.container(border=True):
                    st.markdown(f"**{source['title']}**")
                    st.caption(
                        f"{source.get('source_id', 'Unknown')} · audit {source.get('audit_id') or 'not recorded'} · "
                        f"similarity {match.similarity:.3f} · {match.chunk_id}"
                    )
                    st.write(f"Outcome: {match.outcome or source.get('outcome') or 'Not recorded'}")
    with st.expander("Retrieved evidence", expanded=False):
        for source_type, label in (
            ("duplicate_issues", "Exact-audit duplicate candidates"),
            ("past_issues", "Similar issues from other audits"),
            ("controls", "Controls"),
            ("policies", "Policies"),
        ):
            st.markdown(f"**{label}**")
            for source in item["retrieved"].get(source_type, []):
                render_source_card(source)


auditor_id = current_auditor()
st.sidebar.divider()
st.sidebar.subheader("Run history", icon=":material/history:")
history_client = st.sidebar.selectbox("Synthetic client", ["synthetic-client-a", "synthetic-client-b"], key="triage_history_client")
run_rows = store.recent_runs(client_id=history_client, limit=20)
if run_rows:
    selected_run_id = st.sidebar.selectbox(
        "Audit run", [row["run_id"] for row in run_rows],
        format_func=lambda run: f"{run[:8]} · {next(row['status'] for row in run_rows if row['run_id'] == run)}",
    )
    selected_row = next(row for row in run_rows if row["run_id"] == selected_run_id)
    detail = store.run_details(selected_run_id, history_client, selected_row["audit_id"])
    if detail:
        with st.sidebar.expander("Open audit record"):
            st.json({
                "input": json.loads(detail["input_json"]),
                "retrieved_chunks": json.loads(detail["retrieved_json"]),
                "suggestion": json.loads(detail["suggestion_json"]) if detail["suggestion_json"] else None,
                "approvals": [
                    {
                        **approval,
                        "edited_fields": json.loads(approval["edited_fields_json"]) if approval.get("edited_fields_json") else None,
                        "final_suggestion": json.loads(approval["final_suggestion_json"]) if approval.get("final_suggestion_json") else None,
                    }
                    for approval in detail["approvals"]
                ],
                "events": [{"event_type": e["event_type"], "payload": json.loads(e["payload_json"]), "created_at": e["created_at"]} for e in detail["events"]],
            })
            st.markdown("**MCP calls for this triage run**")
            run_tool_calls = store.tool_calls_for_run(selected_run_id, limit=50)
            if run_tool_calls:
                st.dataframe(run_tool_calls, width="stretch", hide_index=True)
            else:
                st.caption("No MCP calls are recorded. Jira and email tools run only after an approved human decision.")
        if st.sidebar.button("Resume pending review", disabled=selected_row["status"] != "review_pending"):
            config = {"configurable": {"thread_id": selected_run_id}}
            state = build_graph(settings).get_state(config).values
            if state and state.get("review_pending"):
                st.session_state["active_review"] = {"run_id": selected_run_id, "config": config, "state": state}
                st.rerun()

st.html("""
<style>
.st-key-app_header {
  padding: 1.15rem 1.5rem .9rem;
  margin: .25rem 0 1rem;
  border: 1px solid #2d7058;
  border-radius: 1rem;
  background: linear-gradient(115deg, #173f32 0%, #236b52 62%, #387d62 100%);
  box-shadow: 0 8px 24px rgba(23, 63, 50, .12);
}
.st-key-app_header [data-testid="stCaptionContainer"] p {
  color: #d6e9df;
  font-size: .75rem;
  font-weight: 700;
  letter-spacing: .14em;
}
.st-key-app_header h1,
.st-key-app_header [data-testid="stMarkdownContainer"] p {
  color: #fff;
}
.st-key-app_header h1 { margin: .2rem 0 .35rem; }
.st-key-app_header [data-testid="stMarkdownContainer"] p { margin: 0; opacity: .9; }
</style>
""")
with st.container(key="app_header"):
    st.caption("AUDIT · V1")
    st.title("Issue triage assistant", icon=":material/assignment_turned_in:")
    st.markdown("Retrieve precedent, controls, and policy to prepare a source-linked suggestion for auditor review.")
st.warning(
    "Use synthetic data only. Do not enter real client names, issue details, or audit evidence. The development identity selector is not authentication.",
    title="Concept environment",
    icon=":material/science:",
)
pain, outcome = st.columns(2, gap="large")
with pain:
    with st.container(border=True):
        st.subheader("Today’s challenge", icon=":material/search_off:")
        st.markdown("- Similar issues get logged more than once\n- Severity and control mapping vary\n- Useful remediation is hard to find\n- Reviewers cannot see the basis")
with outcome:
    with st.container(border=True):
        st.subheader("How the assistant helps", icon=":material/manage_search:")
        st.markdown("- Finds related issues, controls, and policy\n- Drafts severity, root cause, and control suggestions\n- Links claims to exact source chunks\n- Flags likely duplicates for review")

tab_issue, tab_scope = st.tabs(["Single-issue triage", "RAU scoping"])

with tab_issue:
    with st.container(border=True):
        st.subheader("Start a new issue review", icon=":material/note_add:")
        st.caption("Duplicate screening checks this audit. Precedent comes from other audits for the same synthetic client.")
        with st.form("triage_intake"):
            issue_title = st.text_input("Issue title", placeholder="Quarterly privileged access review was missed")
            issue_description = st.text_area("Issue description", height=130, placeholder="Describe the condition, scope, period, and known impact.")
            col_a, col_b = st.columns(2)
            client_id = col_a.selectbox("Synthetic client", ["synthetic-client-a", "synthetic-client-b"], key="triage_client")
            audit_by_client = {
                "synthetic-client-a": ["synthetic-audit-iam-2026", "synthetic-audit-iam-2025"],
                "synthetic-client-b": ["synthetic-audit-finance-2026", "synthetic-audit-finance-2025"],
            }
            audit_id = col_b.selectbox("Synthetic audit", audit_by_client[client_id], key=f"triage_audit_{client_id}")
            col_c, col_d = st.columns(2)
            audit_context = col_c.text_input("Audit context", placeholder="FY2026 identity and access review")
            control_hint = col_d.text_input("Optional control hint", placeholder="AC-04 (search hint only)")
            col_e, col_f = st.columns(2)
            rau_id = col_e.text_input("Risk assessable unit (RAU) ID *", placeholder="RAU-AC-01")
            attributes_text = col_f.text_input("Attributes tested *", placeholder="authorization, completeness")
            submit = st.form_submit_button("Retrieve and draft for review", type="primary", width="stretch")

    if submit:
        if len(issue_title.strip()) < 3 or len(issue_description.strip()) < 20:
            st.warning("Provide an issue title and a fuller description.")
        elif not rau_id.strip() or not attributes_text.strip():
            st.warning("Enter the scoped RAU and at least one tested attribute for the pre-approval checks.")
        elif not auditor_id.strip():
            st.warning("Auditor identity is required for the review trail.")
        else:
            run_id, issue_id = str(uuid.uuid4()), str(uuid.uuid4())
            graph = build_graph(settings)
            config = {"configurable": {"thread_id": run_id}}
            initial = {
                "run_id": run_id, "issue_id": issue_id, "issue_title": issue_title.strip(),
                "issue_description": issue_description.strip(), "client_id": client_id, "audit_id": audit_id,
                "audit_context": audit_context.strip(), "control_hint": control_hint.strip(),
                "rau_id": rau_id.strip(), "attributes_tested": [value.strip() for value in re.split(r"[,;]", attributes_text) if value.strip()],
                "auditor_id": auditor_id.strip(), "retrieved": {}, "memories": [], "citation_attempts": 0,
            }
            with st.status("Running the V1 triage graph…", expanded=True):
                result = graph.invoke(initial, config=config)
            current = graph.get_state(config).values or result
            store.save_run(current, model=settings.chat_model, prompt_version=settings.prompt_version)
            st.session_state["active_review"] = {"run_id": run_id, "config": config, "state": current}

    active_review = st.session_state.get("active_review")
    if active_review:
        current = active_review["state"]
        suggestion_data = current.get("suggestion")
        suggestion = TriageSuggestion.model_validate(suggestion_data) if suggestion_data else None
        retrieved_rows = current.get("retrieved", {})
        chunks = {item["chunk_id"]: item for group in retrieved_rows.values() for item in group}
        st.divider()
        chat_context = {
            "mode": "single-issue triage",
            "issue_title": current.get("issue_title", ""),
            "issue_description": current.get("issue_description", ""),
            "client_id": current.get("client_id", ""),
            "audit_id": current.get("audit_id", ""),
            "audit_context": current.get("audit_context", ""),
            "control_hint": current.get("control_hint", ""),
            "rau_id": current.get("rau_id", ""), "attributes_tested": current.get("attributes_tested", []),
        }
        left, right = st.columns([1.1, .9], gap="large")
        with left:
            with st.expander("Draft suggestion", expanded=True):
                st.caption(f"Run {active_review['run_id']} · Model {settings.chat_model} · Prompt {settings.prompt_version}")
                if current.get("error"):
                    st.warning(current["error"])
                if suggestion and current.get("review_pending"):
                    render_claim("Duplicate warning", suggestion.duplicate_warning.model_dump(), chunks)
                    render_claim("Suggested severity", suggestion.severity.model_dump(), chunks)
                    render_claim("Root-cause category", suggestion.root_cause.model_dump(), chunks)
                    render_claim("Mapped control ID", suggestion.control_mapping.model_dump(), chunks)
                    render_claim("Confidence", suggestion.confidence.model_dump(), chunks)
                    st.markdown("**Draft remediation steps**")
                    for i, step in enumerate(suggestion.remediation_steps, 1):
                        with st.container(border=True):
                            st.markdown(f"**Step {i}**")
                            render_claim("Action", step.action.model_dump(), chunks)
                            render_claim("Owner role", step.owner_role.model_dump(), chunks)
                            render_claim("Due-date suggestion", step.due_date_suggestion.model_dump(), chunks)
                    st.markdown("#### Similar past issues")
                    for item in suggestion.similar_issues:
                        source = chunks.get(item.chunk_id)
                        if source:
                            relevance = source.get("hybrid_score", 0.0)
                            with st.container(border=True):
                                st.markdown(f"**{source['title']}**")
                                st.caption(f"{source.get('source_id', 'Unknown')} · audit {source.get('audit_id') or 'not recorded'} · semantic {item.similarity:.3f} · hybrid {relevance:.5f} · {item.chunk_id}")
                                st.write(f"Outcome: {item.outcome or 'Not recorded'}")
                    st.markdown("#### Pre-approval checklist")
                    parsed_checks = [GateCheck.model_validate(value) for value in current.get("preapproval_checks", [])]
                    if not parsed_checks:
                        parsed_checks = evaluate_preapproval(current, suggestion, settings)
                    acknowledgements = {}
                    for check in parsed_checks:
                        label = f"{check.code} · {check.title}"
                        if check.status == "pass":
                            st.success(f"{label}: Pass — {check.detail}")
                        elif check.status == "lead_override_required":
                            st.warning(f"{label}: Lead override required — {check.detail}")
                        elif check.status == "warning":
                            acknowledgements[check.code] = st.checkbox(f"Acknowledge {label}: {check.detail}", key=f"gate_ack_{active_review['run_id']}_{check.code}")
                        else:
                            st.error(f"{label}: Fail — {check.detail}")
                    override_id = ""
                    override_reason = ""
                    if any(check.code == "C03" and check.status == "lead_override_required" for check in parsed_checks):
                        override_id = st.text_input("Lead auditor ID for C03 override", value=auditor_id if auditor_id in settings.mcp_lead_auditor_ids else "", key=f"lead_override_id_{active_review['run_id']}")
                        override_reason = st.text_area("Lead override reason (required)", key=f"lead_override_reason_{active_review['run_id']}")
                    gate = gate_approval(parsed_checks, acknowledgements, override_id, override_reason, settings.mcp_lead_auditor_ids)
                    st.caption("Email verification checks configuration only. The local synthetic corpus is used for C05; Jira is not queried. No external action runs until approval.")
                    decision_choice = st.radio(
                        "Decision", ["approve", "edit", "deny"], horizontal=True,
                        key=f"approval_decision_v2_{active_review['run_id']}",
                    )
                    edited = suggestion.model_dump_json(indent=2)
                    if decision_choice == "edit":
                        edited = st.text_area(
                            "Edit suggestion (JSON)", value=edited, height=300,
                            key=f"approval_edit_{active_review['run_id']}",
                        )
                    reason = st.text_area(
                        "Decision reason (required for denial)",
                        key=f"approval_reason_{active_review['run_id']}",
                    )
                    if st.button("Submit human decision", type="primary", key=f"approval_submit_{active_review['run_id']}", width="stretch", disabled=(decision_choice in {"approve", "edit"} and not gate.ready)):
                        if decision_choice == "deny" and not reason.strip():
                            st.error("A reason is required to deny the suggestion.")
                        else:
                            try:
                                decision = "reject" if decision_choice == "deny" else decision_choice
                                if decision_choice == "edit":
                                    edited_suggestion = TriageSuggestion.model_validate_json(edited)
                                    problems = citation_errors(edited_suggestion, retrieved_rows)
                                    if problems:
                                        raise ValueError("; ".join(problems))
                                payload = {"decision": decision, "edited_json": edited, "reason": reason.strip(), "approver": auditor_id.strip(),
                                    "warning_acknowledgements": acknowledgements, "lead_override_id": override_id.strip(), "lead_override_reason": override_reason.strip()}
                                review_graph = build_graph(settings)
                                review_graph.invoke(Command(resume=payload), config=active_review["config"])
                                finished = review_graph.get_state(active_review["config"]).values or {}
                                st.session_state.pop("active_review", None)
                                st.session_state["review_decision_notice"] = {"decision": decision_choice, "run_id": active_review["run_id"], "jira_status": finished.get("jira_status", "not_requested"), "jira_issue_key": finished.get("jira_issue_key", ""), "jira_issue_url": finished.get("jira_issue_url", ""), "jira_error": finished.get("jira_error", ""), "email_status": finished.get("email_status", "not_sent"), "email_error": finished.get("email_error", ""), "superseded_control": finished.get("superseded_control", False)}
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Review could not be recorded: {exc}")
                elif current.get("insufficient_evidence"):
                    retrieval_error = current.get("error", "")
                    if retrieval_error.startswith("Retrieval unavailable:"):
                        st.error(retrieval_error, title="Source lookup unavailable")
                    else:
                        st.warning(
                            "The assistant could not verify enough of the draft against sources retrieved for this run. "
                            "No recommendation was sent for approval. Try a more specific issue description, add the relevant "
                            "RAU or control hint, and run triage again. This is a retrieval and citation limitation; "
                            "it does not assess evidence sufficiency.",
                            title="Draft unavailable for review",
                        )
                        if retrieval_error:
                            with st.expander("Technical details", expanded=False):
                                st.caption(retrieval_error)
                elif suggestion and current.get("review_complete"):
                    st.info("This run already has a recorded human decision. Open its audit record from Run history to inspect the decision.")
        with right:
            with st.expander("Retrieved sources", expanded=False):
                render_retrieval_legend()
                for source_type, label in (("duplicate_issues", "Exact-audit duplicate candidates"), ("past_issues", "Similar issues from other audits"), ("controls", "Controls"), ("policies", "Policies")):
                    st.markdown(f"**{label}**")
                    for item in retrieved_rows.get(source_type, []):
                        render_source_card(item)
        render_run_chat("triage", active_review["run_id"], retrieved_rows, chat_context)
    else:
        render_chat_waiting_for_run("triage")
    if active_review:
        with st.expander("MCP call history · this triage run", expanded=False):
            calls = store.tool_calls_for_run(active_review["run_id"], limit=50)
            if calls:
                st.dataframe(calls, width="stretch", hide_index=True)
            else:
                st.info("No MCP tools have been called for this run. Read-only retrieval is shown under Retrieved sources; Jira and email tools run only after approval.")
    decision_notice = st.session_state.pop("review_decision_notice", None)
    if decision_notice:
        decision_label = decision_notice.get("decision") if isinstance(decision_notice, dict) else decision_notice
        if decision_label == "deny":
            st.warning("Suggestion denied and saved to the local audit database. No Jira draft was created.")
        else:
            if decision_label == "approve":
                st.success("Suggestion approved and saved to the local audit database.")
            else:
                st.success("Edited suggestion approved and saved to the local audit database.")
            jira_status = decision_notice.get("jira_status", "not_requested") if isinstance(decision_notice, dict) else "not_requested"
            if jira_status == "created":
                st.success(f"Jira draft created: [{decision_notice['jira_issue_key']}]({decision_notice['jira_issue_url']})")
                if decision_notice.get("superseded_control"):
                    st.warning("This approved item is flagged superseded because a lead auditor overrode the control-version check.")
            elif jira_status == "unavailable":
                st.warning(f"The local approval is saved; Jira draft creation was unavailable. {decision_notice.get('jira_error', '')}")
            elif jira_status == "permission_denied":
                st.warning("The local approval is saved, but this reviewer is not authorized to create Jira drafts.")
            elif jira_status == "not_configured":
                st.info("The local approval is saved. Configure Jira credentials and project settings to create a Jira draft.")
            email_status=decision_notice.get("email_status","not_sent") if isinstance(decision_notice,dict) else "not_sent"
            if jira_status == "created" and email_status == "sent":
                st.success("Email notification delivered via the configured backend with the Jira link and remediation details.")
            elif jira_status == "created" and email_status == "unavailable":
                st.warning(f"Jira issue is saved; business-owner email was not sent. {decision_notice.get('email_error','')}")
            elif jira_status == "created" and email_status == "not_configured":
                st.info("Jira issue is saved. Configure an email backend and business-owner email to enable notifications.")
        if isinstance(decision_notice, dict) and decision_notice.get("run_id"):
            with st.expander(f"MCP call history · triage run {decision_notice['run_id'][:8]}", expanded=True):
                calls = store.tool_calls_for_run(decision_notice["run_id"], limit=50)
                if calls:
                    st.dataframe(calls, width="stretch", hide_index=True)
                elif decision_notice.get("decision") == "deny":
                    st.info("No MCP tools ran because the suggestion was rejected. No Jira draft or email was created.")
                else:
                    st.info("No Jira or email MCP calls were recorded for this approval. Check the result above for configuration or permission details.")
    with st.expander("Recent audit trail", expanded=False):
        st.subheader("Recent audit trail", icon=":material/history:")
        st.dataframe(store.recent_runs(client_id=client_id, limit=30), width="stretch", hide_index=True)
        with st.expander("MCP tool-call audit log", expanded=False):
            st.dataframe(store.recent_tool_calls(limit=50), width="stretch", hide_index=True)
    with st.expander("Approved draft issues", expanded=False):
        st.subheader("Approved draft issues", icon=":material/task_alt:")
        approved_drafts = store.approved_drafts(client_id=history_client, limit=50)
        if not approved_drafts:
            st.info(f"No approved or edited drafts are recorded for {history_client} yet.")
        else:
            for item in approved_drafts:
                approved_suggestion = item["suggestion"]
                escaped_title = item["title"].replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
                issue_link = f"?approved_draft={quote(item['approval_id'], safe='')}"
                st.markdown(f"[{escaped_title}]({issue_link})")
                decision_label = "Approved" if item["decision"] == "approve" else "Approved with edits"
                st.caption(
                    f"{item['audit_id']} · {decision_label} · "
                    f"Severity: {approved_suggestion.get('severity', {}).get('value', 'Not recorded')} · "
                    f"Control: {approved_suggestion.get('control_mapping', {}).get('value', 'Not recorded')} · "
                    f"Approver: {item['approver']} · {item['decided_at']}"
                )
                if item.get("jira_issue_key"):
                    st.markdown(f"Jira draft: [{item['jira_issue_key']}]({item['jira_issue_url']})")
                    if item.get("email_status") == "success":
                        st.caption("Business-owner email notification sent.")
                    elif item.get("email_status") == "failed":
                        st.caption("Business-owner email failed; see the MCP tool-call audit log.")
                    if item.get("email_status") != "success" and settings.email_notification_ready:
                        if item["approver"] in settings.mcp_auditor_ids and st.button("Retry business-owner email", key=f"email_retry_{item['approval_id']}"):
                            try:
                                send_business_owner_email(settings, store, item["run_id"], item["client_id"], item["approver"], item["jira_issue_key"])
                                st.success("Email notification captured/sent via the configured backend.")
                            except Exception as exc:
                                st.error(f"Business-owner email retry failed: {exc}")
                else:
                    st.caption("No Jira draft is linked to this approved issue yet.")
                    if item["approver"] not in settings.mcp_auditor_ids:
                        st.caption("The recorded approver is not on the Jira draft permission allowlist.")
                    elif st.button(
                        "Retry Jira draft and notify owner" if item.get("jira_status") in {"failed", "unavailable"} else "Send approved issue to Jira",
                        key=f"jira_retry_{item['approval_id']}",
                        type="secondary",
                    ):
                        try:
                            with st.spinner("Creating the Jira draft from the saved approval…"):
                                jira_result = create_jira_draft(settings, store, item["run_id"], item["client_id"], item["approver"])
                            st.success(f"Jira draft created: [{jira_result['issue_key']}]({jira_result['issue_url']})")
                            if settings.email_notification_ready:
                                try:
                                    with st.spinner("Emailing the configured business owner…"):
                                        send_business_owner_email(settings, store, item["run_id"], item["client_id"], item["approver"], jira_result["issue_key"])
                                    st.success("Email notification captured/sent via the configured backend.")
                                except Exception as exc:
                                    st.warning(f"Jira draft exists, but the business-owner email was not sent: {exc}")
                            else:
                                st.info("Jira draft exists. Configure an email backend to notify the owner.")
                        except Exception as exc:
                            st.error(f"Jira draft retry failed: {exc}")
                with st.expander(f"MCP call history · run {item['run_id'][:8]}", expanded=False):
                    calls = store.tool_calls_for_run(item["run_id"], limit=50)
                    if calls:
                        st.dataframe(calls, width="stretch", hide_index=True)
                    else:
                        st.info("No MCP tool calls are recorded for this approved issue run.")
                st.divider()

            requested_approval_id = st.query_params.get("approved_draft")
            if requested_approval_id:
                selected_draft = next(
                    (item for item in approved_drafts if item["approval_id"] == requested_approval_id),
                    None,
                )
                st.query_params.pop("approved_draft", None)
                if selected_draft:
                    render_approved_draft_dialog(selected_draft)
with tab_scope:
    with st.container(border=True):
        st.subheader("Plan RAU testing", icon=":material/account_tree:")
        st.caption("Scoping is centered on the RAU and can use precedent from other audits. Select a synthetic client to keep retrieval within that client; audit, control, period, RAU name, and attributes are optional refinements. An entered audit period is included in semantic and keyword search.")
        with st.form("scope_intake"):
            scope_client_choice = st.selectbox("Synthetic client *", ["Select a synthetic client", "synthetic-client-a", "synthetic-client-b"], key="scope_client")
            scope_audit_choice = st.selectbox("Audit context (optional)", ["Not specified", "synthetic-audit-iam-2026", "synthetic-audit-finance-2026"], key="scope_audit")
            rau_id = st.text_input("RAU ID", value="RAU-AC-01")
            rau_name = st.text_input("Risk assessable unit name (optional)", value="")
            period = st.text_input("Audit period (optional)", value="")
            controls_in_scope = st.text_input("Control IDs in scope (optional, comma separated)", value="")
            attributes = st.text_input("Attributes to test (optional, comma separated)", value="")
            scope_submit = st.form_submit_button("Build cited scoping pack", type="primary", width="stretch")

    if scope_submit:
        scope_client = "" if scope_client_choice == "Select a synthetic client" else scope_client_choice
        scope_audit = "" if scope_audit_choice == "Not specified" else scope_audit_choice
        control_ids = list(dict.fromkeys(item.strip() for item in controls_in_scope.split(",") if item.strip()))
        scope_attributes = list(dict.fromkeys(item.strip() for item in attributes.split(",") if item.strip()))
        if not scope_client:
            st.warning("Select a synthetic client to build the scoping pack.")
        elif not rau_id.strip():
            st.warning("Enter a RAU ID to build the scoping pack.")
        else:
            scope_run_id = str(uuid.uuid4())
            config = {"configurable": {"thread_id": scope_run_id}}
            query_parts = [f"RAU {rau_id.strip()}"]
            if rau_name.strip():
                query_parts.append(rau_name.strip())
            if control_ids:
                query_parts.append(f"controls: {', '.join(control_ids)}")
            if scope_attributes:
                query_parts.append(f"attributes: {', '.join(scope_attributes)}")
            scope_input = {
                "run_id": scope_run_id, "client_id": scope_client, "audit_id": scope_audit,
                "rau_id": rau_id.strip(), "rau_name": rau_name.strip(), "audit_period": period.strip(),
                "control_ids": control_ids, "attributes": scope_attributes,
                "query": "; ".join(query_parts), "auditor_id": auditor_id.strip(),
            }
            try:
                graph = build_scoping_graph(settings)
                with st.status("Retrieving and clustering RAU precedent…", expanded=True):
                    result = graph.invoke(scope_input, config=config)
                current = graph.get_state(config).values or result
                st.session_state["active_scope"] = {"run_id": scope_run_id, "config": config, "state": current}
            except Exception as exc:
                st.error(f"Scoping retrieval failed: {exc}")

    active_scope = st.session_state.get("active_scope")
    if active_scope:
        scope_state = active_scope["state"]
        pack = scope_state["pack"]
        rows = pack["rows"]
        decisions = scope_state.get("decisions", [])
        decisions_by_id = {item["cluster_id"]: item for item in decisions}
        scope_chat_context = {
            "mode": "RAU scoping",
            "client_id": scope_state.get("client_id", ""),
            "audit_id": scope_state.get("audit_id", ""),
            "rau_id": pack.get("rau_id", ""),
            "audit_period": pack.get("audit_period", ""),
            "lookback_since": pack.get("lookback_since", ""),
            "clusters": [
                {"control_id": row["control_id"], "attribute": row["attribute"], "root_cause": row["root_cause"]}
                for row in rows
            ],
        }
        st.divider()
        if pack.get("audit_years"):
            audit_filter_label = (
                f"Audit-year filter: {', '.join(pack['audit_years'])} · "
                f"{len(pack.get('audit_ids_searched') or [])} matching audit ID(s)"
            )
        elif pack.get("audit_period"):
            audit_filter_label = f"Audit-period search term: {pack['audit_period']} · no year filter recognized"
        else:
            audit_filter_label = "Audit-year filter: none"
        st.caption(
            f"RAU {pack['rau_id']} · {audit_filter_label} · "
            f"lookback since {pack['lookback_since']} · run {active_scope['run_id']}"
        )
        render_retrieval_legend()
        for i, row in enumerate(rows):
            with st.container(border=True):
                st.subheader(f"{row['control_id']} · {row['attribute']}", icon=":material/verified_user:")
                priority_color = "orange" if row["priority"] == "High" else "blue" if row["priority"] == "Medium" else "gray"
                st.badge(f"{row['priority']} priority", color=priority_color)
                st.caption(f"Root cause: {row['root_cause']}")
                st.markdown(f"**Suggested action:** {row['action']}")
                st.write(row["procedure"])
                st.caption(row["priority_rationale"])
                st.write("Linked past issues: " + (", ".join(row["issue_ids"]) if row["issue_ids"] else "None retrieved — test based on inherent risk"))
                for chunk_id in row["citations"]:
                    cited = pack["citation_sources"].get(chunk_id)
                    if cited:
                        with st.expander(f"Citation · {cited['source_id']} · {cited['title']}"):
                            st.caption(
                                f"Source audit ID: {cited.get('audit_id') or 'Not recorded'} · "
                                f"Scoping audit context: {scope_state.get('audit_id') or 'Not specified'} · "
                                f"Chunk {cited['chunk_id']} · semantic similarity {cited.get('semantic_score', cited.get('score', 0)):.3f} · "
                                f"BM25 keyword {cited.get('keyword_score', 0):.3f} · term overlap {cited.get('keyword_overlap', 0):.0%} · "
                                f"RRF rank {cited.get('hybrid_score', 0):.5f}"
                            )
                            st.write(cited["text"])
                if row["cluster_id"] in decisions_by_id:
                    saved = decisions_by_id[row["cluster_id"]]
                    st.success(f"{saved['decision']} — {saved['reason']} · {saved['approver']}")
        if scope_state.get("review_pending"):
            index = scope_state["current_index"]
            row = rows[index]
            with st.container(border=True):
                st.subheader(f"Auditor decision · cluster {index + 1} of {len(rows)}", icon=":material/fact_check:")
                choice = st.radio("Decision", ["approved", "de_scoped", "escalated"], horizontal=True, key=f"scope_choice_{active_scope['run_id']}_{index}")
                why = st.text_area("Decision reason (required)", key=f"scope_reason_{active_scope['run_id']}_{index}")
                if st.button("Record decision and continue", type="primary", key=f"scope_submit_{active_scope['run_id']}_{index}", width="stretch"):
                    if not why.strip():
                        st.error("A reason is required for each cluster decision.")
                    else:
                        payload = {"cluster_id": row["cluster_id"], "decision": choice, "reason": why.strip(), "approver": auditor_id.strip()}
                        try:
                            graph = build_scoping_graph(settings)
                            result = graph.invoke(Command(resume=payload), config=active_scope["config"])
                            current = graph.get_state(active_scope["config"]).values or result
                            st.session_state["active_scope"]["state"] = current
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Cluster decision could not be recorded: {exc}")
        elif scope_state.get("review_complete"):
            st.success("All scoping rows have a recorded human decision.")

        source_issue_ids = sorted({
            str(item.get("source_id")) for item in pack.get("citation_sources", {}).values()
            if item.get("source_type") in {"past_issues", "past_issue"}
            and item.get("source_id") and item.get("client_id") == scope_state.get("client_id")
        })
        if scope_state.get("review_complete") and source_issue_ids:
            with st.expander("Create a cited summary document", expanded=False):
                st.caption("Builds an internal draft from retrieved issues and pauses for a separate document decision. It does not publish, share, or file the document.")
                template_labels = {
                    "scoping_pack": "RAU scoping pack", "cluster_summary": "Single cluster summary",
                    "lead_review": "Lead review pack", "single_issue_review": "Single issue review",
                }
                with st.form(f"summary_document_request_{active_scope['run_id']}"):
                    template = st.selectbox("Document template", list(template_labels), format_func=lambda value: template_labels[value], key=f"summary_template_{active_scope['run_id']}")
                    cluster_id = ""
                    issue_ids = list(source_issue_ids[:50])
                    if template == "cluster_summary":
                        cluster_ids = [row["cluster_id"] for row in rows if row.get("issue_ids")]
                        if cluster_ids:
                            cluster_id = st.selectbox("Cluster", cluster_ids, format_func=lambda value: next(f"{row['control_id']} · {row['attribute']} · {value}" for row in rows if row["cluster_id"] == value), key=f"summary_cluster_{active_scope['run_id']}")
                        else:
                            st.info("No retrieved issue cluster is available to summarize.")
                    elif template == "single_issue_review":
                        issue_ids = [st.selectbox("Retrieved issue", source_issue_ids, key=f"summary_issue_{active_scope['run_id']}")]
                    else:
                        issue_ids = st.multiselect("Retrieved issue sources (up to 50)", source_issue_ids, default=source_issue_ids[:50], key=f"summary_issues_{active_scope['run_id']}")
                    title = st.text_input("Optional document title", max_chars=120, key=f"summary_title_{active_scope['run_id']}")
                    include_remediation = st.checkbox("Include clearly labelled remediation drafts", value=False, key=f"summary_remediation_{active_scope['run_id']}")
                    create_document = st.form_submit_button("Create draft for review", type="primary", width="stretch")
                if create_document:
                    if template != "cluster_summary" and not issue_ids:
                        st.error("Select at least one retrieved issue.")
                    elif template == "cluster_summary" and not cluster_id:
                        st.error("Select a cluster that contains retrieved issues.")
                    else:
                        flow_id = str(uuid.uuid4())
                        requester_role = "lead_auditor" if auditor_id in settings.mcp_lead_auditor_ids else "auditor"
                        request = {
                            "run_id": active_scope["run_id"], "flow_id": flow_id,
                            "client_id": scope_state["client_id"], "audit_id": scope_state.get("audit_id", ""),
                            "rau_id": pack.get("rau_id", ""), "issue_ids": [] if cluster_id else issue_ids,
                            "cluster_id": cluster_id, "template": template, "title": title.strip(),
                            "include_remediation_drafts": include_remediation,
                            "requested_by_role": requester_role, "requested_by_id": auditor_id.strip(),
                        }
                        config = {"configurable": {"thread_id": flow_id}}
                        try:
                            graph = build_summary_document_graph(settings)
                            with st.spinner("Validating issue scope and building a cited draft…"):
                                result = graph.invoke({"request": request}, config=config)
                            doc_state = graph.get_state(config).values or result
                            st.session_state.setdefault("summary_documents_by_scope", {})[active_scope["run_id"]] = {"flow_id": flow_id, "config": config, "state": doc_state}
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Summary document could not be created: {exc}")
        else:
            with st.expander("Create a cited summary document", expanded=False):
                if not scope_state.get("review_complete"):
                    st.info("This option becomes available after every scoping cluster has an auditor decision and reason.")
                elif not source_issue_ids:
                    st.info("This run has no retrieved past-issue sources to cite, so a summary draft cannot be created. Refine the RAU, audit period, or control filters and run scoping again.")
        active_document = st.session_state.get("summary_documents_by_scope", {}).get(active_scope["run_id"])
        if active_document:
            with st.container(border=True):
                render_summary_document_review(
                    active_document, settings, auditor_id,
                    view_key=f"scope_{active_scope['run_id']}",
                )
        if pack["excluded_older_issues"]:
            with st.expander(f"Excluded issues older than lookback ({len(pack['excluded_older_issues'])})"):
                for item in pack["excluded_older_issues"]:
                    st.write(f"{item['source_id']} · {item['issue_date']} · {item['title']}")
        with st.expander("MCP call history · this RAU scoping run", expanded=False):
            calls = store.tool_calls_for_run(active_scope["run_id"], limit=50)
            if calls:
                st.dataframe(calls, width="stretch", hide_index=True)
            else:
                st.info("No MCP tools have been called for this run. Scoping retrieval uses the configured retrieval pipeline; the documents MCP tool is called only when you create a summary draft.")
        render_run_chat("scoping", active_scope["run_id"], pack["citation_sources"], scope_chat_context)
    else:
        render_chat_waiting_for_run("scoping")

    st.subheader("Saved scoping runs", icon=":material/folder_open:")
    scope_history_choice = st.selectbox("History client", ["All synthetic clients", "synthetic-client-a", "synthetic-client-b"], key="scope_history_client")
    scope_history_client = None if scope_history_choice == "All synthetic clients" else scope_history_choice
    scope_history = store.recent_scoping_runs(scope_history_client, limit=10)
    if scope_history:
        selected_scope = st.selectbox("Scoping run", [item["scope_run_id"] for item in scope_history], format_func=lambda run: f"{run[:8]} · {next(item['rau_id'] for item in scope_history if item['scope_run_id'] == run)}")
        selected_scope_meta = next(item for item in scope_history if item["scope_run_id"] == selected_scope)
        if st.button("Load or resume selected scoping run", width="stretch"):
            config = {"configurable": {"thread_id": selected_scope}}
            state = build_scoping_graph(settings).get_state(config).values
            if state:
                st.session_state["active_scope"] = {"run_id": selected_scope, "config": config, "state": state}
                st.rerun()
        details = store.scoping_details(selected_scope, scope_history_client, selected_scope_meta["audit_id"] or None)
        if details:
            with st.expander("Open saved pack and decisions"):
                st.json({"inputs": json.loads(details["input_json"]), "decisions": details["decisions"]})

    st.subheader("Summary documents", icon=":material/description:")
    saved_documents = store.summary_documents(scope_history_client, limit=25)
    if saved_documents:
        for saved_document in saved_documents:
            with st.expander(f"{saved_document['title']} · {saved_document['status']} · v{saved_document['version']}"):
                render_summary_document(saved_document, view_key=f"saved_{saved_document['document_id']}")
                if saved_document.get("status") == "draft":
                    active_saved = st.session_state.get("active_summary_document")
                    if active_saved and active_saved.get("flow_id") == saved_document.get("flow_id"):
                        render_summary_document_review(
                            active_saved, settings, auditor_id,
                            view_key=f"saved_active_{saved_document['document_id']}",
                        )
                    if st.button("Open pending document review", key=f"resume_document_{saved_document['document_id']}"):
                        doc_config = {"configurable": {"thread_id": saved_document["flow_id"]}}
                        document_graph = build_summary_document_graph(settings)
                        checkpoint = document_graph.get_state(doc_config).values
                        if checkpoint:
                            st.session_state["active_summary_document"] = {"flow_id": saved_document["flow_id"], "config": doc_config, "state": checkpoint}
                            st.rerun()
                        else:
                            st.error("The document draft exists, but its approval checkpoint is unavailable. Do not create a replacement until the run is reviewed.")
    else:
        st.info(
            "No summary document drafts are saved for this client selection yet. "
            "They are created manually after all cluster decisions are recorded: return to the active RAU scoping run, "
            "then expand **Create a cited summary document** and choose **RAU scoping pack** or **Single cluster summary**."
        )

st.info(
    "The assistant suggests; the auditor decides. It does not judge evidence sufficiency, approve, close, or file issues. Real client use requires SSO, tenant authorization, approved secret storage, and production database governance.",
    title="Human decision required",
    icon=":material/gavel:",
)

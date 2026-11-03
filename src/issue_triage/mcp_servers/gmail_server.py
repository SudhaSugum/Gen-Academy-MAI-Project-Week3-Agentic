"""Single-tool Gmail MCP server for notifying the configured business owner."""
from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

from mcp.server import MCPServer

from ..audit_log.store import AuditStore

server = MCPServer("issue-triage-gmail-notification")

def _claim_lines(suggestion: dict[str, Any]) -> list[str]:
    sections=[]
    for label, key in (("Suggested severity", "severity"), ("Root-cause category", "root_cause"), ("Mapped control", "control_mapping"), ("Duplicate screening", "duplicate_warning")):
        claim=suggestion.get(key,{})
        if claim:
            sections.extend([f"{label}: {claim.get('value','')}", f"Rationale: {claim.get('rationale','')}"])
            for citation in claim.get("citations",[]):
                sections.append(f"Evidence: {citation.get('source_id')} · {citation.get('chunk_id')} — {citation.get('quote')}")
    sections.append("Draft remediation steps:")
    for idx, step in enumerate(suggestion.get("remediation_steps",[]),1):
        action=step.get("action",{}); owner=step.get("owner_role",{}); due=step.get("due_date_suggestion",{})
        sections.append(f"{idx}. {action.get('value','')} | Suggested owner role: {owner.get('value','')} | Suggested due date: {due.get('value','')}")
        for label, claim in (("Action",action),("Owner role",owner),("Due date",due)):
            for citation in claim.get("citations",[]):
                sections.append(f"{label} source: {citation.get('source_id')} · {citation.get('chunk_id')} — {citation.get('quote')}")
    confidence=suggestion.get("confidence",{})
    if confidence:
        sections.append(f"Retrieval confidence: {confidence.get('value','')} — {confidence.get('rationale','')}")
        for citation in confidence.get("citations",[]):
            sections.append(f"Confidence source: {citation.get('source_id')} · {citation.get('chunk_id')} — {citation.get('quote')}")
    sections.append("The auditor-approved recommendation is decision support; the business owner should review the source trail and coordinate remediation through the Jira project workflow.")
    return sections

def _formatted_email_html(issue: dict[str, Any], suggestion: dict[str, Any], jira_url: str, run_id: str, approval_id: str, client_id: str) -> str:
    esc = lambda value: html.escape(str(value or ""))
    parts = [
        "<h1>Auditor-approved issue draft</h1>",
        "<p>Prepared for business-owner review. The auditor retains responsibility for the final severity and disposition.</p>",
        f"<p><b>Jira:</b> <a href=\"{esc(jira_url)}\">{esc(jira_url)}</a><br><b>Triage run:</b> {esc(run_id)}<br><b>Approval:</b> {esc(approval_id)}<br><b>Synthetic client:</b> {esc(client_id)}<br><b>Audit:</b> {esc(issue.get('audit_id', ''))}</p>",
        "<h2>Issue details</h2>",
        f"<p><b>{esc(issue.get('issue_title', ''))}</b></p><p>{esc(issue.get('issue_description', '')).replace(chr(10), '<br>')}</p>",
        "<h2>Triage assessment</h2><ul>",
    ]
    for label, key in (("Suggested severity", "severity"), ("Root-cause category", "root_cause"), ("Mapped control", "control_mapping"), ("Duplicate screening", "duplicate_warning"), ("Retrieval confidence", "confidence")):
        claim = suggestion.get(key, {})
        if not claim:
            continue
        parts.append(f"<li><b>{esc(label)}:</b> {esc(claim.get('value', ''))}")
        if claim.get("rationale"):
            parts.append(f"<br>{esc(claim['rationale'])}")
        citations = claim.get("citations", [])
        if citations:
            parts.append("<ul>" + "".join(f"<li><b>{esc(c.get('source_id'))} · {esc(c.get('chunk_id'))}</b>: {esc(c.get('quote'))}</li>" for c in citations) + "</ul>")
        parts.append("</li>")
    parts.append("</ul><h2>Draft remediation plan</h2>")
    steps = suggestion.get("remediation_steps", [])
    if steps:
        parts.append("<ol>")
        for step in steps:
            action, owner, due = step.get("action", {}), step.get("owner_role", {}), step.get("due_date_suggestion", {})
            parts.append(f"<li><b>Action:</b> {esc(action.get('value'))}<br><b>Suggested owner role:</b> {esc(owner.get('value'))}<br><b>Suggested due date:</b> {esc(due.get('value'))}")
            citations = [c for claim in (action, owner, due) for c in claim.get("citations", [])]
            if citations:
                parts.append("<ul>" + "".join(f"<li><b>{esc(c.get('source_id'))} · {esc(c.get('chunk_id'))}</b>: {esc(c.get('quote'))}</li>" for c in citations) + "</ul>")
            parts.append("</li>")
        parts.append("</ol>")
    else:
        parts.append("<p>No remediation steps were supported by the approved suggestion.</p>")
    parts.append("<p><i>Review the cited source material and confirm remediation ownership and timing through the normal business process.</i></p>")
    return "<!doctype html><html><body>" + "".join(parts) + "</body></html>"

def _send_business_owner_email_impl(run_id: str, client_id: str, approver: str, issue_key: str) -> dict[str, Any]:
    """Email the configured business owner after checking a persisted human approval and Jira draft."""
    allowed_reviewers={value.strip() for value in os.getenv("JIRA_DRAFT_ALLOWED_AUDITOR_IDS", os.getenv("MCP_AUDITOR_IDS", "")).split(",") if value.strip()}
    if approver not in allowed_reviewers:
        raise PermissionError("This reviewer is not allowed to trigger owner notifications.")
    store=AuditStore(os.getenv("DATABASE_URL", "sqlite:///./data/issue_triage.db")); store.initialize()
    approval=store.approved_jira_payload(run_id,client_id,approver)
    jira=store.jira_draft_for_run(run_id)
    if not approval or not jira or jira.get("issue_key") != issue_key or jira.get("approval_id") != approval.get("approval_id"):
        raise PermissionError("A matching persisted approval and Jira issue are required before email notification.")
    if store.tool_call_succeeded(run_id,"send_business_owner_email"):
        return {"status":"already_sent","recipient_count":1,"issue_key":issue_key}
    backend=os.getenv("EMAIL_BACKEND", "mailpit").strip().lower()
    smtp_host=os.getenv("EMAIL_SMTP_HOST", "127.0.0.1" if backend == "mailpit" else "smtp.gmail.com").strip()
    smtp_username=os.getenv("EMAIL_SMTP_USERNAME", os.getenv("GMAIL_SENDER", "")).strip()
    smtp_password=os.getenv("EMAIL_SMTP_PASSWORD", os.getenv("GMAIL_APP_PASSWORD", "")).replace(" ", "")
    sender=os.getenv("EMAIL_SENDER", os.getenv("GMAIL_SENDER", "issue-triage@localhost")).strip()
    recipient_raw=os.getenv("BUSINESS_OWNER_EMAIL", "").strip()
    recipient=parseaddr(recipient_raw)[1]
    if backend not in {"mailpit", "smtp"}:
        raise RuntimeError("EMAIL_BACKEND must be either mailpit or smtp.")
    if not sender or not recipient or recipient != recipient_raw:
        raise RuntimeError("Configure EMAIL_SENDER and one valid BUSINESS_OWNER_EMAIL.")
    if backend == "mailpit" and smtp_host.casefold() not in {"127.0.0.1", "localhost", "::1", "mailpit"}:
        raise RuntimeError("Mailpit mode only permits a local Mailpit host.")
    if backend == "smtp" and (not smtp_username or not smtp_password):
        raise RuntimeError("SMTP mode requires EMAIL_SMTP_USERNAME and EMAIL_SMTP_PASSWORD.")
    issue=approval["issue"]; suggestion=approval["suggestion"]
    message=EmailMessage()
    message["From"]=sender; message["To"]=recipient
    issue_title=" ".join(str(issue.get("issue_title","Audit issue")).split())
    message["Subject"]=f"Approved audit issue for review: {issue_key} — {issue_title}"[:250]
    plain_sections = [
        "AUDITOR-APPROVED ISSUE DRAFT",
        "Prepared for business-owner review. The auditor retains responsibility for final severity and disposition.",
        f"Jira: {jira['issue_url']}",
        f"Triage run: {run_id} | Approval: {approval['approval_id']}",
        f"Synthetic client: {client_id} | Audit: {issue.get('audit_id', '')}",
        "ISSUE DETAILS",
        f"Title: {issue.get('issue_title', '')}",
        str(issue.get('issue_description', '')),
        "TRIAGE ASSESSMENT AND SOURCE CITATIONS",
        *_claim_lines(suggestion),
    ]
    message.set_content("\n\n".join(plain_sections))
    message.add_alternative(_formatted_email_html(issue, suggestion, jira["issue_url"], run_id, approval["approval_id"], client_id), subtype="html")
    try:
        smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "1025" if backend == "mailpit" else "465"))
    except ValueError as exc:
        raise RuntimeError("EMAIL_SMTP_PORT must be a valid TCP port number.") from exc
    if not 1 <= smtp_port <= 65535:
        raise RuntimeError("EMAIL_SMTP_PORT must be between 1 and 65535.")
    security=os.getenv("EMAIL_SMTP_SECURITY", "none" if backend == "mailpit" else ("ssl" if smtp_port == 465 else "starttls")).strip().lower()
    if security not in {"none", "ssl", "starttls"} or (backend == "smtp" and security == "none") or (backend == "mailpit" and security != "none"):
        raise RuntimeError("EMAIL_SMTP_SECURITY must be none for Mailpit, or ssl/starttls for SMTP.")
    context=ssl.create_default_context()
    stage="connect"
    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(smtp_host,smtp_port,timeout=20,context=context) as smtp:
                if smtp_username:
                    stage="authenticate"; smtp.login(smtp_username,smtp_password)
                stage="send"; smtp.send_message(message)
        else:
            with smtplib.SMTP(smtp_host,smtp_port,timeout=20) as smtp:
                if security == "starttls":
                    smtp.ehlo(); smtp.starttls(context=context); smtp.ehlo()
                if smtp_username:
                    stage="authenticate"; smtp.login(smtp_username,smtp_password)
                stage="send"; smtp.send_message(message)
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        # Do not automatically retry: disconnect during send can leave delivery ambiguous.
        raise RuntimeError(f"Email SMTP failed during {stage} ({type(exc).__name__}).") from exc
    return {"status":"sent","recipient_count":1,"issue_key":issue_key}


@server.tool()
def send_business_owner_email(run_id: str, client_id: str, approver: str, issue_key: str) -> dict[str, Any]:
    """Return structured, redacted failures so the caller can explain delivery problems."""
    try:
        return _send_business_owner_email_impl(run_id, client_id, approver, issue_key)
    except (PermissionError, RuntimeError) as exc:
        detail = str(exc)
        for secret in (os.getenv("EMAIL_SMTP_PASSWORD", ""), os.getenv("GMAIL_APP_PASSWORD", ""), os.getenv("DATABASE_URL", "")):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        for sensitive in (os.getenv("EMAIL_SMTP_USERNAME", ""), os.getenv("EMAIL_SENDER", ""), os.getenv("GMAIL_SENDER", ""), os.getenv("BUSINESS_OWNER_EMAIL", "")):
            if sensitive:
                detail = detail.replace(sensitive, "[REDACTED]")
        return {"status": "error", "error_type": type(exc).__name__, "error": detail}
    except Exception as exc:
        # Do not send exception text, addresses, issue content, or credentials back to graph state.
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": "Gmail notification failed inside the MCP server; inspect its local configuration and audit database.",
        }


if __name__ == "__main__":
    server.run(transport="stdio")

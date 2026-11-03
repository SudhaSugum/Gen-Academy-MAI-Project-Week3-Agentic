"""Allowlisted MCP client wrappers with bounded execution and redacted audit records."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from ..audit_log.store import AuditStore
from ..config import Settings

logger = logging.getLogger(__name__)
ALLOWED_JIRA_TOOLS = {"create_issue_draft"}

def _safe_failure_detail(exc: BaseException, *secrets: str) -> str:
    """Unwrap MCP/AnyIO exception groups without exposing configured secrets."""
    current = exc
    while True:
        if isinstance(current, BaseExceptionGroup) and current.exceptions:
            current = current.exceptions[0]
            continue
        cause = current.__cause__
        if cause is not None and cause is not current:
            current = cause
            continue
        break
    detail = str(current).strip()
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
    detail = " ".join(detail.split())[:240]
    return f"{type(current).__name__}: {detail or 'No detail supplied'}"

def _server_environment(settings: Settings) -> dict[str, str]:
    root = str(Path(__file__).resolve().parents[3])
    return {
        "PYTHONPATH": root, "DATABASE_URL": settings.database_url,
        "JIRA_BASE_URL": settings.jira_base_url, "JIRA_EMAIL": settings.jira_email,
        "JIRA_API_TOKEN": settings.jira_api_token, "JIRA_PROJECT_KEY": settings.jira_project_key,
        "JIRA_ISSUE_TYPE": settings.jira_issue_type,
        "JIRA_DRAFT_ALLOWED_AUDITOR_IDS": ",".join(settings.mcp_auditor_ids),
        "MCP_LEAD_AUDITOR_IDS": ",".join(settings.mcp_lead_auditor_ids),
    }

async def _call_jira_tool(settings: Settings, run_id: str, client_id: str, approver: str) -> dict[str, Any]:
    from mcp import Client, StdioServerParameters

    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "src.issue_triage.mcp_servers.jira_server"],
        env=_server_environment(settings), cwd=str(Path(__file__).resolve().parents[3]),
    )
    async with Client(parameters) as client:
        tools = await client.list_tools()
        tool_names = {tool.name for tool in tools.tools}
        if tool_names != ALLOWED_JIRA_TOOLS:
            raise RuntimeError("Jira MCP tool allowlist did not match the expected registration.")
        result = await client.call_tool("create_issue_draft", {"run_id": run_id, "client_id": client_id, "approver": approver})
        if result.is_error:
            detail = " ".join(str(block.text) for block in result.content if getattr(block, "text", None))
            raise RuntimeError(detail or "Jira MCP server returned a tool error.")
        payload = result.structured_content
        if not isinstance(payload, dict):
            raise RuntimeError("Jira MCP server returned an invalid structured result.")
        if payload.get("status") == "error":
            raise RuntimeError(f"{payload.get('error_type', 'JiraError')}: {payload.get('error', 'Jira tool failed.')}")
        if payload.get("status") not in {"created", "already_created"} or not payload.get("issue_key"):
            raise RuntimeError("Jira MCP server returned no created issue key.")
        return payload

def create_jira_draft(settings: Settings, store: AuditStore, run_id: str, client_id: str, approver: str) -> dict[str, Any]:
    """Call only the configured Jira MCP write tool; retry transient failures once.

    Jira's deterministic run label makes the server operation safe to retry after an
    ambiguous transport failure: the server searches for that label before creating.
    """
    for attempt in range(2):
        started = time.monotonic()
        try:
            result = asyncio.run(asyncio.wait_for(_call_jira_tool(settings, run_id, client_id, approver), timeout=35))
            store.append_tool_call(
                run_id, "jira", "create_issue_draft",
                {"run_id": "[REDACTED]", "client_id": "[REDACTED]", "approver": "[REDACTED]"},
                "success", round((time.monotonic()-started)*1000), approver=approver,
                result_reference=str(result.get("issue_key", "")),
            )
            return result
        except Exception as exc:
            detail = _safe_failure_detail(exc, settings.jira_api_token, settings.database_url)
            message = detail.casefold()
            transient = any(marker in message for marker in ("timed out", "connection failed", "http 5", "temporarily unavailable"))
            store.append_tool_call(
                run_id, "jira", "create_issue_draft",
                {"run_id": "[REDACTED]", "client_id": "[REDACTED]", "approver": "[REDACTED]"},
                "failed", round((time.monotonic()-started)*1000), approver=approver,
            )
            if attempt == 0 and transient:
                logger.info("run_id=%s mcp_server=jira tool=create_issue_draft retry=1", run_id)
                continue
            logger.warning("run_id=%s mcp_server=jira tool=create_issue_draft failed type=%s detail=%s", run_id, type(exc).__name__, detail)
            raise RuntimeError(f"Jira draft creation unavailable: {detail}") from exc
    raise RuntimeError("Jira draft creation unavailable.")


def _gmail_environment(settings: Settings) -> dict[str, str]:
    # Keep the Gmail subprocess isolated from Jira API credentials.
    root = str(Path(__file__).resolve().parents[3])
    return {
        "PYTHONPATH": root, "DATABASE_URL": settings.database_url,
        "JIRA_DRAFT_ALLOWED_AUDITOR_IDS": ",".join(settings.mcp_auditor_ids),
        "EMAIL_BACKEND": settings.email_backend,
        "EMAIL_SMTP_HOST": settings.email_smtp_host,
        "EMAIL_SMTP_PORT": str(settings.email_smtp_port),
        "EMAIL_SMTP_SECURITY": settings.email_smtp_security,
        "EMAIL_SMTP_USERNAME": settings.email_smtp_username,
        "EMAIL_SMTP_PASSWORD": settings.email_smtp_password,
        "EMAIL_SENDER": settings.email_sender,
        "BUSINESS_OWNER_EMAIL": settings.business_owner_email,
    }

async def _call_gmail_tool(settings: Settings, run_id: str, client_id: str, approver: str, issue_key: str) -> dict[str, Any]:
    from mcp import Client, StdioServerParameters

    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "src.issue_triage.mcp_servers.gmail_server"],
        env=_gmail_environment(settings), cwd=str(Path(__file__).resolve().parents[3]),
    )
    failure: str | None = None
    payload: dict[str, Any] | None = None
    async with Client(parameters) as client:
        tools = await client.list_tools()
        if {tool.name for tool in tools.tools} != {"send_business_owner_email"}:
            failure = "Gmail MCP tool allowlist did not match the expected registration."
        else:
            result = await client.call_tool("send_business_owner_email", {
                "run_id": run_id, "client_id": client_id, "approver": approver, "issue_key": issue_key,
            })
            if result.is_error:
                failure = " ".join(str(block.text) for block in result.content if getattr(block, "text", None)) or "Gmail MCP server returned a tool error."
            else:
                structured = result.structured_content
                if not isinstance(structured, dict):
                    failure = "Gmail MCP server returned an invalid structured result."
                else:
                    payload = structured
                    if payload.get("status") == "error":
                        error_type = str(payload.get("error_type", "Error"))[:80]
                        detail = str(payload.get("error", "Gmail MCP server reported a delivery failure."))[:500]
                        failure = f"{error_type}: {detail}"
                    elif payload.get("status") not in {"sent", "already_sent"}:
                        failure = "Gmail MCP server returned an unexpected delivery status."
    # Raise only after the MCP stdio task groups have closed cleanly; otherwise
    # AnyIO wraps the intended message in an opaque ExceptionGroup.
    if failure:
        raise RuntimeError(failure)
    if payload is None:
        raise RuntimeError("Gmail MCP server returned no result.")
    return payload

def send_business_owner_email(settings: Settings, store: AuditStore, run_id: str, client_id: str, approver: str, issue_key: str) -> dict[str, Any]:
    """Call the single configured Gmail notification tool and audit redacted arguments."""
    started = time.monotonic()
    try:
        result = asyncio.run(asyncio.wait_for(_call_gmail_tool(settings, run_id, client_id, approver, issue_key), timeout=35))
        store.append_tool_call(
            run_id, "gmail", "send_business_owner_email",
            {"run_id": "[REDACTED]", "client_id": "[REDACTED]", "approver": "[REDACTED]", "issue_key": "[REDACTED]", "recipient": "[REDACTED]", "message": "[REDACTED]"},
            "success", round((time.monotonic()-started)*1000), approver=approver,
            result_reference=f"recipient_count={result.get('recipient_count', 0)}",
        )
        return result
    except Exception as exc:
        store.append_tool_call(
            run_id, "gmail", "send_business_owner_email",
            {"run_id": "[REDACTED]", "client_id": "[REDACTED]", "approver": "[REDACTED]", "issue_key": "[REDACTED]", "recipient": "[REDACTED]", "message": "[REDACTED]"},
            "failed", round((time.monotonic()-started)*1000), approver=approver,
        )
        detail = _safe_failure_detail(
            exc, settings.email_smtp_password, settings.database_url,
            settings.email_smtp_username, settings.email_sender, settings.business_owner_email,
        )
        logger.warning(
            "run_id=%s mcp_server=gmail tool=send_business_owner_email failed detail=%s",
            run_id, detail,
        )
        raise RuntimeError(f"Business-owner email could not be sent: {detail}") from exc

def _documents_environment(settings: Settings) -> dict[str, str]:
    root = str(Path(__file__).resolve().parents[3])
    return {
        "PYTHONPATH": root, "DATABASE_URL": settings.database_url,
        "MCP_AUDITOR_IDS": ",".join(settings.mcp_auditor_ids),
        "MCP_LEAD_AUDITOR_IDS": ",".join(settings.mcp_lead_auditor_ids),
    }


async def _call_documents_tool(settings: Settings, request: dict[str, Any]) -> dict[str, Any]:
    from mcp import Client, StdioServerParameters

    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "src.issue_triage.mcp_servers.documents_server"],
        env=_documents_environment(settings), cwd=str(Path(__file__).resolve().parents[3]),
    )
    failure: str | None = None
    payload: dict[str, Any] | None = None
    async with Client(parameters) as client:
        tools = await client.list_tools()
        if {tool.name for tool in tools.tools} != {"create_summary_document_draft"}:
            failure = "Documents MCP tool allowlist did not match the expected registration."
        else:
            result = await client.call_tool("create_summary_document_draft", request)
            if result.is_error:
                failure = " ".join(str(block.text) for block in result.content if getattr(block, "text", None)) or "Documents MCP server returned a tool error."
            else:
                structured = result.structured_content
                if not isinstance(structured, dict):
                    failure = "Documents MCP server returned an invalid structured result."
                else:
                    payload = structured
                    if payload.get("status") == "error":
                        error_type = str(payload.get("error_type", "Error"))[:80]
                        detail = str(payload.get("error", "Document MCP tool failed."))[:400]
                        failure = f"{error_type}: {detail}"
    if failure:
        raise RuntimeError(failure)
    if payload is None:
        raise RuntimeError("Documents MCP server returned no result.")
    return payload


def create_summary_document_draft(settings: Settings, request: dict[str, Any]) -> dict[str, Any]:
    """Call the sole allowlisted documents MCP tool; all arguments are revalidated server-side."""
    return asyncio.run(asyncio.wait_for(_call_documents_tool(settings, request), timeout=35))

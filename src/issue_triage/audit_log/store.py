"""Portable SQL audit store. Application write APIs are append-only for runs, events, and decisions."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text


class AuditStore:
    def __init__(self, database_url: str):
        if database_url.startswith("sqlite:///"):
            db_path = database_url.removeprefix("sqlite:///")
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(database_url, connect_args={"check_same_thread": False}, pool_pre_ping=True)
        else:
            self.engine = create_engine(database_url, pool_pre_ping=True)

    def initialize(self) -> None:
        ddl = [
            "CREATE TABLE IF NOT EXISTS issues (issue_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, client_id TEXT NOT NULL, audit_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, submitted_by TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS triage_runs (run_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, client_id TEXT NOT NULL, audit_id TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL, status TEXT NOT NULL, input_json TEXT NOT NULL, retrieved_json TEXT NOT NULL, suggestion_json TEXT, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS approvals (approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, decision TEXT NOT NULL, edited_fields_json TEXT, final_suggestion_json TEXT, reason TEXT NOT NULL, approver TEXT NOT NULL, decided_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS scoping_runs (scope_run_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, audit_id TEXT NOT NULL, rau_id TEXT NOT NULL, audit_period TEXT NOT NULL, input_json TEXT NOT NULL, pack_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS scoping_decisions (decision_id TEXT PRIMARY KEY, scope_run_id TEXT NOT NULL, cluster_id TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, approver TEXT NOT NULL, decided_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS audit_events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS jira_drafts (draft_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL, issue_key TEXT NOT NULL, issue_url TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tool_calls (tool_call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, server_name TEXT NOT NULL, tool_name TEXT NOT NULL, redacted_arguments_json TEXT NOT NULL, result_status TEXT NOT NULL, duration_ms INTEGER NOT NULL, approver TEXT NOT NULL, result_reference TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS summary_documents (document_id TEXT PRIMARY KEY, flow_id TEXT NOT NULL, run_id TEXT NOT NULL, client_id TEXT NOT NULL, audit_id TEXT NOT NULL, rau_id TEXT NOT NULL, template TEXT NOT NULL, source_issue_ids_json TEXT NOT NULL, location TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS summary_document_versions (version_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version INTEGER NOT NULL, content_json TEXT NOT NULL, created_by_run_id TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(document_id, version))",
            "CREATE TABLE IF NOT EXISTS summary_document_decisions (decision_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, approver TEXT NOT NULL, approver_role TEXT NOT NULL, edited_fields_json TEXT, decided_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_issues_tenant ON issues(client_id, audit_id)",
            "CREATE INDEX IF NOT EXISTS idx_runs_tenant_time ON triage_runs(client_id, audit_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_events_run ON audit_events(run_id, created_at)",
        ]
        with self.engine.begin() as conn:
            for statement in ddl:
                conn.execute(text(statement))
            approval_columns = {column["name"] for column in inspect(conn).get_columns("approvals")}
            if "final_suggestion_json" not in approval_columns:
                conn.execute(text("ALTER TABLE approvals ADD COLUMN final_suggestion_json TEXT"))
            if self.engine.dialect.name == "sqlite":
                for table in ("issues", "triage_runs", "scoping_runs"):
                    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                conn.execute(text("CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                conn.execute(text("CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                conn.execute(text("CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                conn.execute(text("CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                conn.execute(text("CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON scoping_decisions BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                conn.execute(text("CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON scoping_decisions BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                for table in ("jira_drafts", "tool_calls", "summary_documents", "summary_document_versions", "summary_document_decisions"):
                    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))
                    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"))

    @staticmethod
    def _json(value: Any) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        elif hasattr(value, "model_dump_json"):
            value = json.loads(value.model_dump_json())
        return json.dumps(value, default=str, ensure_ascii=False)

    def append_event(self, run_id: str, event_type: str, payload: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO audit_events VALUES (:id,:run,:type,:payload,:created)"), {
                "id": str(uuid.uuid4()), "run": run_id, "type": event_type,
                "payload": self._json(payload), "created": datetime.now(timezone.utc).isoformat(),
            })

    def save_run(self, state: dict[str, Any], model: str, prompt_version: str) -> None:
        issue = {key: state.get(key, "") for key in ("issue_title", "issue_description", "client_id", "audit_id", "audit_context", "control_hint", "auditor_id", "rau_id", "attributes_tested")}
        created = datetime.now(timezone.utc).isoformat()
        status = "review_pending" if state.get("review_pending") else "insufficient_evidence"
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO issues VALUES (:id,:run,:client,:audit,:title,:description,:submitter,:created)"), {
                "id": state["issue_id"], "run": state["run_id"], "client": state["client_id"], "audit": state["audit_id"],
                "title": state["issue_title"], "description": state["issue_description"], "submitter": state["auditor_id"], "created": created,
            })
            conn.execute(text("INSERT INTO triage_runs VALUES (:run,:issue,:client,:audit,:model,:prompt,:status,:input,:retrieved,:suggestion,:created)"), {
                "run": state["run_id"], "issue": state["issue_id"], "client": state["client_id"], "audit": state["audit_id"],
                "model": model, "prompt": prompt_version, "status": status, "input": self._json(issue),
                "retrieved": self._json(state.get("retrieved", {})), "suggestion": self._json(state.get("suggestion")) if state.get("suggestion") else None, "created": created,
            })
            for event in state.get("log", []):
                conn.execute(text("INSERT INTO audit_events VALUES (:id,:run,:type,:payload,:created)"), {
                    "id": str(uuid.uuid4()), "run": state["run_id"], "type": event.get("event", "graph_event"),
                    "payload": self._json(event.get("details", {})), "created": created,
                })
            conn.execute(text("INSERT INTO audit_events VALUES (:id,:run,:type,:payload,:created)"), {
                "id": str(uuid.uuid4()), "run": state["run_id"], "type": "triage_completed",
                "payload": self._json({"model": model, "prompt_version": prompt_version, "status": status, "retrieved": state.get("retrieved", {}), "suggestion": state.get("suggestion"), "error": state.get("error"), "retrieval_latency_ms": state.get("retrieval_latency_ms"), "triage_latency_ms": state.get("triage_latency_ms"), "token_usage": state.get("token_usage", {})}), "created": created,
            })

    def record_approval(self, run_id: str, payload: dict[str, Any], suggestion: Any) -> str:
        decision = payload["decision"]
        suggestion_json = self._json(suggestion)
        approval_id = str(uuid.uuid4())
        with self.engine.begin() as conn:
            conn.execute(text("""INSERT INTO approvals
                (approval_id, run_id, decision, edited_fields_json, final_suggestion_json, reason, approver, decided_at)
                VALUES (:id,:run,:decision,:edited,:suggestion,:reason,:approver,:created)"""), {
                "id": approval_id, "run": run_id, "decision": decision,
                "edited": suggestion_json if decision == "edit" else None,
                "suggestion": suggestion_json if decision in {"approve", "edit"} else None,
                "reason": payload.get("reason", ""), "approver": payload["approver"],
                "created": datetime.now(timezone.utc).isoformat(),
            })
        return approval_id

    def append_tool_call(self, run_id: str, server_name: str, tool_name: str, redacted_arguments: dict[str, Any], result_status: str, duration_ms: int, approver: str = "", result_reference: str = "") -> None:
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO tool_calls VALUES (:id,:run,:server,:tool,:args,:status,:duration,:approver,:reference,:created)"), {
                "id": str(uuid.uuid4()), "run": run_id, "server": server_name, "tool": tool_name,
                "args": self._json(redacted_arguments), "status": result_status, "duration": int(duration_ms),
                "approver": approver, "reference": result_reference, "created": datetime.now(timezone.utc).isoformat(),
            })

    def record_jira_draft(self, run_id: str, approval_id: str, issue_key: str, issue_url: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO jira_drafts VALUES (:id,:run,:approval,:key,:url,:created)"), {
                "id": str(uuid.uuid4()), "run": run_id, "approval": approval_id, "key": issue_key,
                "url": issue_url, "created": datetime.now(timezone.utc).isoformat(),
            })
        self.append_event(run_id, "jira_issue_draft_created", {"approval_id": approval_id, "issue_key": issue_key, "issue_url": issue_url})

    def jira_draft_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT approval_id, issue_key, issue_url, created_at FROM jira_drafts WHERE run_id=:run"), {"run": run_id}).mappings().first()
            return dict(row) if row else None

    def approved_jira_payload(self, run_id: str, client_id: str, approver: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("""SELECT a.approval_id,a.decision,a.approver,a.final_suggestion_json,
                r.client_id,r.input_json,r.retrieved_json FROM approvals a JOIN triage_runs r ON r.run_id=a.run_id
                WHERE a.run_id=:run ORDER BY a.decided_at DESC LIMIT 1"""), {"run": run_id}).mappings().first()
        if not row or row["decision"] not in {"approve", "edit"} or row["client_id"] != client_id or row["approver"] != approver:
            return None
        with self.engine.connect() as conn:
            event = conn.execute(text("SELECT payload_json FROM audit_events WHERE run_id=:run AND event_type='preapproval_gate_decision' ORDER BY created_at DESC LIMIT 1"), {"run": run_id}).scalar()
        gate = json.loads(event) if event else {}
        checks = gate.get("checks", [])
        required = {"C01", "C02", "C03", "C04", "C05", "EV1"}
        if gate.get("decision") != row["decision"] or not required.issubset({str(item.get("code", "")) for item in checks}):
            return None
        acknowledgements = gate.get("warning_acknowledgements", {})
        for check in checks:
            status = check.get("status")
            if status not in {"pass", "lead_overridden"} and not (status == "warning" and acknowledgements.get(check.get("code")) is True):
                return None
        superseded = any(item.get("code") == "C03" and item.get("status") == "lead_overridden" for item in checks)
        lead_id = str(gate.get("lead_override_id", "")) if superseded else ""
        lead_reason = str(gate.get("lead_override_reason", "")) if superseded else ""
        if superseded and (not lead_id or not lead_reason.strip()):
            return None
        return {"approval_id": row["approval_id"], "decision": row["decision"], "approver": row["approver"],
                "client_id": row["client_id"], "issue": json.loads(row["input_json"]),
                "suggestion": json.loads(row["final_suggestion_json"] or "{}"),
                "retrieved": json.loads(row["retrieved_json"] or "{}"),
                "superseded_control": superseded, "lead_override_id": lead_id, "lead_override_reason": lead_reason}

    def tool_call_succeeded(self, run_id: str, tool_name: str) -> bool:
        with self.engine.connect() as conn:
            result = conn.execute(text("SELECT 1 FROM tool_calls WHERE run_id=:run AND tool_name=:tool AND result_status='success' LIMIT 1"), {"run": run_id, "tool": tool_name}).first()
            return result is not None

    def recent_tool_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows=conn.execute(text("SELECT run_id,server_name,tool_name,redacted_arguments_json,result_status,duration_ms,approver,result_reference,created_at FROM tool_calls ORDER BY created_at DESC LIMIT :limit"), {"limit": limit}).mappings().all()
            return [dict(row) for row in rows]

    def tool_calls_for_run(self, run_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return redacted MCP audit rows for one triage or scoping run."""
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT run_id,server_name,tool_name,redacted_arguments_json,result_status,"
                "duration_ms,approver,result_reference,created_at FROM tool_calls "
                "WHERE run_id=:run ORDER BY created_at DESC LIMIT :limit"
            ), {"run": run_id, "limit": limit}).mappings().all()
            return [dict(row) for row in rows]

    def recent_runs(self, client_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT r.run_id, r.audit_id, r.model, COALESCE((SELECT a.decision FROM approvals a WHERE a.run_id=r.run_id ORDER BY a.decided_at DESC LIMIT 1), r.status) AS status, r.created_at FROM triage_runs r WHERE r.client_id=:client ORDER BY r.created_at DESC LIMIT :limit"), {"client": client_id, "limit": limit}).mappings().all()
            return [dict(row) for row in rows]

    def approved_drafts(self, client_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return approved or edited suggestions with their stored evidence for review."""
        statement = text("""SELECT a.approval_id, a.run_id, a.decision, a.reason,
                    a.approver, a.decided_at, i.issue_id, i.title, i.description,
                    i.submitted_by, r.client_id, r.audit_id, r.input_json,
                    COALESCE(a.final_suggestion_json, a.edited_fields_json, r.suggestion_json) AS suggestion_json,
                    r.retrieved_json, j.issue_key AS jira_issue_key, j.issue_url AS jira_issue_url,
                    (SELECT t.result_status FROM tool_calls t WHERE t.run_id=r.run_id AND t.tool_name='create_issue_draft' ORDER BY t.created_at DESC LIMIT 1) AS jira_status,
                    (SELECT t.result_status FROM tool_calls t WHERE t.run_id=r.run_id AND t.tool_name='send_business_owner_email' ORDER BY t.created_at DESC LIMIT 1) AS email_status
                FROM approvals a
                JOIN triage_runs r ON r.run_id=a.run_id
                JOIN issues i ON i.issue_id=r.issue_id
                LEFT JOIN jira_drafts j ON j.run_id=r.run_id
                WHERE r.client_id=:client
                    AND a.decision IN ('approve', 'edit')
                    AND COALESCE(a.final_suggestion_json, a.edited_fields_json, r.suggestion_json) IS NOT NULL
                ORDER BY a.decided_at DESC
                LIMIT :limit""")
        with self.engine.connect() as conn:
            rows = conn.execute(statement, {"client": client_id, "limit": limit}).mappings().all()
        result = []
        for row in rows:
            item = dict(row)
            item["input"] = json.loads(item.pop("input_json") or "{}")
            item["suggestion"] = json.loads(item.pop("suggestion_json"))
            item["retrieved"] = json.loads(item.pop("retrieved_json") or "{}")
            result.append(item)
        return result

    def recent_scoping_runs(self, client_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            if client_id:
                statement = "SELECT scope_run_id, client_id, audit_id, rau_id, audit_period, created_at FROM scoping_runs WHERE client_id=:client ORDER BY created_at DESC LIMIT :limit"
                params = {"client": client_id, "limit": limit}
            else:
                statement = "SELECT scope_run_id, client_id, audit_id, rau_id, audit_period, created_at FROM scoping_runs ORDER BY created_at DESC LIMIT :limit"
                params = {"limit": limit}
            rows = conn.execute(text(statement), params).mappings().all()
            return [dict(row) for row in rows]

    def scoping_details(self, scope_run_id: str, client_id: str | None = None, audit_id: str | None = None) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            filters = ["scope_run_id=:run"]
            params: dict[str, Any] = {"run": scope_run_id}
            if client_id:
                filters.append("client_id=:client")
                params["client"] = client_id
            if audit_id:
                filters.append("audit_id=:audit")
                params["audit"] = audit_id
            row = conn.execute(text(f"SELECT * FROM scoping_runs WHERE {' AND '.join(filters)}"), params).mappings().first()
            if not row:
                return None
            result = dict(row)
            result["decisions"] = [dict(item) for item in conn.execute(text("SELECT cluster_id,decision,reason,approver,decided_at FROM scoping_decisions WHERE scope_run_id=:run ORDER BY decided_at"), {"run": scope_run_id}).mappings().all()]
            return result

    def run_details(self, run_id: str, client_id: str, audit_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM triage_runs WHERE run_id=:run AND client_id=:client AND audit_id=:audit"), {"run": run_id, "client": client_id, "audit": audit_id}).mappings().first()
            if not row:
                return None
            details = dict(row)
            details["events"] = [dict(item) for item in conn.execute(text("SELECT event_type,payload_json,created_at FROM audit_events WHERE run_id=:run ORDER BY created_at"), {"run": run_id}).mappings().all()]
            details["approvals"] = [dict(item) for item in conn.execute(text("SELECT decision,edited_fields_json,final_suggestion_json,reason,approver,decided_at FROM approvals WHERE run_id=:run"), {"run": run_id}).mappings().all()]
            return details

    def scoping_summary_source(self, scope_run_id: str, client_id: str, audit_id: str) -> dict[str, Any] | None:
        """Return a persisted scoping snapshot only when its tenant and audit context match."""
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT scope_run_id,client_id,audit_id,rau_id,pack_json FROM scoping_runs WHERE scope_run_id=:run AND client_id=:client"), {"run": scope_run_id, "client": client_id}).mappings().first()
        if not row or str(row["audit_id"] or "") != str(audit_id or ""):
            return None
        return {**dict(row), "pack": json.loads(row["pack_json"] or "{}")}

    def save_summary_document(self, document: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO summary_documents VALUES (:id,:flow,:run,:client,:audit,:rau,:template,:issues,:location,:created)"), {
                "id": document["document_id"], "flow": document["flow_id"], "run": document["created_by_run_id"],
                "client": document["client_id"], "audit": document.get("audit_id", ""),
                "rau": document.get("rau_id") or "", "template": document["template"],
                "issues": self._json(document["source_issue_ids"]), "location": document["location"],
                "created": document["created_at"],
            })
            conn.execute(text("INSERT INTO summary_document_versions VALUES (:version_id,:document_id,:version,:content,:run,:created)"), {
                "version_id": str(uuid.uuid4()), "document_id": document["document_id"],
                "version": int(document["version"]), "content": self._json(document),
                "run": document["created_by_run_id"], "created": document["created_at"],
            })
        self.append_event(document["created_by_run_id"], "summary_document_draft_created", {
            "document_id": document["document_id"], "version": document["version"],
            "client_id": document["client_id"], "audit_id": document.get("audit_id", ""),
            "template": document["template"], "source_issue_ids": document["source_issue_ids"],
        })

    def summary_document(self, document_id: str, client_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(text("""SELECT d.*,v.version,v.content_json,v.created_by_run_id,v.created_at AS version_created_at,
                (SELECT decision FROM summary_document_decisions x WHERE x.document_id=d.document_id AND x.version=v.version ORDER BY decided_at DESC LIMIT 1) AS latest_decision
                FROM summary_documents d JOIN summary_document_versions v ON v.document_id=d.document_id
                WHERE d.document_id=:id AND d.client_id=:client
                AND v.version=(SELECT MAX(v2.version) FROM summary_document_versions v2 WHERE v2.document_id=d.document_id)"""), {"id": document_id, "client": client_id}).mappings().first()
        if not row:
            return None
        item = dict(row)
        content = json.loads(item.pop("content_json"))
        decision = item.pop("latest_decision")
        content["status"] = decision if decision in {"approved", "rejected"} else "draft"
        return {**content, "created_at": item["created_at"], "versions": self.summary_document_versions(document_id)}

    def summary_document_versions(self, document_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT version,content_json,created_at FROM summary_document_versions WHERE document_id=:id ORDER BY version"), {"id": document_id}).mappings().all()
        return [{"version": row["version"], "content": json.loads(row["content_json"]), "created_at": row["created_at"]} for row in rows]

    def summary_documents(self, client_id: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            if client_id:
                rows = conn.execute(text("SELECT document_id,client_id FROM summary_documents WHERE client_id=:client ORDER BY created_at DESC LIMIT :limit"), {"client": client_id, "limit": limit}).all()
            else:
                rows = conn.execute(text("SELECT document_id,client_id FROM summary_documents ORDER BY created_at DESC LIMIT :limit"), {"limit": limit}).all()
        return [item for row in rows if (item := self.summary_document(row[0], row[1]))]

    def record_summary_document_decision(
        self, document_id: str, client_id: str, version: int, decision: str, reason: str,
        approver: str, approver_role: str, edited_document: dict[str, Any] | None = None,
    ) -> int:
        if decision not in {"approved", "rejected", "edit"}:
            raise ValueError("Document decision must be approved, rejected, or edit.")
        created = datetime.now(timezone.utc).isoformat()
        with self.engine.begin() as conn:
            doc = conn.execute(text("SELECT run_id FROM summary_documents WHERE document_id=:id AND client_id=:client"), {"id": document_id, "client": client_id}).mappings().first()
            latest = conn.execute(text("SELECT MAX(version) FROM summary_document_versions WHERE document_id=:id"), {"id": document_id}).scalar()
            prior = conn.execute(text("SELECT decision FROM summary_document_decisions WHERE document_id=:id AND version=:version ORDER BY decided_at DESC LIMIT 1"), {"id": document_id, "version": version}).scalar()
            if not doc or latest != version or prior:
                raise ValueError("This document version is no longer pending a decision.")
            conn.execute(text("INSERT INTO summary_document_decisions VALUES (:id,:document,:version,:decision,:reason,:approver,:role,:edited,:created)"), {
                "id": str(uuid.uuid4()), "document": document_id, "version": version,
                "decision": decision, "reason": reason, "approver": approver,
                "role": approver_role, "edited": self._json(edited_document) if edited_document else None,
                "created": created,
            })
            new_version = version
            if decision == "edit":
                if not edited_document:
                    raise ValueError("An edited document version is required.")
                new_version = version + 1
                edited_document = {**edited_document, "version": new_version, "status": "draft", "created_at": created}
                conn.execute(text("INSERT INTO summary_document_versions VALUES (:version_id,:document_id,:version,:content,:run,:created)"), {
                    "version_id": str(uuid.uuid4()), "document_id": document_id, "version": new_version,
                    "content": self._json(edited_document), "run": doc["run_id"], "created": created,
                })
        self.append_event(doc["run_id"], "summary_document_decision", {
            "document_id": document_id, "version": version, "decision": decision,
            "approver": approver, "approver_role": approver_role, "reason": reason,
            "new_version": new_version if decision == "edit" else None,
        })
        return new_version

    def save_scoping_run(self, scope_run_id: str, client_id: str, audit_id: str, rau_id: str, period: str, inputs: Any, pack: Any) -> None:
        created = datetime.now(timezone.utc).isoformat()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO scoping_runs VALUES (:id,:client,:audit,:rau,:period,:inputs,:pack,:created)"), {
                "id": scope_run_id, "client": client_id, "audit": audit_id, "rau": rau_id, "period": period,
                "inputs": self._json(inputs), "pack": self._json(pack), "created": created,
            })
        self.append_event(scope_run_id, "scoping_pack_created", {"client_id": client_id, "audit_id": audit_id, "rau_id": rau_id, "pack": pack})

    def record_scoping_decision(self, scope_run_id: str, cluster_id: str, decision: str, reason: str, approver: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO scoping_decisions VALUES (:id,:run,:cluster,:decision,:reason,:approver,:created)"), {
                "id": str(uuid.uuid4()), "run": scope_run_id, "cluster": cluster_id, "decision": decision,
                "reason": reason, "approver": approver, "created": datetime.now(timezone.utc).isoformat(),
            })
        self.append_event(scope_run_id, "scoping_cluster_decision", {"cluster_id": cluster_id, "decision": decision, "reason": reason, "approver": approver})

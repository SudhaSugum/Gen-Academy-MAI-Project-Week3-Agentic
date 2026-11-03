# Issue Triage Assistant Product Specification

**Product version:** V2 concept build  
**Audience:** Internal audit teams, product reviewers, and engineers  
**Data boundary:** Synthetic fixtures only. Never enter real client names, issues, or evidence.

## Product summary

The Issue Triage Assistant helps auditors find relevant synthetic precedent, controls, and policy, then prepare a cited issue suggestion for human review. It uses a stateful, workflow-gated LangGraph design: defined graph nodes select the next step, perform retrieval and validation, pause for human decisions, and handle optional post-approval integrations. The model does not choose arbitrary tools or execute an open-ended tool loop.

The assistant suggests severity, root cause, control mapping, duplicate status, confidence, and remediation. The auditor owns the decision. A saved local approval can trigger a Jira draft and a configured business-owner email through fixed, separately governed MCP servers. Nothing is sent before the human approval and gate checks. Jira has no transition, final assignment, or close tool.

## Problem and users

Audit issues can describe the same condition in different words, leading to duplicate records, inconsistent severity, incorrect control mappings, lost remediation precedent, and recommendations without a verifiable source trail. Auditors need a faster way to find relevant precedent and inspect its basis while keeping their decision authority.

- **Auditor:** provides issue or RAU context, examines sources, and approves, edits, rejects, or decides a scoping cluster.
- **Reviewer or lead auditor:** reviews the saved decision and may override the control-version check only when allowlisted and when a reason is recorded.
- **Operator:** configures model, Pinecone, local persistence, MCP services, secrets, and deployment safeguards.

## Product principles

1. The auditor owns severity, scope, and disposition.
2. Every supported suggestion claim cites retrieved source material from the current run.
3. Citation validity is a mechanical check; exact quote presence does not prove semantic support.
4. Weak retrieval or invalid citations are retrieval limitations, not evidence-sufficiency judgements.
   If the model still returns an invalid duplicate-warning label after bounded recovery, that field is downgraded to `Insufficient evidence` without citations; supported fields may proceed through the ordinary human gate.
   The UI explains that no recommendation is available for review, suggests improving the query/context, and keeps raw citation-validation details collapsed for troubleshooting. If duplicate-warning recovery still returns an unsupported label, that field becomes uncited `Insufficient evidence` so a model formatting error does not invent a duplicate state.
5. External writes are fixed workflow steps and occur only after human approval.
6. Examples and runtime data in this concept build remain synthetic.
7. One inspectable workflow is preferred to an unaccountable agent swarm.

## User flows

### Single-issue triage

**Inputs:** issue title and description; synthetic client; selected audit; required RAU ID and tested attributes; auditor identity; optional audit context and control hint.

**Retrieval:** hybrid semantic and keyword retrieval over synthetic past issues, control documents, and policy documents. Filters are applied before ranking. Duplicate screening uses records from the selected exact audit; related precedent may come from other audits for the same client and RAU. An auditor hint helps retrieve but is not evidence.

**Output:** suggested Critical/High/Medium/Low severity and rationale; root-cause category; mapped control ID; duplicate status; up to five similar issues and outcomes; cited remediation steps with generic owner roles and supported due-date suggestions; confidence, retrieval state, and source citations.

**Review:** a Pydantic-typed pre-approval checklist is evaluated before review is offered. The auditor may approve, edit, or reject. Edits are revalidated and persisted as the final suggestion. Rejection requires a reason. A human decision is durable in the audit database and graph checkpoint.

### RAU scoping

**Inputs:** required synthetic client and RAU ID; optional audit, period, RAU name, control IDs, and attributes.

**Retrieval and output:** retrieval is centered on the RAU and can include same-client precedent from other audits. A recognized year in the audit period filters to matching client audit IDs. Past issues are clustered by control, attribute, and root cause, with supporting source chunks, lookback information, and older excluded items shown separately.

**Human decision:** each cluster pauses for an auditor choice (approve, de-scope, or escalate) and a reason. The summary-document expander remains visible and explains its prerequisites. Once all cluster decisions are recorded and the run contains retrieved past-issue sources, an auditor may request a deterministic cited summary document draft from that source set. The screen shows document metadata and PDF controls, not the draft body. The full draft and citations are opened in the in-app PDF viewer or downloaded as a PDF attachment; the separate edit decision retains editable fields. The document has a separate human approval/edit/reject workflow and is not published or shared.

### Run-scoped chat

A chat panel is visible in both the triage and RAU scoping tabs. Before a run, question entry is disabled. After retrieval, it answers follow-ups only from that run's sources, cites each factual claim, and cannot alter recommendations or decisions. Chat history remains scoped to its run. Each workflow also exposes the redacted MCP call history for its active run; the saved approved-issue view keeps post-approval tool calls accessible after the review checkpoint closes.

### Approved draft history

Approved and edited issue records are visible in a collapsible history section. Opening an issue title shows original input, final accepted suggestion, citations, similar issues, retrieval snapshot, and reviewer decision. Recent audit events and tool-call logs are available in collapsible sections.

### Post-approval Jira and email

The fixed sequence is: draft suggestion, pre-approval checks, email destination/configuration verification, durable human approval/edit, Jira draft creation, business-owner email, and visible outcome. No Jira or email call occurs before approval. The Jira and email representations are formatted from the persisted final approved or edited issue, include issue context, triage assessment, remediation and source citations, and link back to the run/approval. The recipient is configured outside the model. Mailpit is the local default; SMTP is an explicit opt-in. Jira/email failures do not undo local approval, and explicit human retry is available from approved history.

## Pre-approval checks

| Check | Required result | Override |
| --- | --- | --- |
| C01 Citation completeness | Every supported severity, root cause, control, and remediation claim has a citation; fields explicitly marked insufficient evidence may remain uncited | None |
| C02 Citation validity | Each citation belongs to a source retrieved this run; quote matches exactly | None |
| C03 Control validity/version | Control is in RAU scope and version matches live V2 data or indexed V1 data with a version note | Allowlisted lead auditor only, with reason; mark resulting draft superseded |
| C04 RAU scope | Client, audit, RAU, and attributes match; no cross-client or cross-RAU issue enters the set | None |
| C05 Duplicate screening | Local synthetic check for same RAU, control, root cause, and open-ticket status | None; exact duplicate blocks approval |
| EV1 Email verification | Configured address/backend is syntactically and operationally ready | Not a write; never sends before approval |

Approve/edit is enabled only when every check passes, a warning is acknowledged, or C03 is validly lead-overridden. Any non-overridable failure blocks approval. C05 is local-only and makes no Jira call.

## Agentic workflow and integrations

The workflow has explicit state, named graph nodes, checkpointed human interrupts, bounded citation-aware recovery, and structured errors. It uses hybrid Pinecone/BM25 retrieval and one permitted citation repair rather than making the model choose arbitrary tools. In this branch, controls and policies are retrieved through the RAG pipeline; separate live Controls and Evidence MCP servers are not implemented. Jira, email, and document MCP failures are visible and do not undo saved local decisions. Per-run MCP call history is available in both single-issue triage and RAU scoping; the global audit view remains available for cross-run review.

MCP capabilities implemented in this branch are:

- **Jira:** create a labeled issue draft from a persisted, approved suggestion. No transition, final assignment, or close tool is exposed. The Jira server rechecks approval and citations before creating the draft.
- **Documents:** create a cited summary-document draft from validated in-run sources. No publish/share/send operation is exposed.
- **Email:** send the configured business-owner notification only after the Jira draft exists and approval is verified.

Separate live Controls and Evidence MCP servers, including control-history and evidence-metadata tools, are not implemented in this branch. Controls and policies are retrieved through Pinecone and local BM25; no raw evidence is downloaded or judged. Each implemented MCP call uses an allowlisted tool name, client scope where applicable, timeouts and bounded retry behavior, sanitized errors, and append-only audit metadata with redacted arguments and approver context for write-shaped calls. Secrets never enter prompts, graph state, or logs. The graph controls call order.

## Retrieval, citations, and data

- Pinecone has separate issue, control, and policy namespaces.
- Pinecone semantic retrieval is combined with local-corpus BM25 keyword retrieval by Reciprocal Rank Fusion. Scores are diagnostics, not confidence probabilities.
- Metadata filtering is applied before ranking. Same-client and applicable audit/RAU/period rules are preserved.
- `RecursiveCharacterTextSplitter` is used for source chunking while retaining source and chunk metadata.
- The deterministic synthetic generator creates issue records, control Word documents, and policy PDFs. Ingestion extracts, chunks, embeds, and upserts them to Pinecone.
- SQLAlchemy stores run inputs, retrieval snapshots, approvals, audit events, scoping decisions, documents, and tool-call records. SQLite supports local learning; Postgres is scaffolded for hosted use.
- Optional Mem0 stores only concise approved/corrected patterns scoped to auditor and synthetic client. Memory is not citable and cannot receive raw issue text, chunks, or unapproved drafts.

A citation validator checks current-run retrieval membership, source identity/type where required, and exact quote match. One bounded repair may be attempted. If retrieval is weak or validation still fails, the assistant shows insufficient evidence and does not route an unsupported suggestion to human approval.

## Out of scope

- Real client data, production tenant authorization, or a final severity decision.
- Automatic approval, closure, final assignment, transition, or filing.
- Jira issue creation or email delivery before a durable human approval.
- Downloading or judging raw evidence, or evidence-sufficiency decisions.
- An LLM-selected tool sequence, recipient, or arbitrary agent loop.
- Jira transitions, final assignment, and close tools.
- Publishing or sharing summary documents.
- A production-evaluated severity model or a multi-agent swarm.

## Acceptance criteria

- Every supported suggestion is backed by current-run source IDs, chunk IDs, and verbatim quotes.
- Unsupported retrieval/citation results cannot reach approval as a grounded suggestion.
- Exact-audit duplicate candidates stay distinct from cross-audit precedent.
- C01-C05 and EV1 are rechecked before a durable approval; non-overridable failures block it.
- The persisted edited suggestion, not the original model draft, supplies Jira/email content.
- No Jira or email write runs before approval; Jira's exposed tools cannot close, transition, or finally assign an issue.
- Implemented Jira/email/document MCP calls have redacted audit metadata; integration failures remain visible without undoing local decisions. RAG retrieval remains available independently of those services.
- Run chat stays source-grounded and non-mutating; scoping cluster decisions record a reason.
- Summary document drafts cite only validated in-run data and wait for a separate human decision.

## Production gates

Before real client data is considered, implement and verify identity-to-client/audit authorization on every read/write path; complete privacy, retention, security, backup, incident, and vendor reviews; adopt deployment-managed secrets; commit a resolved dependency lock; validate migrations and restore procedures; add operational monitoring; and independently evaluate retrieval, severity, prompt, and citation relevance against approved labeled data. OIDC sign-in alone does not provide tenant authorization.

## Demo materials

- [Demo scenarios and sample inputs](demo_scenarios.md) cover triage, review, scoping, chat, documents, integrations, and weak retrieval.
- [4–5 minute demo script](demo_script_4min.md) provides a timed business and technical walkthrough.

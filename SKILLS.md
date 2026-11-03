# Project skills and architecture guide

This guide describes the current V2 synthetic Issue Triage Assistant implementation. Use [`docs/demo_scenarios.md`](docs/demo_scenarios.md) for copy-ready inputs and [`docs/demo_script_4min.md`](docs/demo_script_4min.md) for a timed demonstration. Durable contributor constraints are in [`AGENTS.md`](AGENTS.md), product requirements in [`docs/product_spec.md`](docs/product_spec.md), and model behavior in [`docs/system_prompt.md`](docs/system_prompt.md).

## Product flows

- **Single issue:** intake requires synthetic client/audit, RAU, and tested attributes. The graph retrieves exact-audit duplicate candidates separately from same-client cross-audit precedent, drafts cited claims, validates/repairs citations, and pauses for an auditor decision.
- **RAU scoping:** RAU is primary; client is required; audit, period, control, and attributes are optional refinements. Scoping clusters precedent and records a human decision and reason for each cluster.
- **Run chat:** the panel is visible in each workflow; before a run, its input is disabled. After retrieval, it answers only from that run's chunks, cites facts, and cannot mutate recommendations or decisions.
- **Cited documents:** a fixed document graph creates summary drafts from validated retrieved sources. The screen shows metadata and PDF controls, not the draft body; the full cited draft is available in the PDF viewer or as a downloaded attachment. Editable fields remain in the separate edit decision. Human approval/edit/reject is separate, and no publish/share tool exists.
- **Issue approval and delivery:** C01-C05 and EV1 are checked before approval. The durable approve/edit decision is saved locally; only then can fixed graph nodes create a Jira draft and send a notification to the configured owner. Jira/email content comes from the persisted final approved version and has structured citations.
- **History:** approved issue titles open full details; recent audit history and approved issues are collapsible. Per-run MCP call history is available in both triage and scoping, including post-approval calls from saved issues.

## Retrieval and citation practices

- Load generated synthetic issues, Word controls, and PDF policies; preserve source IDs, chunk IDs, and metadata during extraction and chunking.
- Use Pinecone for semantic retrieval, local BM25 for keyword retrieval, and RRF for rank fusion. Filters apply before ranking.
- Explain similarity, BM25, overlap, and RRF as retrieval diagnostics; never as confidence probabilities.
- Keep citation membership, source identity/type, and exact quote validation explicit. Exact text presence does not prove semantic support; the auditor checks relevance.
- Use `Insufficient evidence` for weak retrieval or invalid citations. It is a retrieval limitation, not a judgement about audit evidence sufficiency.
- After bounded recovery, map an invalid duplicate-warning label to uncited `Insufficient evidence`; never infer a duplicate state from a model formatting error.
- Present weak-retrieval and citation-validation failures in plain language with a useful retry hint; keep raw chunk-level diagnostics in a collapsed troubleshooting area.
- Keep summary-document creation visible in RAU scoping. Explain that all cluster decisions and retrieved past-issue sources are prerequisites; never imply drafts are created automatically.

## Graph and tool conventions

- Keep triage in the single explicit LangGraph workflow and RAU scoping in its own explicit graph. Use typed state/models, checkpoints, bounded recovery, and named nodes.
- Graph order controls tool calls. The LLM must not choose arbitrary MCP tools or tool order.
- Expose only allowlisted MCP tools. Reads can occur before approval; Jira writes and owner email occur after the durable human decision only.
- Tenant scope, timeouts, bounded retry, structured/sanitized errors, environment secrets, and redacted tool-call audit rows are required.
- Jira has no transition, final assignment, or close operation. Email uses only the configured owner recipient. Summary documents are drafts without publish/share/send operations.
- Jira/email/document MCP failures leave the local run/decision inspectable and show a visible outcome. Issue/control/policy retrieval uses Pinecone and BM25 in this branch; live Controls and Evidence MCP servers are not implemented.

## Persistence and synthetic data

- SQLAlchemy stores runs, decisions, snapshots, audit events, scoping records, summary-document versions, and tool calls. SQLite is for local learning; Postgres is scaffolded.
- Mem0 is optional and scoped to auditor + synthetic client. Store only concise approved/corrected patterns; never raw issue content, evidence, or unapproved drafts.
- Keep generator definitions deterministic. `data/synthetic/policies/*.md` is source corpus material and must not be casually changed as product documentation.
- Do not add real client examples or data to fixtures, prompts, logs, or docs.

## Local learning workflow

```bash
python scripts/generate_synthetic_data.py
python -m src.issue_triage.ingestion.cli_ingest
streamlit run app.py
```

See [`README.md`](README.md) for environment setup, [`docs/demo_scenarios.md`](docs/demo_scenarios.md) for sample flows, and [`docs/demo_script_4min.md`](docs/demo_script_4min.md) for the presentation script.

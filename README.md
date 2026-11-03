# Issue Triage Assistant V2 concept build

An evidence-grounded, workflow-gated decision-support app for internal auditors. The auditor provides a synthetic issue or RAU context; the system retrieves similar synthetic issues, control records, and policy passages; then prepares a cited recommendation or scoping pack for human review. The auditor owns severity and disposition. This concept build is synthetic-only.

## What the system does

- **Single-issue triage:** hybrid retrieval suggests severity, root cause, control, duplicate status, remediation, and confidence with citations. Claims explicitly marked `Insufficient evidence` may be uncited; C01 requires citations for supported values.
  If retrieval or citation validation cannot ground a draft, the UI provides a plain-language explanation and retry guidance; detailed validator errors remain available in a collapsed diagnostic panel.
  Invalid duplicate-warning labels are downgraded to an uncited `Insufficient evidence` field after the bounded repair attempt rather than blocking otherwise grounded suggestions.
- **RAU scoping:** retrieves same-client precedent, groups issues into control/attribute/root-cause clusters, and pauses for a human decision and reason on each cluster.
- **Run chat:** always-visible chat panel in both workflows; becomes enabled once a run has retrieved sources and cites factual answers from that run.
- **Cited summary documents:** creates internal drafts from the scoping run's retrieved issue sources, validates claims/citations, and pauses for a separate human decision. The screen shows document metadata and PDF controls, not the draft body. Auditors open the full cited draft in the in-app PDF viewer or download it as an attachment; editable fields remain available in the separate edit decision.
- **Approval-gated Jira and email:** after all pre-approval gates pass and an auditor approves or edits, fixed workflow nodes create a Jira draft and notify the configured business owner. The final accepted suggestion is formatted with citations. No write occurs before approval.
- **Auditability:** SQL stores runs, retrieval snapshots, decisions and tool-call history. Per-run MCP history is visible in both triage and RAU scoping; approved issue history, recent audit trail, and the cross-run MCP log are collapsible.
- **Recovery UX:** weak retrieval and citation failures show a plain-language explanation and retry guidance, with technical validator details collapsed. Invalid duplicate-warning labels are downgraded to uncited `Insufficient evidence` after bounded recovery.

No Jira transition, final assignment, or close tool is exposed. No action auto-closes or auto-files an issue. External actions after approval are visible and auditable, and failures do not reverse the local human decision.

## Quick start

Requires Python 3.11-3.13.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` and `PINECONE_API_KEY` in `.env`; configure the Pinecone index/dimension as documented in `.env.example`. `MEM0_API_KEY` is optional. Generate and ingest the synthetic corpus, then start Streamlit:

```bash
python scripts/generate_synthetic_data.py
python -m src.issue_triage.ingestion.cli_ingest
streamlit run app.py
```

For local email demos, Mailpit captures messages without external delivery. Start `docker compose -f docker-compose.mailpit.yml up -d`; open the Mailpit UI at `http://localhost:8025`. Jira and SMTP require separate credentials and a non-production sandbox. Never place secrets in source files, prompts, graph state, or logs.

## Demo preparation and examples

- Follow [demo scenarios and sample inputs](docs/demo_scenarios.md) to exercise triage, duplicate screening, chat, scoping, cluster decisions, cited document review, and post-approval integrations.
- Use the [4–5 minute demo script](docs/demo_script_4min.md) for a timed business and technical presentation.

The samples use only generated `synthetic-client-a`/`synthetic-client-b` data. Retrieval/model outputs may vary; the auditor checks citations and decides.

## Architecture

Streamlit provides the UI; LangChain handles prompts and typed structured output; explicit LangGraph workflows coordinate retrieval, citation validation/recovery, approval interrupts, and fixed MCP calls. Pinecone semantic search and local BM25 keyword search are fused with RRF. SQLAlchemy persists audit records in SQLite locally, with a Postgres deployment scaffold. Governed MCP servers currently isolate Jira draft creation, owner email, and cited summary-document drafts. Controls and policies are retrieved through Pinecone plus local BM25; separate live Controls and Evidence MCP servers are not implemented in this branch. The model cannot select an arbitrary tool sequence.

See [`docs/product_spec.md`](docs/product_spec.md) for the detailed behavior and scope, [`docs/system_prompt.md`](docs/system_prompt.md) for model rules, [`SKILLS.md`](SKILLS.md) for architecture and contributor guidance, and [`AGENTS.md`](AGENTS.md) for durable repository constraints.

## Demo and project documents

The project overview is available as [Word](docs/Issue_Triage_Assistant_Project_Document.docx) and [PDF](docs/Issue_Triage_Assistant_Project_Document.pdf). It describes product scope, workflow gates, citation-aware recovery, MCP history, synthetic demo inputs, and deployment limits. [Demo scenarios](docs/demo_scenarios.md) provide copy-ready inputs for each flow; the [4–5 minute script](docs/demo_script_4min.md) is ready to present.

## Real-client boundary

This concept is **not ready for real client information**. Before that, add and verify tenant authorization on every retrieval and database path, implement deployment-managed secrets, complete privacy/security/operational reviews, resolve dependencies, and independently evaluate retrieval, severity calibration, prompt behavior, and citation relevance. See the production gates in the product specification.

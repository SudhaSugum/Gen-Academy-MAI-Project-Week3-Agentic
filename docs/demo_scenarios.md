# Demo Scenarios and Sample Inputs

All examples below are synthetic and designed around the generated corpus. Model ranking can vary; inspect the retrieved citations and let the auditor make the decision. Do not enter real client information.

## Before the demo

1. Generate/index synthetic content and start the app (see README).
2. Confirm the expected Pinecone index and namespaces are populated.
3. Use Mailpit for email capture. Open its local UI at `http://localhost:8025`.
4. If demonstrating Jira, use a non-production project and a permitted demo auditor. Verify owner address/backend configuration before the live run.
5. Jira/email calls are real writes to their configured demo endpoints, but they occur only after an auditor approves. If you do not want an external Jira write, skip that integration step and demonstrate local approval/history instead.

## Flow 1: Single-issue retrieval and cited suggestion

In **Single-issue triage**, enter:

| Field | Sample |
| --- | --- |
| Issue title | Quarterly access recertification was still incomplete at cutoff |
| Issue description | At the review cutoff, several sampled application accounts had no completed quarterly recertification sign-off. Continued business need could not be confirmed for those accounts. Scope is limited to the synthetic-client-a identity and access sample. |
| Synthetic client | `synthetic-client-a` |
| Synthetic audit | `synthetic-audit-iam-2026` |
| Audit context | FY2026 identity and access review |
| Optional control hint | `AC-04` |
| RAU ID | `RAU-AC-01` |
| Attributes tested | `completeness` |

Click **Retrieve and draft for review**. Expand retrieved sources and check for relevant past issue, control, and policy chunks. Open citations and compare the quotes with their chunks. The wording is deliberately different from the synthetic past issue title to demonstrate semantic retrieval alongside keyword search. AC-04 covers completeness, so keep this triage sample scoped to `completeness`. For a separate timeliness example, use title `Former staff access was disabled late`, describe an HR-to-identity handoff delay, set control hint `AC-07`, and set the tested attribute to `timeliness`. Do not combine attributes from unrelated controls in a single-issue run.

## Flow 2: Duplicate screen versus cross-audit precedent

Use the Flow 1 input. Inspect **Duplicate warning** and the separate exact-audit duplicate candidates. Explain that only an open candidate from the selected audit can block under C05; a closed item or similar item from another audit is precedent, not an exact-audit open duplicate. C05 uses the local synthetic store and makes no Jira request.

For a clearer alternate-audit comparison, change the audit to `synthetic-audit-iam-2025` and keep the same client/RAU/control hint. Compare which audit provides duplicate candidates versus which older/sibling audit issues serve as precedent. Verify the current corpus status before promising a specific candidate in a live presentation.

## Flow 3: Human edit and approve/reject

After a grounded Flow 1 suggestion, expand **MCP call history · this triage run** to inspect per-run MCP activity; before approval it should explain that Jira and email tools have not run. After a saved decision, inspect the MCP call history shown immediately below the outcome; the same run-specific log is also available from the sidebar audit record and approved issue history. Then inspect every C01-C05 and EV1 result. The UI prevents approve/edit until checks pass, warnings are acknowledged, or a permitted lead override is recorded. For an edit demo, choose **Edit**, revise the readable fields in the dialog, provide a decision reason, and submit. The graph revalidates the edited claims and citations. The saved approved version drives subsequent Jira/email formatting.

For a rejection demo, start a separate run, choose **Reject**, enter reason `Synthetic demo: reviewer requests additional control-owner confirmation`, and submit. Confirm the decision is saved and no Jira/email write happens.

## Flow 4: RAU scoping and cluster decision

In **RAU scoping**, enter:

| Field | Sample |
| --- | --- |
| Synthetic client | `synthetic-client-a` |
| RAU ID | `RAU-AC-01` |
| Audit context (optional) | `synthetic-audit-iam-2026` |
| RAU name | `Application access recertification` |
| Audit period | `2026` |
| Control IDs in scope | `AC-02, AC-04, AC-07` |
| Attributes to test | `authorization, completeness, timeliness` |

Click **Build cited scoping pack**. Point out the audit-year filter, hybrid retrieval diagnostics, clusters, citation source chunks, and any older issues excluded by lookback. Expand **MCP call history · this RAU scoping run** to inspect tool calls and see when no MCP tools were needed. Decide one cluster and record a reason, for example `Retain for testing: periodic sign-off and escalation are relevant to this RAU`. Repeat until every cluster decision is recorded. Each cluster is a separate human checkpoint. Summary documents are not created automatically; after all decisions, use the **Create a cited summary document** expander described in Flow 6. The empty **Summary documents** section means no draft has been saved for the selected client yet.

## Flow 5: Run chat follow-ups

After a triage or scoping run, use that run's chat panel:

- `List the audits represented in the retrieved issues.`
- `List the policy documents retrieved and what each one covers.`
- `What remediation approach worked for the previous access attestation issue?`
- `Which source supports the AC-04 mapping? Quote it.`
- `Show the retrieved 2026 audit context for this RAU.`

The answer should cite the run's current chunks. Ask a follow-up such as `Which of those was from the 2025 audit?` to show continuity. A question unsupported by the current run should receive an insufficient-evidence answer rather than an uncited fact.

## Flow 6: Cited summary document draft

After RAU clusters have human decisions, expand **Create a cited summary document**. Choose **RAU scoping pack**, retain the retrieved issue selection (or choose a subset), title it `Synthetic RAU access review summary`, leave remediation drafts unchecked for the first pass, and create the draft. The page shows its metadata and PDF controls; the draft body is not repeated on screen. Select **Open PDF view** to inspect the full draft and citations in the PDF viewer, or **Download PDF attachment** to save it. The separate **Edit** decision still exposes editable fields. Try **Single cluster summary** for a smaller output. The document flow creates a saved draft and never publishes or sends it.

## Flow 7: Post-approval Jira and email (optional)

Use a passing, reviewed triage run. Approve or approve with edits, using a reviewer authorized for Jira drafts. After the durable local approval, the fixed graph creates a Jira draft and then sends the configured owner email. Open the Jira issue and Mailpit message. Confirm both show the edited/final version, run/approval references, remediation steps, and citations. Check approved issue history and the recent audit/tool-call log.

If Jira is not configured, demonstrate the saved local approval and its explicit Jira-unavailable message instead. If an MCP server is unavailable, the V1-style triage remains inspectable and the UI reports the availability limitation. Do not claim an external write succeeded without the returned Jira/email status.

## Flow 8: Insufficient retrieval

Use a synthetic issue description about an unrelated topic absent from the corpus while keeping the required client, audit, RAU, and attributes valid. Run triage and observe whether retrieval/citation validation produces an explicit insufficient-evidence state. The exact outcome depends on the configured corpus/index and thresholds; do not force or bypass validation to make the demo pass. The UI should explain in plain language that the draft is unavailable for review, suggest adding issue detail or a relevant RAU/control hint, and keep raw chunk errors in **Technical details**. The correct behavior is no unsupported grounded suggestion and no approval path.

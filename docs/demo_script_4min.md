# Four to five minute product and technical demo script

## 0:00-0:35 | Business problem

“Auditors often encounter the same control issue described in different language. That can create duplicate records, inconsistent severity and control mapping, and remediation lessons that are hard to find. This assistant helps retrieve relevant precedent and show a reviewer the source trail, while leaving the decision with the auditor.”

## 0:35-1:25 | Single-issue retrieval

“Here I’ll enter a synthetic access-review issue for synthetic-client-a, audit 2026, and RAU-AC-01. Several quarterly access sign-offs were missing at cutoff. I’ll use AC-04 and the completeness attribute.”

Click **Retrieve and draft for review**. Show the retrieved issues, controls, and policies; point out semantic similarity, keyword overlap, and RRF rank. Open a citation.

“The wording differs from the precedent, so semantic retrieval helps find it; keyword search also contributes. These scores describe retrieval ranking, not probability. I check whether the quoted source actually supports the claim.”

## 1:25-2:15 | Citation recovery and approval gate

“Each supported field needs a citation. If retrieval or citation repair is weak, the UI says why the draft is unavailable in plain language, with technical chunk details tucked under Technical details. It does not judge audit evidence sufficiency. An invalid duplicate label is downgraded to Insufficient evidence rather than turned into a duplicate claim.”

Show the C01-C05 and EV1 checklist. Explain that C05 uses local exact-audit screening and does not call Jira. Demonstrate a human edit with a reason, or use a separate run to demonstrate rejection. A failed non-overridable check blocks approval; an eligible C03 override requires an allowlisted lead and reason.

## 2:15-3:10 | RAU scoping and document draft

“Scoping starts from the RAU. I’ll use RAU-AC-01 for 2026 and controls AC-02, AC-04, and AC-07, which cover authorization, completeness, and timeliness in this synthetic example.”

Click **Build cited scoping pack**. Show the year filter, cluster citations, and per-run MCP call history. Record a decision and reason for each cluster. After all decisions, expand **Create a cited summary document**. Create an RAU scoping pack draft if retrieved issue sources are available. The screen shows metadata and PDF controls; open the PDF to review the full cited draft, then demonstrate its separate review/edit decision. It is never published automatically.

## 3:10-3:45 | Run-scoped chat

“The chat uses only this run’s retrieved sources. I’ll ask, ‘What remediation approach worked for the previous access attestation issue?’ and open its citation. Follow-up questions work, but chat cannot edit the suggestion or record an approval.”

## 3:45-4:30 | Human-approved integrations and audit history

“Only after the local human approval and all gates pass can fixed workflow nodes create a Jira draft and notify the configured business owner. The payload uses the final approved or edited version. MCP calls appear in per-run history and the global audit view; redacted arguments are recorded. A local Mailpit setup can capture the email.”

## 4:30-5:00 | Close

“This is a synthetic concept build. The auditor remains responsible for severity and disposition, and nothing is auto-closed or auto-filed. Before real client use, tenant authorization, deployment controls, dependency locking, operational reviews, and independent retrieval and citation evaluation must be completed.”

## Presenter notes

- Retrieval output can vary with the configured index and thresholds. Do not promise a particular match or force a result.
- Use the controls and attributes together as shown: AC-02 / authorization, AC-04 / completeness, AC-07 / timeliness.
- If no issue sources were retrieved, the summary-draft expander explains why the document cannot be created for that run.
- Keep Jira optional; use a non-production board and the configured demo identity only.

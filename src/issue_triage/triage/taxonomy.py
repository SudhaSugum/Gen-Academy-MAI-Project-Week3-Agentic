"""Versioned severity rubric and root-cause taxonomy."""

SEVERITY_RUBRIC = """
Critical: credible immediate material harm, systemic compromise, or legally mandated escalation requiring urgent executive response.
High: significant exposure, control failure with plausible material impact, repeat failure, or unverified privileged access.
Medium: meaningful control deficiency with bounded scope and no identified material impact; timely remediation is needed.
Low: limited, isolated process weakness with low exposure and an effective compensating control.
If retrieved evidence does not support a level, return Insufficient evidence. Do not infer final audit severity.
""".strip()

ROOT_CAUSE_CATEGORIES = [
    "Governance and ownership", "Process design", "Process execution", "Access lifecycle handoff",
    "Monitoring and escalation", "Technology configuration", "Evidence retention",
    "Training and awareness", "Third-party oversight", "Capacity and resourcing",
    "Unclear policy or standard", "Other", "Insufficient evidence",
]

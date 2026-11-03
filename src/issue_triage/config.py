"""Environment-only application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    checkpoint_path: str
    checkpoint_backend: str
    checkpoint_database_url: str
    pinecone_api_key: str
    pinecone_index: str
    pinecone_cloud: str
    pinecone_region: str
    openai_api_key: str
    chat_model: str
    embedding_model: str
    embedding_dimensions: int
    retrieval_threshold: float
    keyword_overlap_threshold: float
    prompt_version: str
    mem0_api_key: str
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_project_key: str
    jira_issue_type: str
    mcp_auditor_ids: tuple[str, ...]
    mcp_lead_auditor_ids: tuple[str, ...]
    email_backend: str
    email_smtp_host: str
    email_smtp_port: int
    email_smtp_security: str
    email_smtp_username: str
    email_smtp_password: str
    email_sender: str
    business_owner_email: str

    @property
    def email_notification_ready(self) -> bool:
        common = bool(self.email_sender and self.business_owner_email)
        if self.email_backend == "mailpit":
            return common
        if self.email_backend == "smtp":
            return common and bool(self.email_smtp_host and self.email_smtp_username and self.email_smtp_password)
        return False


def get_settings() -> Settings:
    load_dotenv()
    email_backend = os.getenv("EMAIL_BACKEND", "mailpit").strip().lower()
    default_smtp_host = "127.0.0.1" if email_backend == "mailpit" else "smtp.gmail.com"
    default_smtp_port = "1025" if email_backend == "mailpit" else os.getenv("GMAIL_SMTP_PORT", "465")
    default_smtp_security = "none" if email_backend == "mailpit" else ("ssl" if default_smtp_port == "465" else "starttls")
    legacy_smtp_username = os.getenv("GMAIL_SENDER", "") if email_backend == "smtp" else ""
    legacy_smtp_password = os.getenv("GMAIL_APP_PASSWORD", "") if email_backend == "smtp" else ""
    default_email_sender = "issue-triage@localhost" if email_backend == "mailpit" else os.getenv("GMAIL_SENDER", "")
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/issue_triage.db"),
        checkpoint_path=os.getenv("CHECKPOINT_PATH", "./data/checkpoints.sqlite"),
        checkpoint_backend=os.getenv("CHECKPOINT_BACKEND", "sqlite").lower(),
        checkpoint_database_url=os.getenv("CHECKPOINT_DATABASE_URL", os.getenv("DATABASE_URL", "sqlite:///./data/checkpoints.sqlite")),
        pinecone_api_key=os.getenv("PINECONE_API_KEY", ""),
        pinecone_index=os.getenv("PINECONE_INDEX", "issue-triage-v1"),
        pinecone_cloud=os.getenv("PINECONE_CLOUD", "aws"),
        pinecone_region=os.getenv("PINECONE_REGION", "us-east-1"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        chat_model=os.getenv("CHAT_MODEL", "gpt-4o-mini"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "1536")),
        retrieval_threshold=float(os.getenv("RETRIEVAL_THRESHOLD", "0.45")),
        keyword_overlap_threshold=float(os.getenv("KEYWORD_OVERLAP_THRESHOLD", "0.12")),
        prompt_version=os.getenv("PROMPT_VERSION", "triage_v1"),
        mem0_api_key=os.getenv("MEM0_API_KEY", ""),
        jira_base_url=os.getenv("JIRA_BASE_URL", ""),
        jira_email=os.getenv("JIRA_EMAIL", ""),
        jira_api_token=os.getenv("JIRA_API_TOKEN", ""),
        jira_project_key=os.getenv("JIRA_PROJECT_KEY", ""),
        jira_issue_type=os.getenv("JIRA_ISSUE_TYPE", "Task"),
        mcp_auditor_ids=tuple(value.strip() for value in os.getenv("JIRA_DRAFT_ALLOWED_AUDITOR_IDS", os.getenv("MCP_AUDITOR_IDS", "")).split(",") if value.strip()),
        mcp_lead_auditor_ids=tuple(value.strip() for value in os.getenv("MCP_LEAD_AUDITOR_IDS", "").split(",") if value.strip()),
        email_backend=email_backend,
        email_smtp_host=os.getenv("EMAIL_SMTP_HOST", default_smtp_host).strip(),
        email_smtp_port=int(os.getenv("EMAIL_SMTP_PORT", default_smtp_port)),
        email_smtp_security=os.getenv("EMAIL_SMTP_SECURITY", default_smtp_security).strip().lower(),
        email_smtp_username=os.getenv("EMAIL_SMTP_USERNAME", legacy_smtp_username).strip(),
        email_smtp_password=os.getenv("EMAIL_SMTP_PASSWORD", legacy_smtp_password),
        email_sender=os.getenv("EMAIL_SENDER", default_email_sender).strip(),
        business_owner_email=os.getenv("BUSINESS_OWNER_EMAIL", ""),
    )

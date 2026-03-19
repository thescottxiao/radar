"""Shared ingestion schemas for email processing."""

from datetime import datetime

from pydantic import BaseModel, Field


class EmailContent(BaseModel):
    """Parsed email content passed through the ingestion pipeline."""

    message_id: str = Field(description="Unique message identifier (e.g., Gmail message ID)")
    from_address: str = Field(description="Sender email address")
    to_addresses: list[str] = Field(default_factory=list, description="Recipient email addresses")
    subject: str = Field(default="", description="Email subject line")
    body_text: str = Field(default="", description="Plain text body")
    body_html: str = Field(default="", description="HTML body (if available)")
    date: datetime | None = Field(default=None, description="Email send date")

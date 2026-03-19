from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://radar:radar@localhost:5432/radar"

    # LLM
    anthropic_api_key: SecretStr = SecretStr("")

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # Google Pub/Sub
    google_cloud_project: str = ""
    gmail_pubsub_topic: str = ""
    gmail_pubsub_subscription: str = ""

    # WhatsApp (Meta Cloud API)
    whatsapp_api_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_webhook_secret: SecretStr = SecretStr("")

    # Encryption
    token_encryption_key: SecretStr = SecretStr("")

    # Webhook base URL (public URL for receiving Google push notifications, e.g. ngrok)
    webhook_base_url: str = ""

    # Forward-to email
    forward_email_domain: str = "radar.app"


settings = Settings()

from src.config import Settings


def test_settings_defaults():
    s = Settings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        _env_file=None,
    )
    assert s.forward_email_domain == "radar.app"
    assert s.database_url == "postgresql+asyncpg://test:test@localhost/test"


def test_settings_whatsapp_defaults():
    s = Settings(_env_file=None)
    assert s.whatsapp_phone_number_id == ""
    assert s.whatsapp_verify_token == ""

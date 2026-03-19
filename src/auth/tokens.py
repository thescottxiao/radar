import base64
import os

from cryptography.fernet import Fernet

from src.config import settings


def _get_fernet() -> Fernet:
    key = settings.token_encryption_key.get_secret_value()
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    # Fernet requires a 32-byte URL-safe base64-encoded key.
    # If the user supplies a raw 32-byte hex key, convert it.
    if len(key) == 64:
        # Hex-encoded 32 bytes -> base64
        raw = bytes.fromhex(key)
        key = base64.urlsafe_b64encode(raw).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt an OAuth token using AES-256 (Fernet)."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode())


def decrypt_token(ciphertext: bytes) -> str:
    """Decrypt an OAuth token."""
    f = _get_fernet()
    return f.decrypt(ciphertext).decode()


def generate_encryption_key() -> str:
    """Generate a new Fernet-compatible encryption key."""
    return Fernet.generate_key().decode()


def generate_encryption_key_hex() -> str:
    """Generate a 32-byte hex key (alternative format)."""
    return os.urandom(32).hex()

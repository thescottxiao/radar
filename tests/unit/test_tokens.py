import os
from unittest.mock import patch

from cryptography.fernet import Fernet
from src.auth.tokens import decrypt_token, encrypt_token, generate_encryption_key


def test_encrypt_decrypt_roundtrip():
    key = Fernet.generate_key().decode()
    with patch("src.auth.tokens.settings") as mock_settings:
        mock_settings.token_encryption_key.get_secret_value.return_value = key
        plaintext = "ya29.a0AfH6SMB_test_refresh_token_value"
        encrypted = encrypt_token(plaintext)
        assert isinstance(encrypted, bytes)
        assert encrypted != plaintext.encode()
        decrypted = decrypt_token(encrypted)
        assert decrypted == plaintext


def test_encrypt_decrypt_with_hex_key():
    hex_key = os.urandom(32).hex()
    with patch("src.auth.tokens.settings") as mock_settings:
        mock_settings.token_encryption_key.get_secret_value.return_value = hex_key
        plaintext = "test_token_123"
        encrypted = encrypt_token(plaintext)
        decrypted = decrypt_token(encrypted)
        assert decrypted == plaintext


def test_generate_key_is_valid():
    key = generate_encryption_key()
    # Should be a valid Fernet key
    Fernet(key.encode())


def test_different_encryptions_produce_different_ciphertext():
    key = Fernet.generate_key().decode()
    with patch("src.auth.tokens.settings") as mock_settings:
        mock_settings.token_encryption_key.get_secret_value.return_value = key
        plaintext = "same_token"
        enc1 = encrypt_token(plaintext)
        enc2 = encrypt_token(plaintext)
        # Fernet includes timestamp + IV so ciphertexts differ
        assert enc1 != enc2
        assert decrypt_token(enc1) == decrypt_token(enc2) == plaintext

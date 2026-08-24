from cryptography.fernet import Fernet

from app.core.security import SecretBox, mask_secret


def test_secret_is_encrypted_and_masked() -> None:
    box = SecretBox(Fernet.generate_key().decode())
    encrypted = box.encrypt("super-secret")
    assert "super-secret" not in encrypted
    assert box.decrypt(encrypted) == "super-secret"
    assert mask_secret("abcdefgh") == "****efgh"

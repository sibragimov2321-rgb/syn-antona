from cryptography.fernet import Fernet

from app.core.security import SecretBox, mask_secret
from app.telegram.runner import is_admin_telegram_user


def test_secret_is_encrypted_and_masked() -> None:
    box = SecretBox(Fernet.generate_key().decode())
    encrypted = box.encrypt("super-secret")
    assert "super-secret" not in encrypted
    assert box.decrypt(encrypted) == "super-secret"
    assert mask_secret("abcdefgh") == "****efgh"


def test_emergency_control_is_admin_only() -> None:
    assert is_admin_telegram_user(42, {42})
    assert not is_admin_telegram_user(7, {42})

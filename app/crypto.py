"""Cifratura semplice per credenziali nel DB usando Fernet."""
import os
from cryptography.fernet import Fernet

_KEY_ENV = "BACKUP_SECRET_KEY"
_KEY_FILE = ".secret_key"


def _is_valid_fernet_key(key: str) -> bool:
    try:
        Fernet(key.encode() if isinstance(key, str) else key)
        return True
    except Exception:
        return False


def _get_fernet() -> Fernet:
    key = os.getenv(_KEY_ENV)

    if key and not _is_valid_fernet_key(key):
        key = None
        os.environ.pop(_KEY_ENV, None)

    if not key:
        if os.path.exists(_KEY_FILE):
            with open(_KEY_FILE, "r") as f:
                candidate = f.read().strip()
            if _is_valid_fernet_key(candidate):
                key = candidate
            else:
                key = Fernet.generate_key().decode()
                with open(_KEY_FILE, "w") as f:
                    f.write(key)
        else:
            key = Fernet.generate_key().decode()
            with open(_KEY_FILE, "w") as f:
                f.write(key)
        os.environ[_KEY_ENV] = key

    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ""

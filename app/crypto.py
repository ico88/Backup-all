"""Cifratura semplice per credenziali nel DB usando Fernet."""
import os
import base64
from cryptography.fernet import Fernet

_KEY_ENV = "BACKUP_SECRET_KEY"


def _get_fernet() -> Fernet:
    key = os.getenv(_KEY_ENV)
    if not key:
        # Genera e salva una chiave al primo avvio (solo sviluppo)
        key = Fernet.generate_key().decode()
        os.environ[_KEY_ENV] = key
        key_file = ".secret_key"
        if not os.path.exists(key_file):
            with open(key_file, "w") as f:
                f.write(key)
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode()).decode()

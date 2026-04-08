from __future__ import annotations

import keyring

_SERVICE = "kalshi"
_KEY_ID_USERNAME = "key_id"
_PRIVATE_KEY_USERNAME = "private_key"


def save_credentials(key_id: str, private_key_pem: str) -> None:
    """Store the Kalshi Key ID and RSA private key in the OS keychain."""
    keyring.set_password(_SERVICE, _KEY_ID_USERNAME, key_id)
    keyring.set_password(_SERVICE, _PRIVATE_KEY_USERNAME, private_key_pem)


def get_key_id() -> str:
    key_id = keyring.get_password(_SERVICE, _KEY_ID_USERNAME)
    if not key_id:
        raise RuntimeError("Kalshi Key ID not found. Run: weather-market kalshi-setup")
    return key_id


def get_private_key_pem() -> str:
    pem = keyring.get_password(_SERVICE, _PRIVATE_KEY_USERNAME)
    if not pem:
        raise RuntimeError("Kalshi private key not found. Run: weather-market kalshi-setup")
    return pem


def delete_credentials() -> None:
    """Remove both stored credentials from the OS keychain."""
    try:
        keyring.delete_password(_SERVICE, _KEY_ID_USERNAME)
    except Exception:
        pass
    try:
        keyring.delete_password(_SERVICE, _PRIVATE_KEY_USERNAME)
    except Exception:
        pass

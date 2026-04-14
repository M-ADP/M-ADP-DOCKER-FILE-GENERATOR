import os

_VAULT_PATH = "/vault/secrets/.env"
_LOCAL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")

VAULT_ENV_FILE = _VAULT_PATH if os.path.exists(_VAULT_PATH) else _LOCAL_PATH

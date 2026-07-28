"""
Auto-refreshing Gmail token helper.
Agents 4+5 call get_gmail_token() instead of reading env directly.
"""
import os, re
from pathlib import Path

_TOKEN_FILE = Path(__file__).parent.parent / "token.json"
_ENV_FILE   = Path(__file__).parent.parent / ".env"

def get_gmail_token() -> str:
    """Return a valid Gmail access token, refreshing from token.json if expired."""
    raw = os.getenv("GMAIL_ACCESS_TOKEN", "")
    # Always try refreshing from token.json if available
    if _TOKEN_FILE.exists():
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE))
            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    _TOKEN_FILE.write_text(creds.to_json())
                    # Update .env
                    env = _ENV_FILE.read_text()
                    env = re.sub(r"^GMAIL_ACCESS_TOKEN=.*",
                                 f"GMAIL_ACCESS_TOKEN={creds.token}",
                                 env, flags=re.MULTILINE)
                    _ENV_FILE.write_text(env)
            return creds.token
        except Exception:
            pass
    return raw

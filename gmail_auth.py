"""
Gmail OAuth2 Setup — run once to authorise Agent 4 + Agent 5.

Run in your terminal with:
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 gmail_auth.py

Steps:
  1. Script prints a URL and opens your browser
  2. Log in with Google → click Allow
  3. Browser redirects to localhost — script captures the code automatically
  4. token.json saved + .env updated with GMAIL_ACCESS_TOKEN
"""

import json, os, re, sys
from pathlib import Path

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import httpx

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

TOKEN_FILE = "token.json"
ENV_FILE   = ".env"
CRED_FILE  = "credentials.json"


def save_token_to_env(token: str):
    env_path = Path(ENV_FILE)
    text = env_path.read_text()
    if re.search(r"^GMAIL_ACCESS_TOKEN=", text, re.MULTILINE):
        text = re.sub(r"^GMAIL_ACCESS_TOKEN=.*", f"GMAIL_ACCESS_TOKEN={token}", text, flags=re.MULTILINE)
    else:
        text += f"\nGMAIL_ACCESS_TOKEN={token}\n"
    env_path.write_text(text)
    print("✅  .env updated with GMAIL_ACCESS_TOKEN", flush=True)


def verify_gmail(token: str):
    print("\n── Verifying Gmail API access ───────────────────────────────────", flush=True)
    try:
        r = httpx.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"maxResults": 3, "labelIds": "INBOX"},
        )
        if r.status_code != 200:
            print(f"❌  Gmail error: {r.status_code} {r.text[:200]}", flush=True)
            return
        msgs = r.json().get("messages", [])
        print(f"✅  Gmail API working — inbox messages accessible", flush=True)
        for m in msgs:
            detail = httpx.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
                headers={"Authorization": f"Bearer {token}"},
                params={"format": "metadata", "metadataHeaders": "Subject"},
            ).json()
            hdrs = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            print(f"  • {hdrs.get('Subject', '(no subject)')[:80]}", flush=True)
        print("\n🎉  Gmail setup complete!", flush=True)
    except Exception as e:
        print(f"❌  Verification error: {e}", flush=True)


def main():
    print("=" * 65, flush=True)
    print("  GMAIL OAUTH2 SETUP", flush=True)
    print("=" * 65, flush=True)

    # ── Use existing token if still valid ─────────────────────────────────────
    if Path(TOKEN_FILE).exists():
        print(f"\nFound existing {TOKEN_FILE}...", flush=True)
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            if creds.valid:
                print("✅  Token is still valid!", flush=True)
                save_token_to_env(creds.token)
                verify_gmail(creds.token)
                return
            if creds.expired and creds.refresh_token:
                print("Refreshing expired token...", flush=True)
                creds.refresh(Request())
                Path(TOKEN_FILE).write_text(creds.to_json())
                print("✅  Token refreshed!", flush=True)
                save_token_to_env(creds.token)
                verify_gmail(creds.token)
                return
        except Exception as e:
            print(f"Existing token unusable: {e} — starting fresh flow", flush=True)

    if not Path(CRED_FILE).exists():
        print(f"❌  {CRED_FILE} not found. Download from Google Cloud Console.", flush=True)
        return

    print("\nStarting OAuth2 flow...", flush=True)
    print("The browser should open automatically.", flush=True)
    print("If it doesn't, copy and open the URL that will be printed below.\n", flush=True)

    flow = InstalledAppFlow.from_client_secrets_file(CRED_FILE, SCOPES)

    # run_local_server: starts local HTTP server, opens browser, waits for callback
    # For installed-app credentials, Google allows any localhost port
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        authorization_prompt_message="\nAuth URL (open this if browser didn't open):\n{url}\n",
        success_message="Authorization complete! You can close this browser tab.",
    )

    Path(TOKEN_FILE).write_text(creds.to_json())
    print(f"\n✅  Token saved → {TOKEN_FILE}", flush=True)
    save_token_to_env(creds.token)
    verify_gmail(creds.token)


if __name__ == "__main__":
    main()

"""
One-time OAuth setup for read-only Gmail access.

Run once:  poetry run python scripts/gmail_oauth_setup.py
It prints an authorization URL (does NOT try to auto-open a browser, since this
may run headless). Open that URL yourself, sign in as the Gmail account you
want to read, and approve access. The script then exchanges the resulting code
for tokens and saves them to token_gmail.json (gitignored) at the project root.

Re-run any time to re-authorize (e.g. if the refresh token is revoked).
"""
import glob
import logging
import os

from google_auth_oauthlib.flow import InstalledAppFlow

logging.basicConfig(level=logging.INFO)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
TOKEN_PATH = os.path.join(os.path.dirname(__file__), '..', 'token_gmail.json')

CLIENT_SECRET_CANDIDATES = glob.glob(
    os.path.join(os.path.dirname(__file__), '..', 'client_secret_*.json')
)


def main():
    if not CLIENT_SECRET_CANDIDATES:
        raise SystemExit(
            "No client_secret_*.json found in the project root. Download the OAuth "
            "Desktop app client JSON from Google Cloud Console > APIs & Services > "
            "Credentials and place it there."
        )
    client_secret_file = CLIENT_SECRET_CANDIDATES[0]
    print(f"Using client secret file: {os.path.basename(client_secret_file)}")

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=False, prompt='consent')

    with open(TOKEN_PATH, 'w') as f:
        f.write(creds.to_json())

    print(f"\nSaved credentials (including refresh token) to {os.path.abspath(TOKEN_PATH)}")


if __name__ == '__main__':
    main()

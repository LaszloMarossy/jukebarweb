"""
Thin wrapper for reading and writing text files to a GCS bucket.
Bucket name is read from the GCS_BUCKET environment variable.
Credentials come from GOOGLE_APPLICATION_CREDENTIALS (path to service account JSON)
or from the GCS_KEY_JSON environment variable (raw JSON string, for Render).
"""
import json
import os
import tempfile

from google.cloud import storage
from google.oauth2 import service_account


def _client() -> storage.Client:
    # Render: key passed as a JSON string env var
    key_json = os.environ.get("GCS_KEY_JSON")
    if key_json:
        info  = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(info)
        return storage.Client(credentials=creds, project=info["project_id"])

    # Local dev: point GOOGLE_APPLICATION_CREDENTIALS at the JSON key file
    creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_file:
        with open(creds_file, "r") as f:
            info = json.load(f)
        creds = service_account.Credentials.from_service_account_file(creds_file)
        return storage.Client(credentials=creds, project=info["project_id"])

    return storage.Client()


def _bucket() -> storage.Bucket:
    return _client().bucket(os.environ["GCS_BUCKET"])


def read(path: str, default: str = "") -> str:
    """Download a text file from GCS. Returns default if not found."""
    try:
        return _bucket().blob(path).download_as_text(encoding="utf-8")
    except Exception:
        return default


def write(path: str, content: str) -> None:
    """Upload a UTF-8 text file to GCS."""
    _bucket().blob(path).upload_from_string(
        content, content_type="text/plain; charset=utf-8"
    )


def exists(path: str) -> bool:
    """Return True if the GCS object exists."""
    try:
        return _bucket().blob(path).exists()
    except Exception:
        return False

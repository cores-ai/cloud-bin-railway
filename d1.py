"""
Cloudflare D1 client shared by the Railway bot.
Manages database initialization and SQL queries via Cloudflare D1 REST API.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    folder_label TEXT NOT NULL,
    folder_slug TEXT NOT NULL UNIQUE,
    date_ist TEXT NOT NULL,
    time_ist TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    image_base64 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_drafts (
    owner_id INTEGER PRIMARY KEY,
    email TEXT,
    photo1_b64 TEXT,
    photo1_mime TEXT,
    photo2_b64 TEXT,
    photo2_mime TEXT,
    updated_at TEXT NOT NULL
);
"""

def load_env() -> None:
    """Fill any missing variables from the local .env. Existing process env wins."""
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


class D1:
    def __init__(self, account_id: str, api_token: str, database_id: str):
        self.url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def run(self, sql: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"sql": sql}
        if params is not None:
            payload["params"] = params
        response = requests.post(self.url, headers=self.headers, json=payload, timeout=60)
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"D1 returned a non-JSON response ({response.status_code})") from exc
        if not response.ok or not data.get("success"):
            errors = data.get("errors") or response.text
            raise RuntimeError(f"D1 error: {errors}")
        result = data.get("result") or [{}]
        return result[0] if result else {}

    def rows(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        return self.run(sql, params).get("results") or []

    def init(self) -> None:
        for statement in SCHEMA.split(";"):
            sql = statement.strip()
            if sql:
                self.run(sql)

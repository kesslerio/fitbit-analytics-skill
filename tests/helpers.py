"""Shared test helpers: fake HTTP responses and errors for urllib."""

import io
import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def http_error(code, reason, payload=None, url="https://example.invalid/"):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(url=url, code=code, msg=reason, hdrs=None, fp=io.BytesIO(body))

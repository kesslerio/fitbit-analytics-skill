#!/usr/bin/env python3
"""
Google Health API OAuth: auth state file, token refresh, and one-time login.

All auth state lives in one file the process owns:
    ~/.fitbit-analytics/google-health.json  (mode 0600)

Environment variables, secrets.conf and command-line token flags are never read.

Usage:
    # First login: import the OAuth client JSON downloaded from Google Cloud.
    python3 google_health_auth.py login --client-secrets /path/to/client_secret.json

    # Re-login: reuses the client credentials already in the state file.
    python3 google_health_auth.py login

    # Two-step login when stdin is not interactive: start, then finish.
    python3 google_health_auth.py login < /dev/null
    python3 google_health_auth.py login --redirect-url 'https://www.google.com/?state=...&code=...'

The login prints a consent URL. Open it in any browser on any device, approve,
and paste back the full URL the browser lands on. No port is opened.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".fitbit-analytics"
STATE_PATH = STATE_DIR / "google-health.json"
PENDING_LOGIN_PATH = STATE_DIR / "google-health-login-pending.json"
LOCK_PATH = STATE_DIR / "google-health.lock"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE_PREFIX = "https://www.googleapis.com/auth/"
REQUIRED_SCOPES = (
    SCOPE_PREFIX + "googlehealth.activity_and_fitness.readonly",
    SCOPE_PREFIX + "googlehealth.health_metrics_and_measurements.readonly",
    SCOPE_PREFIX + "googlehealth.sleep.readonly",
)

REFRESH_MARGIN = timedelta(minutes=5)
RETRY_DELAYS_SECONDS = (1, 2, 4)
RETRYABLE_HTTP_CODES = (429, 500, 502, 503, 504)
HTTP_TIMEOUT_SECONDS = 10


def _display_path(path):
    path = Path(path).resolve()
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


LOGIN_COMMAND = f"python3 {_display_path(__file__)} login"


class FitbitAuthError(RuntimeError):
    """Base error for authentication failures."""


class FitbitReauthRequiredError(FitbitAuthError):
    """Raised when the owner must run the login again."""


def reauth_message(cause):
    return f"Google Health auth {cause}; run: {LOGIN_COMMAND}"


@contextmanager
def state_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise FitbitAuthError(f"Cannot read Google Health auth state {path}: {error}") from error


def missing_scopes(granted):
    granted = set(granted or ())
    return [scope for scope in REQUIRED_SCOPES if scope not in granted]


def short_scope(scope):
    return scope[len(SCOPE_PREFIX):] if scope.startswith(SCOPE_PREFIX) else scope


def _now():
    return datetime.now(timezone.utc)


def _expires_at(tokens):
    return (_now() + timedelta(seconds=int(tokens.get("expires_in", 3600)))).isoformat()


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _token_error(error):
    try:
        body = json.loads(error.read().decode("utf-8"))
    except Exception:
        return None, None
    if not isinstance(body, dict):
        return None, None
    return body.get("error"), body.get("error_description")


def _post_token(params):
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(params).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


class GoogleHealthAuth:
    """Supplies a valid access token from the state file, refreshing on demand."""

    def __init__(self):
        self._state = None

    def _load_state(self):
        state = read_json(STATE_PATH)
        if not state or not state.get("refresh_token"):
            raise FitbitReauthRequiredError(reauth_message("is missing (no login yet)"))
        if not state.get("client_id") or not state.get("client_secret"):
            raise FitbitReauthRequiredError(reauth_message("state has no OAuth client"))
        if missing_scopes(state.get("scopes")):
            raise FitbitReauthRequiredError(reauth_message("lacks a required scope"))
        return state

    def _token_is_fresh(self, state):
        expires_at = _parse_time(state.get("expires_at"))
        return bool(state.get("access_token")) and expires_at is not None and expires_at - REFRESH_MARGIN > _now()

    def access_token(self):
        """Return an access token valid for at least the refresh margin."""
        if self._state is None:
            self._state = self._load_state()
        if not self._token_is_fresh(self._state):
            self.refresh()
        return self._state["access_token"]

    def refresh(self, force=False):
        """Refresh the access token under the state lock.

        Another process may have refreshed while this one waited for the lock, so
        the state is re-read first and the POST is skipped when the stored token is
        fresh, unless the caller forces it (for example after an HTTP 401).
        """
        with state_lock():
            state = self._load_state()
            if not force and self._token_is_fresh(state):
                self._state = state
                return False

            params = {
                "grant_type": "refresh_token",
                "refresh_token": state["refresh_token"],
                "client_id": state["client_id"],
                "client_secret": state["client_secret"],
            }
            tokens = self._post_refresh(params)

            access_token = tokens.get("access_token")
            if not access_token:
                raise FitbitAuthError("Google Health token refresh returned no access token.")
            state["access_token"] = access_token
            state["expires_at"] = _expires_at(tokens)
            if tokens.get("refresh_token"):
                state["refresh_token"] = tokens["refresh_token"]
            if tokens.get("scope"):
                state["scopes"] = sorted(tokens["scope"].split())
            atomic_write_json(STATE_PATH, state)
            self._state = state

            if missing_scopes(state.get("scopes")):
                raise FitbitReauthRequiredError(reauth_message("lacks a required scope"))
            return True

    def _post_refresh(self, params):
        attempts = len(RETRY_DELAYS_SECONDS) + 1
        for attempt in range(attempts):
            try:
                return _post_token(params)
            except urllib.error.HTTPError as error:
                error_code, description = _token_error(error)
                if error_code == "invalid_grant":
                    raise FitbitReauthRequiredError(reauth_message("is revoked or expired")) from error
                if error.code in RETRYABLE_HTTP_CODES and attempt < attempts - 1:
                    time.sleep(RETRY_DELAYS_SECONDS[attempt])
                    continue
                detail = description or error_code or error.reason
                raise FitbitAuthError(
                    f"Google Health token refresh failed: HTTP {error.code} {detail}"
                ) from error
            except (urllib.error.URLError, OSError) as error:
                # urllib wraps only send-side failures in URLError; a read
                # timeout surfaces as a bare TimeoutError.
                if attempt < attempts - 1:
                    time.sleep(RETRY_DELAYS_SECONDS[attempt])
                    continue
                reason = getattr(error, "reason", error)
                raise FitbitAuthError(
                    f"Google Health token refresh failed after {attempts} attempts: {reason}"
                ) from error
        raise FitbitAuthError("Google Health token refresh failed after retries.")


class LoginError(RuntimeError):
    """A login step failed; the message tells the owner what to do next."""


def load_client_secrets(path):
    """Read a Google OAuth client JSON (Web or Desktop client)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LoginError(f"Client secrets file not found: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise LoginError(f"Cannot read client secrets file {path}: {error}") from error

    client = data.get("web") or data.get("installed")
    if not isinstance(client, dict):
        raise LoginError("Client secrets file has no 'web' or 'installed' client; download the OAuth client JSON again.")
    redirect_uris = client.get("redirect_uris") or []
    if not client.get("client_id") or not client.get("client_secret") or not redirect_uris:
        raise LoginError("Client secrets file lacks client_id, client_secret or redirect_uris.")
    return {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": redirect_uris[0],
    }


def _stored_client():
    state = read_json(STATE_PATH)
    if state and state.get("client_id") and state.get("client_secret") and state.get("redirect_uri"):
        return {key: state[key] for key in ("client_id", "client_secret", "redirect_uri")}
    raise LoginError(
        "No OAuth client is stored yet. Fetch the Google OAuth client JSON from your password manager and run: "
        f"{LOGIN_COMMAND} --client-secrets <path>"
    )


def _pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def start_login(client):
    """Create a pending login (state + PKCE verifier) and return the consent URL."""
    pending = dict(client)
    pending["state"] = secrets.token_urlsafe(32)
    pending["code_verifier"] = secrets.token_urlsafe(64)
    atomic_write_json(PENDING_LOGIN_PATH, pending)

    query = urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": client["redirect_uri"],
        "response_type": "code",
        "scope": " ".join(REQUIRED_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": pending["state"],
        "code_challenge": _pkce_challenge(pending["code_verifier"]),
        "code_challenge_method": "S256",
    })
    return f"{AUTH_URL}?{query}"


def finish_login(redirect_url):
    """Check the pasted redirect URL, exchange the code, and write the state file."""
    pending = read_json(PENDING_LOGIN_PATH)
    if not pending:
        raise LoginError(f"No login is in progress. Start one with: {LOGIN_COMMAND}")

    params = urllib.parse.parse_qs(urllib.parse.urlsplit(redirect_url.strip()).query)
    if params.get("error"):
        raise LoginError(f"Google returned '{params['error'][0]}' instead of a code; run the login again and approve access.")
    if params.get("state", [None])[0] != pending["state"]:
        raise LoginError("The pasted URL does not match the login in progress (state mismatch); run the login again.")
    code = params.get("code", [None])[0]
    if not code:
        raise LoginError("The pasted URL has no authorization code; paste the full URL from the browser's address bar.")

    try:
        tokens = _post_token({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": pending["code_verifier"],
            "redirect_uri": pending["redirect_uri"],
            "client_id": pending["client_id"],
            "client_secret": pending["client_secret"],
        })
    except urllib.error.HTTPError as error:
        error_code, description = _token_error(error)
        detail = description or error_code or error.reason
        raise LoginError(
            f"Google rejected the authorization code (HTTP {error.code} {detail}); run the login again."
        ) from error
    except urllib.error.URLError as error:
        raise LoginError(f"Could not reach Google to exchange the code: {error.reason}") from error

    if not tokens.get("refresh_token"):
        raise LoginError(
            "Google returned no refresh token. Remove this app's access in your Google Account "
            "if needed, then run the login again and approve consent."
        )
    granted = sorted((tokens.get("scope") or "").split())
    missing = missing_scopes(granted)
    if missing:
        names = ", ".join(short_scope(scope) for scope in missing)
        raise LoginError(f"Consent did not grant {names}; run the login again and leave every box checked.")

    state = {
        "client_id": pending["client_id"],
        "client_secret": pending["client_secret"],
        "redirect_uri": pending["redirect_uri"],
        "refresh_token": tokens["refresh_token"],
        "access_token": tokens.get("access_token"),
        "expires_at": _expires_at(tokens),
        "scopes": granted,
        "logged_in_at": _now().isoformat(),
    }
    with state_lock():
        atomic_write_json(STATE_PATH, state)
    PENDING_LOGIN_PATH.unlink(missing_ok=True)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-time Google Health login")
    subparsers = parser.add_subparsers(dest="command", required=True)
    login = subparsers.add_parser("login", help="Authorize access with a browser consent")
    login.add_argument("--client-secrets", help="Google OAuth client JSON (needed on first login only)")
    login.add_argument("--redirect-url", help="Finish a started login with the URL the browser landed on")
    args = parser.parse_args(argv)

    try:
        if args.redirect_url:
            finish_login(args.redirect_url)
        else:
            client = load_client_secrets(args.client_secrets) if args.client_secrets else _stored_client()
            url = start_login(client)
            print("Open this URL in any browser, approve access, then paste the full URL the browser lands on.")
            print(url)
            try:
                redirect_url = input("Redirect URL: ")
            except EOFError:
                print(f"\nNo URL on stdin. Finish with: {LOGIN_COMMAND} --redirect-url '<pasted URL>'")
                return 0
            finish_login(redirect_url)
    except (LoginError, FitbitAuthError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Google Health login saved to {_display_path(STATE_PATH)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time as pytime
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from helpers import ROOT, FakeResponse, http_error

import fitbit_api
import google_health_auth as gha

SCOPES = list(gha.REQUIRED_SCOPES)
CLIENT_JSON = {
    "web": {
        "client_id": "test-client.apps.googleusercontent.com",
        "client_secret": "test-client-secret",
        "redirect_uris": ["https://www.google.com"],
    }
}


def iso_in(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tempdir.name)
        self.state_path = self.dir / "google-health.json"
        self.pending_path = self.dir / "google-health-login-pending.json"
        patches = [
            mock.patch.object(gha, "STATE_PATH", self.state_path),
            mock.patch.object(gha, "PENDING_LOGIN_PATH", self.pending_path),
            mock.patch.object(gha, "LOCK_PATH", self.dir / "google-health.lock"),
            mock.patch.object(gha.time, "sleep"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tempdir.cleanup)

    def write_state(self, **overrides):
        state = {
            "client_id": "stored-client",
            "client_secret": "stored-secret",
            "redirect_uri": "https://www.google.com",
            "refresh_token": "stored-refresh",
            "access_token": "stored-access",
            "expires_at": iso_in(hours=1),
            "scopes": SCOPES,
        }
        state.update(overrides)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        return state

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def client_secrets_file(self):
        path = self.dir / "client_secret_test.json"
        path.write_text(json.dumps(CLIENT_JSON), encoding="utf-8")
        return path


class LoginTests(AuthTestCase):
    def start(self, client_secrets=True):
        client = gha.load_client_secrets(self.client_secrets_file()) if client_secrets else gha._stored_client()
        url = gha.start_login(client)
        return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)

    def redirect_for(self, consent_query, code="auth-code", state=None):
        state = state if state is not None else consent_query["state"][0]
        return f"https://www.google.com/?state={state}&code={code}&scope=ignored"

    def token_response(self, **overrides):
        payload = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3599,
            "scope": " ".join(SCOPES),
            "token_type": "Bearer",
        }
        payload.update(overrides)
        return payload

    def test_consent_url_requests_offline_access_with_pkce_and_all_scopes(self):
        query = self.start()

        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["code_challenge"][0])
        self.assertEqual(sorted(query["scope"][0].split()), sorted(SCOPES))
        self.assertEqual(query["redirect_uri"], ["https://www.google.com"])
        self.assertNotIn("include_granted_scopes", query)

    def test_login_exchanges_code_with_verifier_and_writes_private_state(self):
        query = self.start()
        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        posted = {}

        def fake_urlopen(req, timeout=10):
            self.assertEqual(req.full_url, gha.TOKEN_URL)
            posted.update(urllib.parse.parse_qs(req.data.decode("utf-8")))
            return FakeResponse(self.token_response())

        with mock.patch.object(gha.urllib.request, "urlopen", side_effect=fake_urlopen):
            gha.finish_login(self.redirect_for(query))

        self.assertEqual(posted["grant_type"], ["authorization_code"])
        self.assertEqual(posted["code"], ["auth-code"])
        self.assertEqual(posted["code_verifier"], [pending["code_verifier"]])
        self.assertEqual(posted["redirect_uri"], ["https://www.google.com"])
        self.assertEqual(gha._pkce_challenge(pending["code_verifier"]), query["code_challenge"][0])

        state = self.read_state()
        self.assertEqual(state["refresh_token"], "new-refresh")
        self.assertEqual(sorted(state["scopes"]), sorted(SCOPES))
        self.assertIn("expires_at", state)
        self.assertEqual(state["client_id"], CLIENT_JSON["web"]["client_id"])
        self.assertEqual(stat.S_IMODE(os.stat(self.state_path).st_mode), 0o600)
        self.assertFalse(self.pending_path.exists())

    def test_login_with_mismatched_state_fails_and_writes_nothing(self):
        query = self.start()

        with mock.patch.object(gha.urllib.request, "urlopen") as urlopen_mock:
            with self.assertRaisesRegex(gha.LoginError, "state mismatch"):
                gha.finish_login(self.redirect_for(query, state="forged"))

        urlopen_mock.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_login_missing_sleep_scope_names_it_and_writes_nothing(self):
        query = self.start()
        partial = " ".join(scope for scope in SCOPES if not scope.endswith("sleep.readonly"))

        with mock.patch.object(
            gha.urllib.request, "urlopen", return_value=FakeResponse(self.token_response(scope=partial))
        ):
            with self.assertRaisesRegex(gha.LoginError, "googlehealth.sleep.readonly"):
                gha.finish_login(self.redirect_for(query))

        self.assertFalse(self.state_path.exists())

    def test_login_without_refresh_token_tells_owner_to_retry_consent(self):
        query = self.start()
        response = self.token_response()
        del response["refresh_token"]

        with mock.patch.object(gha.urllib.request, "urlopen", return_value=FakeResponse(response)):
            with self.assertRaisesRegex(gha.LoginError, "no refresh token.*login again"):
                gha.finish_login(self.redirect_for(query))

        self.assertFalse(self.state_path.exists())

    def test_relogin_reuses_stored_client_and_replaces_tokens(self):
        self.write_state(refresh_token="old-refresh", access_token="old-access")
        query = self.start(client_secrets=False)
        posted = {}

        def fake_urlopen(req, timeout=10):
            posted.update(urllib.parse.parse_qs(req.data.decode("utf-8")))
            return FakeResponse(self.token_response())

        with mock.patch.object(gha.urllib.request, "urlopen", side_effect=fake_urlopen):
            gha.finish_login(self.redirect_for(query))

        self.assertEqual(query["client_id"], ["stored-client"])
        self.assertEqual(posted["client_secret"], ["stored-secret"])
        state = self.read_state()
        self.assertEqual((state["client_id"], state["client_secret"]), ("stored-client", "stored-secret"))
        self.assertEqual((state["refresh_token"], state["access_token"]), ("new-refresh", "new-access"))

    def test_first_login_without_client_secrets_points_to_1password(self):
        with self.assertRaisesRegex(gha.LoginError, "1Password.*--client-secrets"):
            gha._stored_client()

    def test_cli_two_step_login_completes_with_redirect_url(self):
        stdout = io.StringIO()
        with mock.patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(stdout):
                exit_code = gha.main(["login", "--client-secrets", str(self.client_secrets_file())])
        self.assertEqual(exit_code, 0)
        consent_url = next(line for line in stdout.getvalue().splitlines() if line.startswith(gha.AUTH_URL))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(consent_url).query)

        with mock.patch.object(gha.urllib.request, "urlopen", return_value=FakeResponse(self.token_response())):
            with redirect_stdout(io.StringIO()):
                exit_code = gha.main(["login", "--redirect-url", self.redirect_for(query)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.read_state()["refresh_token"], "new-refresh")


class RefreshTests(AuthTestCase):
    def test_expiring_token_refreshes_with_body_credentials_and_keeps_refresh_token(self):
        self.write_state(expires_at=iso_in(minutes=2))
        posted = {}

        def fake_urlopen(req, timeout=10):
            self.assertEqual(req.full_url, gha.TOKEN_URL)
            self.assertIsNone(req.get_header("Authorization"))
            posted.update(urllib.parse.parse_qs(req.data.decode("utf-8")))
            return FakeResponse({"access_token": "fresh-access", "expires_in": 3599, "scope": " ".join(SCOPES)})

        with mock.patch.object(gha.urllib.request, "urlopen", side_effect=fake_urlopen):
            token = gha.GoogleHealthAuth().access_token()

        self.assertEqual(token, "fresh-access")
        self.assertEqual(posted["grant_type"], ["refresh_token"])
        self.assertEqual(posted["refresh_token"], ["stored-refresh"])
        self.assertEqual(posted["client_id"], ["stored-client"])
        self.assertEqual(posted["client_secret"], ["stored-secret"])
        state = self.read_state()
        self.assertEqual(state["refresh_token"], "stored-refresh")
        self.assertEqual(state["access_token"], "fresh-access")
        self.assertGreater(datetime.fromisoformat(state["expires_at"]), datetime.now(timezone.utc) + timedelta(minutes=50))

    def test_fresh_token_is_used_without_refresh(self):
        self.write_state()

        with mock.patch.object(gha.urllib.request, "urlopen") as urlopen_mock:
            token = gha.GoogleHealthAuth().access_token()

        self.assertEqual(token, "stored-access")
        urlopen_mock.assert_not_called()

    def test_invalid_grant_raises_reauth_required_and_keeps_state(self):
        before = self.write_state(expires_at=iso_in(minutes=-1))
        error = http_error(400, "Bad Request", {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})

        with mock.patch.object(gha.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gha.FitbitReauthRequiredError) as caught:
                gha.GoogleHealthAuth().access_token()

        self.assertIn("revoked or expired", str(caught.exception))
        self.assertIn(gha.LOGIN_COMMAND, str(caught.exception))
        self.assertEqual(self.read_state(), before)

    def test_transient_503_retries_with_backoff_then_succeeds(self):
        self.write_state(expires_at=iso_in(minutes=-1))
        responses = [
            http_error(503, "Service Unavailable"),
            http_error(503, "Service Unavailable"),
            FakeResponse({"access_token": "fresh-access", "expires_in": 3599}),
        ]

        def fake_urlopen(req, timeout=10):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(gha.urllib.request, "urlopen", side_effect=fake_urlopen):
            token = gha.GoogleHealthAuth().access_token()

        self.assertEqual(token, "fresh-access")
        self.assertEqual([c.args[0] for c in gha.time.sleep.call_args_list], [1, 2])

    def test_exhausted_503_retries_raise_generic_auth_error_not_reauth(self):
        before = self.write_state(expires_at=iso_in(minutes=-1))

        with mock.patch.object(
            gha.urllib.request, "urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(http_error(503, "Unavailable"))
        ) as urlopen_mock:
            with self.assertRaises(gha.FitbitAuthError) as caught:
                gha.GoogleHealthAuth().access_token()

        self.assertNotIsInstance(caught.exception, gha.FitbitReauthRequiredError)
        self.assertEqual(urlopen_mock.call_count, 4)
        self.assertEqual([c.args[0] for c in gha.time.sleep.call_args_list], [1, 2, 4])
        self.assertEqual(self.read_state(), before)

    def test_missing_state_raises_reauth_naming_login_command(self):
        with self.assertRaises(gha.FitbitReauthRequiredError) as caught:
            gha.GoogleHealthAuth().access_token()

        self.assertIn(gha.LOGIN_COMMAND, str(caught.exception))
        self.assertIn("auth", str(caught.exception))

    def test_state_missing_a_scope_raises_reauth(self):
        self.write_state(scopes=SCOPES[:2])

        with self.assertRaisesRegex(gha.FitbitReauthRequiredError, "scope"):
            gha.GoogleHealthAuth().access_token()

    def test_env_and_secrets_tokens_are_ignored(self):
        # The legacy client read tokens from this file under HOME.
        secrets_conf = self.dir / ".config" / "systemd" / "user" / "secrets.conf"
        secrets_conf.parent.mkdir(parents=True)
        secrets_conf.write_text('FITBIT_ACCESS_TOKEN="secrets-access"\nFITBIT_REFRESH_TOKEN="secrets-refresh"\n')
        env = {
            "HOME": str(self.dir),
            "FITBIT_ACCESS_TOKEN": "env-access",
            "FITBIT_REFRESH_TOKEN": "env-refresh",
            "FITBIT_CLIENT_ID": "env-client",
            "FITBIT_CLIENT_SECRET": "env-secret",
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(gha.urllib.request, "urlopen") as urlopen_mock:
                with self.assertRaises(gha.FitbitReauthRequiredError):
                    gha.GoogleHealthAuth().access_token()
            urlopen_mock.assert_not_called()

            self.write_state(expires_at=iso_in(minutes=-1))
            posted = {}

            def fake_urlopen(req, timeout=10):
                posted.update(urllib.parse.parse_qs(req.data.decode("utf-8")))
                return FakeResponse({"access_token": "fresh-access", "expires_in": 3599})

            with mock.patch.object(gha.urllib.request, "urlopen", side_effect=fake_urlopen):
                token = gha.GoogleHealthAuth().access_token()

        self.assertEqual(token, "fresh-access")
        self.assertEqual(posted["refresh_token"], ["stored-refresh"])
        self.assertEqual(posted["client_id"], ["stored-client"])

    def test_concurrent_refreshes_post_once(self):
        self.write_state(expires_at=iso_in(minutes=-1))
        post_count = 0
        count_lock = threading.Lock()

        def slow_urlopen(req, timeout=10):
            nonlocal post_count
            with count_lock:
                post_count += 1
            pytime.sleep(0.2)
            return FakeResponse({"access_token": "fresh-access", "expires_in": 3599})

        tokens = []
        with mock.patch.object(gha.urllib.request, "urlopen", side_effect=slow_urlopen):
            threads = [threading.Thread(target=lambda: tokens.append(gha.GoogleHealthAuth().access_token())) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(post_count, 1)
        self.assertEqual(tokens, ["fresh-access", "fresh-access"])

    def test_refresh_waits_for_lock_holder(self):
        self.write_state(expires_at=iso_in(minutes=-1))
        release = threading.Event()
        finished = threading.Event()

        def hold_lock():
            with gha.state_lock():
                release.wait(timeout=2)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        pytime.sleep(0.1)

        def refresh():
            try:
                gha.GoogleHealthAuth().refresh(force=True)
            finally:
                finished.set()

        with mock.patch.object(
            gha.urllib.request, "urlopen", return_value=FakeResponse({"access_token": "fresh", "expires_in": 3599})
        ):
            refresher = threading.Thread(target=refresh)
            refresher.start()
            pytime.sleep(0.1)
            self.assertFalse(finished.is_set())
            release.set()
            refresher.join(timeout=2)
        holder.join(timeout=2)

        self.assertTrue(finished.is_set())

    def test_atomic_write_leaves_no_temp_files_and_is_private(self):
        gha.atomic_write_json(self.state_path, {"a": 1})
        gha.atomic_write_json(self.state_path, {"a": 2})

        self.assertEqual(self.read_state(), {"a": 2})
        self.assertEqual(stat.S_IMODE(os.stat(self.state_path).st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith(".")), [])


class CredentialFlagTests(unittest.TestCase):
    def test_alerts_rejects_access_token_flag(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "alerts.py"), "--access-token", "x"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --access-token", result.stderr)

    def test_fitbit_api_cli_rejects_client_id_flag(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "fitbit_api.py"), "steps", "--client-id", "x"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --client-id", result.stderr)

    def test_fitbit_client_takes_no_credentials(self):
        with self.assertRaises(TypeError):
            fitbit_api.FitbitClient(access_token="x")


if __name__ == "__main__":
    unittest.main()

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fitbit_briefing

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "briefing"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def read_golden(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFitbitClient:
    """Serves Fitbit-shaped responses from a synthetic day fixture."""

    def __init__(self, fixture, azm_error=None):
        self.fixture = fixture
        self.azm_error = azm_error
        self.raw_requests = []

    def _series(self, key, start_date, end_date):
        return [
            entry for entry in self.fixture[key]
            if start_date <= entry["dateTime"] <= end_date
        ]

    def get_steps(self, start_date, end_date):
        return {"activities-steps": self._series("steps", start_date, end_date)}

    def get_calories(self, start_date, end_date):
        return {"activities-calories": self._series("calories", start_date, end_date)}

    def get_activity_summary(self, start_date, end_date):
        return self.fixture["activity_summary"].get(start_date, {"activities": [], "summary": {}})

    def get_heartrate(self, start_date, end_date):
        return {"activities-heart": self._series("heartrate", start_date, end_date)}

    def get_sleep(self, start_date, end_date):
        return {
            "sleep": [
                entry for entry in self.fixture["sleep"]
                if start_date <= entry["dateOfSleep"] <= end_date
            ]
        }

    def get_logged_activities(self, date):
        return {"activities": self.fixture["logged_activities"].get(date, [])}

    def get_active_zone_minutes(self, start_date, end_date):
        if self.azm_error is not None:
            raise self.azm_error
        return {
            "activities-active-zone-minutes": self._series("active_zone_minutes", start_date, end_date)
        }

    def _request(self, endpoint, *args, **kwargs):
        # Legacy raw-path access; the briefing should not need it.
        self.raw_requests.append(endpoint)
        date = endpoint.split("/date/")[1].split("/")[0].replace(".json", "")
        if "active-zone-minutes" in endpoint:
            return self.get_active_zone_minutes(date, date)
        return self.get_logged_activities(date)


def run_briefing(client, output_format):
    """Run the briefing CLI against a fake client; return (stdout, stderr, exit_code)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = ["fitbit_briefing.py", "--date", client.fixture["target_date"], "--format", output_format]
    exit_code = 0
    with mock.patch.object(fitbit_briefing, "FitbitClient", return_value=client):
        with mock.patch.object(sys, "argv", argv):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    fitbit_briefing.main()
                except SystemExit as exit_signal:
                    exit_code = exit_signal.code
    return stdout.getvalue(), stderr.getvalue(), exit_code


class BriefingGoldenTests(unittest.TestCase):
    def test_full_day_text_matches_golden(self):
        stdout, _, exit_code = run_briefing(FakeFitbitClient(load_fixture("full_day.json")), "text")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, read_golden("full_day.text.txt"))

    def test_full_day_json_matches_golden(self):
        stdout, _, exit_code = run_briefing(FakeFitbitClient(load_fixture("full_day.json")), "json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout), json.loads(read_golden("full_day.output.json")))

    def test_full_day_brief_matches_golden(self):
        stdout, _, exit_code = run_briefing(FakeFitbitClient(load_fixture("full_day.json")), "brief")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, read_golden("full_day.brief.txt"))

    def test_empty_day_text_matches_golden(self):
        stdout, _, exit_code = run_briefing(FakeFitbitClient(load_fixture("empty_day.json")), "text")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, read_golden("empty_day.text.txt"))

    def test_azm_fetch_error_omits_only_the_azm_section(self):
        client = FakeFitbitClient(load_fixture("full_day.json"), azm_error=RuntimeError("HTTP 500"))

        stdout, _, exit_code = run_briefing(client, "text")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, read_golden("full_day_no_azm.text.txt"))

    def test_briefing_fetches_through_client_methods_only(self):
        client = FakeFitbitClient(load_fixture("full_day.json"))

        run_briefing(client, "text")

        self.assertEqual(client.raw_requests, [])


if __name__ == "__main__":
    unittest.main()

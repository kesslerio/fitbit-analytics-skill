import io
import json
import re
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from helpers import FakeResponse, http_error

import alerts
import fitbit_api
import google_health_auth as gha
from test_fitbit_briefing import FakeFitbitClient, load_fixture, read_golden, run_briefing

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "google_health"

# The filter field each list/reconcile request must use, per the v4 API reference.
EXPECTED_FILTER_FIELDS = {
    "daily-resting-heart-rate": "daily_resting_heart_rate.date",
    "daily-heart-rate-zones": "daily_heart_rate_zones.date",
    "daily-oxygen-saturation": "daily_oxygen_saturation.date",
    "sleep": "sleep.interval.civil_end_time",
    "exercise": "exercise.interval.civil_start_time",
    "weight": "weight.sample_time.civil_time",
}


def _iso(civil_date):
    return f"{civil_date['year']:04d}-{civil_date['month']:02d}-{civil_date['day']:02d}"


def _point_date(data_type, point):
    if data_type == "sleep":
        return _iso(point["sleep"]["interval"]["civilEndTime"]["date"])
    if data_type == "exercise":
        return _iso(point["exercise"]["interval"]["civilStartTime"]["date"])
    if data_type == "weight":
        return _iso(point["weight"]["sampleTime"]["civilTime"]["date"])
    return _iso(point[fitbit_api._camel(data_type)]["date"])


class FakeGoogleHealth:
    """Serves fixture data points like the v4 API: range filtering and paging."""

    def __init__(self, page_limits=None):
        self.page_limits = page_limits or {}
        self.requests = []
        self.errors = []

    def fixture(self, data_type, method):
        path = FIXTURES / f"{data_type}.{method}.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("rollupDataPoints", payload.get("dataPoints", []))

    def urlopen(self, req, timeout=None):
        self.requests.append(req)
        if self.errors:
            raise self.errors.pop(0)
        assert req.get_header("Authorization") == "Bearer valid-access", req.get_header("Authorization")

        split = urllib.parse.urlsplit(req.full_url)
        tail = split.path.split("/v4/users/me/dataTypes/")[1]
        data_type, resource = tail.split("/", 1)
        method = resource.split(":", 1)[1] if ":" in resource else "list"

        if method == "dailyRollUp":
            body = json.loads(req.data.decode("utf-8"))
            start, end = _iso(body["range"]["start"]["date"]), _iso(body["range"]["end"]["date"])
            assert body["windowSizeDays"] == 1
            assert (date.fromisoformat(end) - date.fromisoformat(start)).days <= fitbit_api.ROLLUP_MAX_DAYS.get(
                data_type, fitbit_api.DEFAULT_ROLLUP_MAX_DAYS
            ), (data_type, start, end)
            points = [
                p for p in self.fixture(data_type, method)
                if start <= _iso(p["civilStartTime"]["date"]) < end
            ]
            page_token = body.get("pageToken")
            key = "rollupDataPoints"
        else:
            query = urllib.parse.parse_qs(split.query)
            filter_text = query["filter"][0]
            field = EXPECTED_FILTER_FIELDS[data_type]
            match = re.fullmatch(rf'{re.escape(field)} >= "([\d-]+)" AND {re.escape(field)} < "([\d-]+)"', filter_text)
            assert match, filter_text
            start, end = match.groups()
            points = [p for p in self.fixture(data_type, method) if start <= _point_date(data_type, p) < end]
            page_token = query.get("pageToken", [None])[0]
            key = "dataPoints"

        offset = int(page_token or 0)
        limit = self.page_limits.get(data_type, len(points) or 1)
        response = {key: points[offset:offset + limit]}
        if offset + limit < len(points):
            response["nextPageToken"] = str(offset + limit)
        return FakeResponse(response)

    def calls(self, data_type):
        return [r for r in self.requests if f"/dataTypes/{data_type}/" in r.full_url]


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.dir = Path(tempdir.name)
        self.state_path = self.dir / "google-health.json"
        self.state_path.write_text(json.dumps({
            "client_id": "client", "client_secret": "secret", "redirect_uri": "https://www.google.com",
            "refresh_token": "refresh", "access_token": "valid-access",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "scopes": list(gha.REQUIRED_SCOPES),
        }), encoding="utf-8")
        self.server = FakeGoogleHealth()
        for patcher in (
            mock.patch.object(gha, "STATE_PATH", self.state_path),
            mock.patch.object(gha, "LOCK_PATH", self.dir / "google-health.lock"),
            mock.patch.object(fitbit_api.urllib.request, "urlopen", side_effect=self.server.urlopen),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = fitbit_api.FitbitClient()


class ActivityMappingTests(AdapterTestCase):
    def test_steps_over_eight_days_are_string_values_in_date_order(self):
        data = self.client.get_steps("2026-01-13", "2026-01-20")

        self.assertEqual(
            data["activities-steps"],
            [{"dateTime": d, "value": v} for d, v in [
                ("2026-01-13", "7200"), ("2026-01-14", "9100"), ("2026-01-15", "6400"), ("2026-01-16", "8800"),
                ("2026-01-17", "11050"), ("2026-01-18", "5300"), ("2026-01-19", "7650"), ("2026-01-20", "8421"),
            ]],
        )

    def test_days_without_rollup_points_read_as_zero(self):
        data = self.client.get_steps("2026-01-20", "2026-01-21")

        self.assertEqual(data["activities-steps"][1], {"dateTime": "2026-01-21", "value": "0"})

    def test_calories_are_rounded_whole_kilocalories(self):
        data = self.client.get_calories("2026-01-20", "2026-01-20")

        self.assertEqual(data["activities-calories"], [{"dateTime": "2026-01-20", "value": "2310"}])

    def test_activity_summary_converts_distance_and_active_minutes(self):
        summary = self.client.get_activity_summary("2026-01-20", "2026-01-20")["summary"]

        self.assertEqual(summary["distance"], 6.12)
        self.assertEqual(summary["floors"], 9)
        self.assertEqual(summary["veryActiveMinutes"], 20)
        self.assertEqual(summary["fairlyActiveMinutes"], 15)

    def test_distance_series_is_in_km(self):
        data = self.client.get_distance("2026-01-20", "2026-01-20")

        self.assertEqual(data["activities-distance"], [{"dateTime": "2026-01-20", "value": "6.12"}])

    def test_logged_activities_map_exercise_sessions(self):
        data = self.client.get_logged_activities("2026-01-19")

        self.assertEqual(len(data["activities"]), 1)
        activity = data["activities"][0]
        self.assertEqual((activity["name"], activity["duration"], activity["calories"]), ("Walk", 1800000, 160))

    def test_active_zone_minutes_total_is_sum_of_credited_zone_minutes(self):
        data = self.client.get_active_zone_minutes("2026-01-19", "2026-01-19")

        self.assertEqual(data["activities-active-zone-minutes"], [{"dateTime": "2026-01-19", "value": {
            "activeZoneMinutes": 24, "fatBurnActiveZoneMinutes": 12,
            "cardioActiveZoneMinutes": 8, "peakActiveZoneMinutes": 4,
        }}])

    def test_day_without_active_zone_minutes_is_omitted(self):
        data = self.client.get_active_zone_minutes("2026-01-20", "2026-01-20")

        self.assertEqual(data["activities-active-zone-minutes"], [])


class HeartRateMappingTests(AdapterTestCase):
    def test_heartrate_maps_resting_hr_and_zones(self):
        day = self.client.get_heartrate("2026-01-20", "2026-01-20")["activities-heart"][0]

        self.assertEqual(day["dateTime"], "2026-01-20")
        self.assertEqual(day["value"]["restingHeartRate"], 58)
        self.assertEqual(day["value"]["heartRateZones"], [
            {"name": "Out of Range", "minutes": 1210, "min": 30, "max": 98},
            {"name": "Fat Burn", "minutes": 42, "min": 98, "max": 137},
            {"name": "Cardio", "minutes": 12, "min": 137, "max": 166},
            {"name": "Peak", "minutes": 3, "min": 166, "max": 220},
        ])

    def test_missing_resting_hr_point_leaves_key_absent(self):
        day = self.client.get_heartrate("2026-01-19", "2026-01-19")["activities-heart"][0]

        self.assertNotIn("restingHeartRate", day["value"])

    def test_thirty_day_range_is_split_at_maximum_window_and_merged_in_order(self):
        data = self.client.get_heartrate("2026-01-01", "2026-01-30")

        dates = [entry["dateTime"] for entry in data["activities-heart"]]
        self.assertEqual(len(dates), 30)
        self.assertEqual(dates, sorted(set(dates)))
        self.assertEqual(len(self.server.calls("time-in-heart-rate-zone")), 3)


class SleepAndMetricMappingTests(AdapterTestCase):
    def test_sleep_for_date_puts_main_sleep_first_without_efficiency(self):
        sessions = self.client.get_sleep("2026-01-20", "2026-01-20")["sleep"]

        self.assertEqual([s["isMainSleep"] for s in sessions], [True, False])
        main = sessions[0]
        self.assertEqual(main["dateOfSleep"], "2026-01-20")
        self.assertEqual(main["duration"], 27000000)
        self.assertEqual(main["minutesAwake"], 38)
        self.assertEqual(main["minutesAsleep"], 412)
        self.assertNotIn("efficiency", main)

    def test_sleep_follows_next_page_token(self):
        self.server.page_limits["sleep"] = 1

        sessions = self.client.get_sleep("2026-01-20", "2026-01-20")["sleep"]

        self.assertEqual(len(sessions), 2)
        self.assertEqual(len(self.server.calls("sleep")), 2)

    def test_sleep_stages_summary_uses_fitbit_level_names(self):
        main = self.client.get_sleep_stages("2026-01-20", "2026-01-20")["sleep"][0]

        self.assertEqual(main["levels"]["summary"], {
            "wake": {"count": 21, "minutes": 38}, "light": {"count": 25, "minutes": 230},
            "deep": {"count": 4, "minutes": 82}, "rem": {"count": 6, "minutes": 100},
        })

    def test_spo2_maps_daily_average_and_bounds(self):
        self.assertEqual(self.client.get_spo2("2026-01-20", "2026-01-20"), [
            {"dateTime": "2026-01-20", "value": {"avg": 95.8, "min": 94.1, "max": 97.4}},
        ])

    def test_weight_grams_become_kg_using_last_measurement_per_date(self):
        data = self.client.get_weight("2026-01-19", "2026-01-20")

        self.assertEqual(data["body-weight"], [
            {"dateTime": "2026-01-19", "value": "80.6"},
            {"dateTime": "2026-01-20", "value": "80.25"},
        ])


class ErrorMappingTests(AdapterTestCase):
    def test_401_refreshes_once_and_retries(self):
        self.server.errors.append(http_error(401, "Unauthorized"))

        with mock.patch.object(self.client._auth, "refresh") as refresh:
            data = self.client.get_steps("2026-01-20", "2026-01-20")

        refresh.assert_called_once_with(force=True)
        self.assertEqual(data["activities-steps"][0]["value"], "8421")

    def test_second_401_raises(self):
        self.server.errors.extend([http_error(401, "Unauthorized"), http_error(401, "Unauthorized")])

        with mock.patch.object(self.client._auth, "refresh"):
            with self.assertRaises(fitbit_api.FitbitAuthError):
                self.client.get_steps("2026-01-20", "2026-01-20")

    def test_insufficient_scope_403_requires_relogin(self):
        self.server.errors.append(http_error(403, "Forbidden", {"error": {
            "code": 403, "message": "Request had insufficient authentication scopes.", "status": "PERMISSION_DENIED",
            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
        }}))

        with self.assertRaises(fitbit_api.FitbitReauthRequiredError):
            self.client.get_sleep("2026-01-20", "2026-01-20")

    def test_account_not_linked_tells_owner_to_sign_in_to_app(self):
        self.server.errors.append(http_error(400, "Bad Request", {"error": {
            "code": 400, "message": "The user has not linked their account.", "status": "FAILED_PRECONDITION",
            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "ACCOUNT_NOT_LINKED"}],
        }}))

        with self.assertRaises(fitbit_api.FitbitAuthError) as caught:
            self.client.get_steps("2026-01-20", "2026-01-20")

        self.assertNotIsInstance(caught.exception, fitbit_api.FitbitReauthRequiredError)
        self.assertIn("sign in to the Google Health app", str(caught.exception))

    def test_read_timeout_names_the_failed_operation(self):
        self.server.errors.append(TimeoutError("timed out"))

        with self.assertRaisesRegex(fitbit_api.GoogleHealthApiError, "steps dailyRollUp failed: timed out"):
            self.client.get_steps("2026-01-20", "2026-01-20")

    def test_other_http_error_names_the_failed_operation(self):
        self.server.errors.append(http_error(500, "Internal Server Error", {"error": {"code": 500, "message": "boom"}}))

        with self.assertRaisesRegex(fitbit_api.GoogleHealthApiError, "steps dailyRollUp failed: HTTP 500 boom"):
            self.client.get_steps("2026-01-20", "2026-01-20")


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 20, 8, 0, 0)


class EndToEndTests(AdapterTestCase):
    def test_golden_day_through_google_matches_briefing_golden(self):
        stdout, _, exit_code = run_briefing(fitbit_api.FitbitClient(), "text", target_date="2026-01-20")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, read_golden("full_day.text.txt"))

    def test_alerts_from_google_match_alerts_from_fitbit_shaped_data(self):
        def run_alerts(client):
            stdout = io.StringIO()
            with mock.patch.object(alerts, "FitbitClient", return_value=client), \
                    mock.patch.object(alerts, "datetime", FixedDatetime), \
                    mock.patch.object(sys, "argv", ["alerts.py", "--days", "7", "--json"]), \
                    redirect_stdout(stdout):
                alerts.main()
            return json.loads(stdout.getvalue())

        from_fitbit = run_alerts(FakeFitbitClient(load_fixture("full_day.json")))
        from_google = run_alerts(fitbit_api.FitbitClient())

        self.assertTrue(from_fitbit["alerts"])
        self.assertEqual(from_google, from_fitbit)


if __name__ == "__main__":
    unittest.main()

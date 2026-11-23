#!/usr/bin/env python3
"""
Fitbit data client backed by the Google Health API v4.

The legacy Fitbit Web API is shut down. FitbitClient reads the same metrics from
Google Health and returns them in the Fitbit Web API JSON shapes that the
briefing, alerts and this CLI already parse.

Login once with scripts/google_health_auth.py (see SETUP.md).

Usage:
    python fitbit_api.py activity --days 7
    python fitbit_api.py heartrate --days 7
    python fitbit_api.py sleep --days 7
    python fitbit_api.py report --type weekly
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from google_health_auth import FitbitAuthError  # noqa: E402
from google_health_auth import FitbitReauthRequiredError  # noqa: E402
from google_health_auth import GoogleHealthAuth  # noqa: E402
from google_health_auth import reauth_message  # noqa: E402

API_BASE_URL = "https://health.googleapis.com/v4/users/me/dataTypes"
HTTP_TIMEOUT_SECONDS = 10

# dailyRollUp accepts at most this many days per request; other types allow 90.
ROLLUP_MAX_DAYS = {
    "total-calories": 14,
    "active-minutes": 14,
    "heart-rate": 14,
    "calories-in-heart-rate-zone": 14,
    "time-in-heart-rate-zone": 14,
}
DEFAULT_ROLLUP_MAX_DAYS = 90
SESSION_PAGE_SIZE = 25  # sleep and exercise pages hold at most 25 sessions

HEART_RATE_ZONES = (
    ("LIGHT", "Out of Range"),
    ("MODERATE", "Fat Burn"),
    ("VIGOROUS", "Cardio"),
    ("PEAK", "Peak"),
)
# Zone thresholds change slowly, so a day without its own record uses the latest
# record from up to this many days before it.
ZONE_THRESHOLD_LOOKBACK_DAYS = 7

SLEEP_STAGE_LEVELS = {"AWAKE": "wake", "LIGHT": "light", "DEEP": "deep", "REM": "rem"}
CLASSIC_SLEEP_LEVELS = {"AWAKE": "awake", "ASLEEP": "asleep", "RESTLESS": "restless"}


class GoogleHealthApiError(RuntimeError):
    """A Google Health data request failed for a reason other than auth."""


def _to_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _dates(start_date, end_date):
    start, end = _to_date(start_date), _to_date(end_date)
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def _civil_date(value):
    return {"year": value.year, "month": value.month, "day": value.day}


def _date_string(civil):
    if civil and "date" in civil:
        civil = civil["date"]
    if not civil:
        return None
    return f"{int(civil['year']):04d}-{int(civil['month']):02d}-{int(civil['day']):02d}"


def _civil_datetime_string(civil):
    day = _date_string(civil)
    if not day:
        return None
    time_of_day = civil.get("time") or {}
    return (
        f"{day}T{int(time_of_day.get('hours', 0)):02d}:{int(time_of_day.get('minutes', 0)):02d}:"
        f"{int(time_of_day.get('seconds', 0)):02d}.000"
    )


def _interval_local(interval, which):
    """(local date, local datetime string) for an interval's "Start" or "End".

    The live v4 API sends only the UTC time and its UTC offset for sleep and exercise intervals;
    a civil time, when present, takes precedence.
    """
    civil = interval.get(f"civil{which}Time")
    if civil:
        return _date_string(civil), _civil_datetime_string(civil)
    utc = _timestamp(interval.get(f"{which.lower()}Time"))
    if utc is None:
        return None, None
    local = utc + timedelta(seconds=_duration_seconds(interval.get(f"{which.lower()}UtcOffset")))
    return local.date().isoformat(), local.strftime("%Y-%m-%dT%H:%M:%S.000")


def _duration_seconds(value):
    if not value:
        return 0.0
    return float(str(value).rstrip("s"))


def _timestamp(value):
    """Parse an RFC 3339 timestamp, tolerating nanosecond fractions."""
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        zone = rest[len(digits):]
        text = f"{head}.{digits[:6]}{zone}"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _camel(data_type):
    head, *rest = data_type.split("-")
    return head + "".join(part.capitalize() for part in rest)


def _date_filter(field, start_date, end_date):
    end_exclusive = (_to_date(end_date) + timedelta(days=1)).isoformat()
    return f'{field} >= "{start_date}" AND {field} < "{end_exclusive}"'


def _km(millimeters):
    return round(_int(millimeters) / 1e6, 2)


class FitbitClient:
    """Reads Fitbit data through Google Health API v4 in Fitbit Web API shapes."""

    def __init__(self):
        self._auth = GoogleHealthAuth()

    def _request(self, method, path, operation, query=None, body=None, allow_retry=True):
        url = f"{API_BASE_URL}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bearer {self._auth.access_token()}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 401 and allow_retry:
                self._auth.refresh(force=True)
                return self._request(method, path, operation, query, body, allow_retry=False)
            raise self._api_error(error, operation) from error
        except (urllib.error.URLError, OSError) as error:
            # urllib wraps only send-side failures in URLError; a read timeout
            # surfaces as a bare TimeoutError.
            reason = getattr(error, "reason", error)
            raise GoogleHealthApiError(f"Google Health {operation} failed: {reason}") from error

    def _api_error(self, error, operation):
        try:
            raw = error.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            message = json.loads(raw).get("error", {}).get("message")
        except (ValueError, AttributeError):
            message = None

        if error.code == 403 and (
            "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in raw or "insufficient authentication scopes" in raw.lower()
        ):
            return FitbitReauthRequiredError(reauth_message("lacks a required scope"))
        if "ACCOUNT_NOT_LINKED" in raw:
            return FitbitAuthError(
                "Google Health account is not linked: sign in to the Google Health app on the phone, then retry."
            )
        if error.code == 401:
            return FitbitAuthError(f"Google Health {operation} rejected a refreshed token (HTTP 401).")
        return GoogleHealthApiError(f"Google Health {operation} failed: HTTP {error.code} {message or error.reason}")

    def _pages(self, method, path, operation, query=None, body=None):
        page_token = None
        while True:
            page_query = dict(query or {})
            page_body = dict(body) if body is not None else None
            if page_token and page_body is not None:
                page_body["pageToken"] = page_token
            elif page_token:
                page_query["pageToken"] = page_token
            page = self._request(method, path, operation, page_query or None, page_body)
            yield page
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    def _daily_rollup(self, data_type, start_date, end_date):
        field = _camel(data_type)
        max_days = ROLLUP_MAX_DAYS.get(data_type, DEFAULT_ROLLUP_MAX_DAYS)
        operation = f"{data_type} dailyRollUp"
        values = {}
        chunk_start, end = _to_date(start_date), _to_date(end_date)
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=max_days - 1), end)
            body = {
                "range": {
                    "start": {"date": _civil_date(chunk_start)},
                    "end": {"date": _civil_date(chunk_end + timedelta(days=1))},
                },
                "windowSizeDays": 1,
                "pageSize": (chunk_end - chunk_start).days + 1,
            }
            for page in self._pages("POST", f"{data_type}/dataPoints:dailyRollUp", operation, body=body):
                for point in page.get("rollupDataPoints", []):
                    day = _date_string(point.get("civilStartTime"))
                    if day and point.get(field) is not None:
                        values[day] = point[field]
            chunk_start = chunk_end + timedelta(days=1)
        return values

    def _data_points(self, data_type, filter_text, method="list", page_size=None):
        path = f"{data_type}/dataPoints" + (":reconcile" if method == "reconcile" else "")
        query = {"filter": filter_text}
        if page_size:
            query["pageSize"] = page_size
        points = []
        for page in self._pages("GET", path, f"{data_type} {method}", query=query):
            points.extend(page.get("dataPoints", []))
        return points

    def _daily_records(self, data_type, start_date, end_date):
        """(date, record) for each record of a daily data type in the range."""
        key = _camel(data_type)
        field = data_type.replace("-", "_") + ".date"
        for point in self._data_points(data_type, _date_filter(field, start_date, end_date)):
            record = point.get(key) or {}
            yield _date_string(record.get("date")), record

    def get_steps(self, start_date, end_date):
        """Daily step totals as Fitbit `activities-steps`."""
        rollup = self._daily_rollup("steps", start_date, end_date)
        return {"activities-steps": [
            {"dateTime": day, "value": str(_int(rollup.get(day, {}).get("countSum")))}
            for day in _dates(start_date, end_date)
        ]}

    def get_calories(self, start_date, end_date):
        """Daily total calories as Fitbit `activities-calories`."""
        rollup = self._daily_rollup("total-calories", start_date, end_date)
        return {"activities-calories": [
            {"dateTime": day, "value": str(_int(rollup.get(day, {}).get("kcalSum")))}
            for day in _dates(start_date, end_date)
        ]}

    def get_distance(self, start_date, end_date):
        """Daily distance in km as Fitbit `activities-distance`."""
        rollup = self._daily_rollup("distance", start_date, end_date)
        return {"activities-distance": [
            {"dateTime": day, "value": str(_km(rollup.get(day, {}).get("millimetersSum")))}
            for day in _dates(start_date, end_date)
        ]}

    def get_activity_summary(self, start_date, end_date=None):
        """One day's activity summary (distance in km, floors, active minutes)."""
        distance = self._daily_rollup("distance", start_date, start_date).get(start_date, {})
        floors = self._daily_rollup("floors", start_date, start_date).get(start_date, {})
        active = self._daily_rollup("active-minutes", start_date, start_date).get(start_date, {})

        minutes_by_level = {
            entry.get("activityLevel"): _int(entry.get("activeMinutesSum"))
            for entry in active.get("activeMinutesRollupByActivityLevel", [])
        }
        distance_km = _km(distance.get("millimetersSum"))
        return {"summary": {
            "distance": distance_km,
            "distances": [{"activity": "total", "distance": distance_km}],
            "floors": _int(floors.get("countSum")),
            "veryActiveMinutes": minutes_by_level.get("VIGOROUS", 0),
            "fairlyActiveMinutes": minutes_by_level.get("MODERATE", 0),
            "lightlyActiveMinutes": minutes_by_level.get("LIGHT", 0),
        }}

    def get_logged_activities(self, date):
        """Exercises started on one date as Fitbit `activities[]` (duration in ms)."""
        points = self._data_points(
            "exercise",
            _date_filter("exercise.interval.civil_start_time", date, date),
            page_size=SESSION_PAGE_SIZE,
        )
        activities = []
        for point in points:
            exercise = point.get("exercise") or {}
            interval = exercise.get("interval") or {}
            seconds = _duration_seconds(exercise.get("activeDuration"))
            if not seconds and interval.get("startTime") and interval.get("endTime"):
                seconds = (_timestamp(interval["endTime"]) - _timestamp(interval["startTime"])).total_seconds()
            activities.append({
                "name": exercise.get("displayName", "Unknown"),
                "duration": int(seconds * 1000),
                "calories": _int((exercise.get("metricsSummary") or {}).get("caloriesKcal")),
                "startTime": _interval_local(interval, "Start")[1],
            })
        activities.sort(key=lambda activity: activity["startTime"] or "")
        return {"activities": activities}

    def get_active_zone_minutes(self, start_date, end_date):
        """Daily Active Zone Minutes as Fitbit `activities-active-zone-minutes`.

        Google's per-zone sums are already credited (one per fat-burn minute, two
        per cardio or peak minute), so the total is their sum, as in Fitbit.
        """
        rollup = self._daily_rollup("active-zone-minutes", start_date, end_date)
        days = []
        for day in _dates(start_date, end_date):
            value = rollup.get(day)
            if not value:
                continue
            fat_burn = _int(value.get("sumInFatBurnHeartZone"))
            cardio = _int(value.get("sumInCardioHeartZone"))
            peak = _int(value.get("sumInPeakHeartZone"))
            total = fat_burn + cardio + peak
            if total:
                days.append({"dateTime": day, "value": {
                    "activeZoneMinutes": total,
                    "fatBurnActiveZoneMinutes": fat_burn,
                    "cardioActiveZoneMinutes": cardio,
                    "peakActiveZoneMinutes": peak,
                }})
        return {"activities-active-zone-minutes": days}

    def get_heartrate(self, start_date, end_date):
        """Resting HR and heart-rate zones per day as Fitbit `activities-heart`."""
        resting = {}
        for day, record in self._daily_records("daily-resting-heart-rate", start_date, end_date):
            if day and record.get("beatsPerMinute") is not None:
                resting[day] = _int(record["beatsPerMinute"])

        lookback_start = (_to_date(start_date) - timedelta(days=ZONE_THRESHOLD_LOOKBACK_DAYS)).isoformat()
        thresholds = {}
        for day, record in self._daily_records("daily-heart-rate-zones", lookback_start, end_date):
            if day:
                thresholds[day] = {
                    zone.get("heartRateZoneType"): (_int(zone.get("minBeatsPerMinute")), _int(zone.get("maxBeatsPerMinute")))
                    for zone in record.get("heartRateZones", [])
                }

        time_in_zone = self._daily_rollup("time-in-heart-rate-zone", start_date, end_date)

        days = []
        for day in _dates(start_date, end_date):
            known = [threshold_day for threshold_day in thresholds if threshold_day <= day]
            day_thresholds = thresholds[max(known)] if known else {}
            minutes = {
                entry.get("heartRateZone"): round(_duration_seconds(entry.get("duration")) / 60)
                for entry in (time_in_zone.get(day) or {}).get("timeInHeartRateZones", [])
            }
            zones = []
            for zone_type, name in HEART_RATE_ZONES:
                zone = {"name": name, "minutes": minutes.get(zone_type, 0)}
                if zone_type in day_thresholds:
                    zone["min"], zone["max"] = day_thresholds[zone_type]
                zones.append(zone)
            value = {"customHeartRateZones": [], "heartRateZones": zones}
            if day in resting:
                value["restingHeartRate"] = resting[day]
            days.append({"dateTime": day, "value": value})
        return {"activities-heart": days}

    def get_sleep(self, start_date, end_date):
        """Sleep sessions by the date they end, main sleep first within a date.

        Google Health does not provide sleep efficiency, so entries have no
        `efficiency` key.
        """
        points = self._data_points(
            "sleep",
            _date_filter("sleep.interval.civil_end_time", start_date, end_date),
            method="reconcile",
            page_size=SESSION_PAGE_SIZE,
        )
        sessions = [self._sleep_entry(point.get("sleep") or {}) for point in points]
        sessions = [session for session in sessions if session["dateOfSleep"]]
        sessions.sort(key=lambda session: (session["dateOfSleep"], not session["isMainSleep"], session["startTime"] or ""))
        return {"sleep": sessions}

    def get_sleep_stages(self, start_date, end_date):
        """Sleep sessions including the per-stage `levels.summary`."""
        return self.get_sleep(start_date, end_date)

    def _sleep_entry(self, sleep):
        interval = sleep.get("interval") or {}
        summary = sleep.get("summary") or {}
        metadata = sleep.get("metadata") or {}
        classic = sleep.get("type") == "CLASSIC"
        level_names = CLASSIC_SLEEP_LEVELS if classic else SLEEP_STAGE_LEVELS

        levels = {}
        for stage in summary.get("stagesSummary", []):
            name = level_names.get(stage.get("type"))
            if name:
                levels[name] = {"count": _int(stage.get("count")), "minutes": _int(stage.get("minutes"))}

        minutes_in_period = _int(summary.get("minutesInSleepPeriod"))
        return {
            "dateOfSleep": _interval_local(interval, "End")[0],
            "startTime": _interval_local(interval, "Start")[1],
            "endTime": _interval_local(interval, "End")[1],
            "duration": minutes_in_period * 60000,
            "timeInBed": minutes_in_period,
            "minutesAsleep": _int(summary.get("minutesAsleep")),
            "minutesAwake": _int(summary.get("minutesAwake")),
            "minutesToFallAsleep": _int(summary.get("minutesToFallAsleep")),
            "minutesAfterWakeup": _int(summary.get("minutesAfterWakeUp")),
            "isMainSleep": bool(metadata.get("mainSleep")),
            "type": "classic" if classic else "stages",
            "levels": {"summary": levels},
        }

    def get_spo2(self, start_date, end_date):
        """Daily SpO2 (sleep average and confidence bounds) as Fitbit spo2 entries."""
        days = []
        for day, record in self._daily_records("daily-oxygen-saturation", start_date, end_date):
            if day:
                days.append({"dateTime": day, "value": {
                    "avg": record.get("averagePercentage"),
                    "min": record.get("lowerBoundPercentage"),
                    "max": record.get("upperBoundPercentage"),
                }})
        days.sort(key=lambda entry: entry["dateTime"])
        return days

    def get_weight(self, start_date, end_date):
        """Weight in kg as Fitbit `body-weight`, the last measurement of each date."""
        latest = {}
        for point in self._data_points(
            "weight",
            _date_filter("weight.sample_time.civil_time", start_date, end_date),
        ):
            record = point.get("weight") or {}
            sample_time = record.get("sampleTime") or {}
            day = _date_string(sample_time.get("civilTime"))
            if not day or record.get("weightGrams") is None:
                continue
            taken_at = sample_time.get("physicalTime") or ""
            if day not in latest or taken_at >= latest[day][0]:
                latest[day] = (taken_at, float(record["weightGrams"]))
        return {"body-weight": [
            {"dateTime": day, "value": str(round(grams / 1000, 2))}
            for day, (_, grams) in sorted(latest.items())
        ]}


class FitbitAnalyzer:
    """Analyze Fitbit data"""

    def __init__(self, steps_data=None, hr_data=None, sleep_data=None, activity_data=None):
        self.steps = steps_data or []
        self.hr = hr_data or []
        self.sleep = sleep_data or []
        self.activity = activity_data or []

    def average_metric(self, data, key):
        """Calculate average of a metric"""
        if not data:
            return None
        # Convert to float to handle string values from API
        values = []
        for d in data:
            val = d.get(key)
            if val is not None:
                try:
                    values.append(float(val))
                except (ValueError, TypeError):
                    continue
        return sum(values) / len(values) if values else None

    def trend(self, data, key, days=7):
        """Calculate trend over N days"""
        if len(data) < 2:
            return 0
        recent = data[-days:]
        if len(recent) < 2:
            return 0
        try:
            first = float(recent[0].get(key, 0))
            last = float(recent[-1].get(key, 0))
            return last - first
        except (ValueError, TypeError):
            return 0

    def summary(self):
        """Generate summary"""
        steps_data = self.steps.get("activities-steps", []) if self.steps else []
        hr_data = self.hr.get("activities-heart", []) if self.hr else []

        avg_steps = self.average_metric(steps_data, "value")

        # Extract resting HR
        resting_hrs = []
        for day in hr_data:
            value = day.get("value", {})
            if isinstance(value, dict):
                resting_hrs.append(value.get("restingHeartRate"))
            else:
                resting_hrs.append(value)

        avg_rhr = sum([r for r in resting_hrs if r]) / len([r for r in resting_hrs if r]) if resting_hrs else None

        return {
            "avg_steps": avg_steps,
            "avg_resting_hr": avg_rhr,
            "steps_trend": self.trend(steps_data, "value"),
            "days_tracked": len(steps_data)
        }


class FitbitReporter:
    """Generate Fitbit reports"""

    def __init__(self, client):
        self.client = client

    def generate_report(self, report_type="weekly", days=None):
        """Generate report"""
        if not days:
            days = 7 if report_type == "weekly" else 30

        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        steps = self.client.get_steps(start_date, end_date)
        hr = self.client.get_heartrate(start_date, end_date)
        sleep = self.client.get_sleep(start_date, end_date)

        analyzer = FitbitAnalyzer(steps, hr, sleep)
        summary = analyzer.summary()

        return {
            "report_type": report_type,
            "period": f"{start_date} to {end_date}",
            "summary": summary,
            "data": {
                "steps": steps,
                "heartrate": hr,
                "sleep": sleep
            }
        }




def main():
    parser = argparse.ArgumentParser(description="Fitbit Analytics CLI (Google Health API)")
    parser.add_argument("command", choices=["activity", "steps", "calories", "heartrate", "sleep", "report", "summary"],
                       help="Data type to fetch or report type")
    parser.add_argument("--days", type=int, default=7, help="Number of days")
    parser.add_argument("--type", default="weekly", help="Report type")

    args = parser.parse_args()

    try:
        client = FitbitClient()

        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

        if args.command in ["activity", "steps"]:
            data = client.get_steps(start_date, end_date)
            print(json.dumps(data, indent=2))

        elif args.command == "calories":
            data = client.get_calories(start_date, end_date)
            print(json.dumps(data, indent=2))

        elif args.command == "heartrate":
            data = client.get_heartrate(start_date, end_date)
            print(json.dumps(data, indent=2))

        elif args.command == "sleep":
            data = client.get_sleep(start_date, end_date)
            print(json.dumps(data, indent=2))

        elif args.command == "summary":
            steps = client.get_steps(start_date, end_date)
            hr = client.get_heartrate(start_date, end_date)
            analyzer = FitbitAnalyzer(steps, hr)
            summary = analyzer.summary()
            print(json.dumps(summary, indent=2))

        elif args.command == "report":
            reporter = FitbitReporter(client)
            report = reporter.generate_report(args.type, args.days)
            print(json.dumps(report, indent=2))

    except FitbitAuthError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"API Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

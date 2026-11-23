---
name: fitbit-analytics
description: Fitbit health and fitness data integration. Fetch steps, heart rate, sleep, activity, calories, and trends through the Google Health API (the Fitbit Web API replacement). Generate automated health reports and alerts. Requires a one-time Google login (scripts/google_health_auth.py login).
metadata: {"openclaw":{"requires":{"bins":["python3"]},"homepage":"https://github.com/kesslerio/fitbit-analytics-openclaw-skill"}}
---

# Fitbit Analytics

## Quick Start

```bash
# One-time Google login (see SETUP.md for the Google Cloud project)
python scripts/google_health_auth.py login --client-secrets /path/to/client_secret.json

# Generate morning briefing with Active Zone Minutes
python scripts/fitbit_briefing.py

# Fetch daily steps
python scripts/fitbit_api.py steps --days 7

# Get heart rate data
python scripts/fitbit_api.py heartrate --days 7

# Sleep summary
python scripts/fitbit_api.py sleep --days 7

# Generate weekly health report
python scripts/fitbit_api.py report --type weekly

# Get activity summary
python scripts/fitbit_api.py summary --days 7
```

## When to Use

Use this skill when:
- Fetching Fitbit metrics (steps, calories, heart rate, sleep)
- Analyzing activity trends over time
- Setting up alerts for inactivity or abnormal heart rate
- Generating daily/weekly health reports

## Core Workflows

### 1. Daily Briefing
```bash
# Generate morning health briefing (includes Active Zone Minutes)
python scripts/fitbit_briefing.py                    # Today's briefing
python scripts/fitbit_briefing.py --date 2026-01-20  # Specific date
python scripts/fitbit_briefing.py --format brief     # 3-line summary
python scripts/fitbit_briefing.py --format json      # JSON output

# Example output includes:
# - Yesterday's activities (logged exercises)
# - Yesterday's Active Zone Minutes (total, Fat Burn, Cardio, Peak)
# - Today's activity summary (steps, calories, floors, distance)
# - Heart rate (resting, average, zones)
# - Sleep (duration, awake minutes)
# - Trends vs 7-day average
```

**Example JSON output:**
```json
{
  "date": "2026-01-21",
  "steps_today": 8543,
  "calories_today": 2340,
  "distance_today": 6.8,
  "floors_today": 12,
  "active_minutes": 47,
  "resting_hr": 58,
  "avg_hr": 72,
  "sleep_hours": 7.2,
  "sleep_efficiency": null,
  "awake_count": 12,
  "activity_level": "Moderate",
  "steps_trend": 12
}
```

Yesterday's activities and Active Zone Minutes appear in the text format only.

**Exit codes:** 0 when the briefing was printed, 1 for any other failure (the last stderr line is a one-line `ERROR:` summary), and 2 when Google Health needs a new login (stderr is one line naming the login command).

**Note:** Sleep efficiency, Daily Readiness and Cardio Load are not available from the Google Health API. The report leaves efficiency out; the skill does not estimate it.

### 2. Data Fetching (CLI)
```bash
# Available commands:
python scripts/fitbit_api.py steps --days 7
python scripts/fitbit_api.py calories --days 7
python scripts/fitbit_api.py heartrate --days 7
python scripts/fitbit_api.py sleep --days 7
python scripts/fitbit_api.py summary --days 7
python scripts/fitbit_api.py report --type weekly
```

### 3. Data Fetching (Python API)
```bash
export PYTHONPATH="{baseDir}/scripts"
python - <<'PY'
from fitbit_api import FitbitClient

client = FitbitClient()  # Uses the login stored in ~/.fitbit-analytics/google-health.json

# Fetch data (requires start_date and end_date)
steps_data = client.get_steps(start_date="2026-01-01", end_date="2026-01-16")
hr_data = client.get_heartrate(start_date="2026-01-01", end_date="2026-01-16")
sleep_data = client.get_sleep(start_date="2026-01-01", end_date="2026-01-16")
activity_summary = client.get_activity_summary(start_date="2026-01-16")
PY
```

### 4. Analysis
```bash
export PYTHONPATH="{baseDir}/scripts"
python - <<'PY'
from fitbit_api import FitbitAnalyzer

analyzer = FitbitAnalyzer(steps_data, hr_data)
summary = analyzer.summary()
print(summary)  # Returns: avg_steps, avg_resting_hr, step_trend
PY
```

### 5. Alerts
```bash
python {baseDir}/scripts/alerts.py --days 7 --steps 8000 --sleep 7
```

## Scripts

- `scripts/fitbit_api.py` - Data client (Google Health API in Fitbit-shaped JSON), CLI, and analysis
- `scripts/google_health_auth.py` - One-time Google login and on-demand token refresh
- `scripts/fitbit_briefing.py` - Morning briefing CLI (text/brief/json output)
- `scripts/alerts.py` - Threshold-based notifications

## Available API Methods

| Method | Description |
|--------|-------------|
| `get_steps(start, end)` | Daily step counts |
| `get_calories(start, end)` | Daily calories burned |
| `get_distance(start, end)` | Daily distance |
| `get_activity_summary(date)` | One day's activity summary |
| `get_logged_activities(date)` | Exercises logged on one date |
| `get_heartrate(start, end)` | Heart rate data |
| `get_sleep(start, end)` | Sleep sessions (no efficiency) |
| `get_sleep_stages(start, end)` | Detailed sleep stages |
| `get_spo2(start, end)` | Blood oxygen levels |
| `get_weight(start, end)` | Weight measurements |
| `get_active_zone_minutes(start, end)` | Active Zone Minutes (AZM) breakdown |

## References

- `references/google-health-api.md` - Google Health API endpoints, metric mapping, and error handling
- `references/metrics.md` - Metric definitions and interpretations

## Authentication

The Google Health API uses Google OAuth 2.0. See `SETUP.md`:
1. Create a Google Cloud project, enable the Google Health API, and create an OAuth client (consent screen External, In Production).
2. Run `python scripts/google_health_auth.py login --client-secrets <client JSON>` once. Open the printed URL in any browser and paste back the URL it lands on.
3. The login is stored in `~/.fitbit-analytics/google-health.json` (mode 0600). The access token refreshes on demand; no refresh job is needed.
4. To log in again later, run `python scripts/google_health_auth.py login` (it reuses the stored client).

## Environment

No environment variables are needed. Credentials are never read from the environment or `secrets.conf`.

## Automation (Cron Jobs)

Cron jobs are configured in OpenClaw's gateway, not in this repo. Add these to your OpenClaw setup:

### Daily Morning Briefing (8:00 AM)
```bash
openclaw cron add \
  --name "Morning Fitbit Health Report" \
  --cron "0 8 * * *" \
  --tz "America/Los_Angeles" \
  --session isolated \
  --wake next-heartbeat \
  --deliver \
  --channel telegram \
  --target "<YOUR_TELEGRAM_CHAT_ID>" \
  --message "python3 /path/to/your/scripts/fitbit_briefing.py --format text"
```

**Note:** Replace `/path/to/your/` with your actual path and `<YOUR_TELEGRAM_CHAT_ID>` with your Telegram channel/group ID.

No token refresh job is needed: Google refresh tokens do not rotate, and the client refreshes the access token when it runs. Have the scheduler alert on a non-zero exit so a needed re-login (exit 2) is noticed the same day.

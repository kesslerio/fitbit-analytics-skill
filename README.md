# Fitbit Analytics Skill for OpenClaw 🦞

[![OpenClaw Community Skill](https://img.shields.io/badge/openclaw-community%20skill-blue)](https://github.com/openclaw/openclaw)
[![ClawdHub Listed](https://img.shields.io/badge/clawdhub-listed-green)](https://clawdhub.com/skills/fitbit-analytics)

Fitbit health and fitness data integration for OpenClaw. Fetch steps, heart rate, sleep, activity, calories, and trends through the Google Health API (the replacement for the shut-down Fitbit Web API). Generate automated health reports, correlations, and alerts.

## Features

- **Activity Tracking**: Fetch daily steps, distance, calories, floors, and active minutes.
- **Heart Rate**: Resting heart rate and time in heart-rate zones.
- **Sleep Analytics**: Sleep duration, minutes awake, and sleep stages (Deep, Light, REM, Wake). Sleep efficiency is not available from the Google Health API, so the report leaves it out.
- **Reports**: Generate daily/weekly health reports with trend analysis.
- **Automation**: A morning briefing with exit codes a scheduler can alert on.

## Setup

Google turned off the Fitbit Web API and Fitbit OAuth, so this skill uses the
Google Health API v4 with a one-time Google login. Follow [SETUP.md](SETUP.md):

1. Sign in to the Google Health app (the renamed Fitbit app) on your phone.
2. Create a Google Cloud project, enable the Google Health API, set the OAuth
   consent screen to External and In Production, and create an OAuth client.
3. Log in once:
   ```bash
   python3 scripts/google_health_auth.py login --client-secrets /path/to/client_secret.json
   ```
   Open the printed URL in any browser, approve, and paste back the URL the
   browser lands on. The command works on a headless server.

## Usage

> Note: For Python imports, set `PYTHONPATH` to the `scripts/` folder:
>
> ```bash
> export PYTHONPATH="$(pwd)/scripts"
> ```

### Fetch Daily Stats
```bash
# Get steps for today
python scripts/fitbit_api.py steps --days 1

# Get sleep data
python scripts/fitbit_api.py sleep --days 1
```

### Generate Weekly Report
```bash
python scripts/fitbit_api.py report --type weekly
```

### Daily Morning Briefing
```bash
# Text format (for Telegram)
python scripts/fitbit_briefing.py --format text

# Brief format (3-line summary)
python scripts/fitbit_briefing.py --format brief

# JSON format (for programmatic use)
python scripts/fitbit_briefing.py --format json
```

**Example brief output:**
```
📊 8,543 steps • 2,340 cal
❤️ Resting HR: 58 • 💤 7.2h sleep
🏃 Moderate • ↑ 12% vs avg
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

## Structure
- `scripts/fitbit_api.py`: Data client (Google Health API in Fitbit-shaped JSON) and CLI tool.
- `scripts/google_health_auth.py`: One-time Google login and token refresh.
- `scripts/fitbit_briefing.py`: Morning briefing CLI (text/brief/json output).
- `scripts/alerts.py`: Threshold-based notifications.
- `references/`: API and metrics documentation.
- `docs/`: Privacy Policy and Terms of Service.

## Token Refresh Behavior
- All auth state lives in `~/.fitbit-analytics/google-health.json` (mode 0600). Environment variables and `secrets.conf` are not read.
- The client refreshes the access token on demand when it expires within 5 minutes, and once more after an HTTP 401.
- Google refresh tokens are not rotated, so no refresh cron job is needed. A refresh token lasts until it is revoked or unused for 6 months (7 days if the OAuth client is left in Testing status).
- When Google rejects the refresh token, or a required scope is missing, the client raises `FitbitReauthRequiredError` with the login command to run.

## Automation
Schedule the briefing and alert on a non-zero exit:

| Exit code | Meaning |
|-----------|---------|
| 0 | The briefing was printed |
| 1 | Another failure; the last stderr line is a one-line `ERROR:` summary |
| 2 | Google Health needs a new login; stderr is one line naming the login command |

```bash
0 8 * * * cd /path/to/fitbit-analytics-openclaw-skill && python3 scripts/fitbit_briefing.py --format text
```

## License
Apache 2.0

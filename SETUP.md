# Fitbit Analytics Skill Setup Guide

Google shut down the Fitbit Web API and Fitbit OAuth (the old endpoints answer
HTTP 410; the shutdown message says 2026-10-30, Google's about page says
2026-11-02). This skill now reads your Fitbit data through the **Google Health
API v4** with Google OAuth. Old Fitbit tokens and developer apps no longer work.

You need Python 3.10 or newer. The skill uses only the standard library.

## 1. Check your account

1. Your Fitbit account must have been moved to a Google account.
2. Sign in to the **Google Health app** (the renamed Fitbit app) on your phone and
   check that last night's sleep and today's steps appear. Until you do, the API
   answers `ACCOUNT_NOT_LINKED`.
3. Let the Google Health app run in the background, so the phone syncs your
   tracker before the morning briefing runs.

## 2. Create a Google Cloud project

Use your personal Google account, not a work (Workspace) account.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project. If project creation offers a Personal (sole-user) mode, note the
   choice: it cannot be changed later.
2. Enable the **Google Health API** for the project.
3. In **Google Auth Platform**:
   - Set the user type to **External** and add yourself as a test user.
   - Add these scopes:
     - `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`
     - `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly`
     - `https://www.googleapis.com/auth/googlehealth.sleep.readonly`
   - Set the publishing status to **In Production**. In Testing status Google
     expires the login every 7 days, and the briefing will ask you to log in again
     weekly.
4. Create an **OAuth client**: type *Web application* with the authorized
   redirect URI `https://www.google.com`. (A *Desktop* client also works; the
   login uses whatever redirect URI the client file names.)
5. Download the client JSON. Keep it in a password manager and delete the
   downloaded copy. The skill needs it only for the first login, or if its auth
   state file is lost.

## 3. Log in once

Run the login on the machine that will run the skill. It works on a headless
server: nothing listens on a port.

```bash
python3 scripts/google_health_auth.py login --client-secrets /path/to/client_secret.json
```

1. The command prints a consent URL. Open it in a browser on any device.
2. Approve access. Google may warn that it "hasn't verified this app"; for your
   own project, choose to continue. Leave every requested permission checked.
3. The browser lands on a page whose address starts with your redirect URI
   (for example `https://www.google.com/?state=...&code=...`). Copy the **full
   address** from the address bar.
4. Paste it at the `Redirect URL:` prompt.

If your session cannot answer the prompt, start the login with stdin closed and
finish it later with the pasted URL:

```bash
python3 scripts/google_health_auth.py login --client-secrets /path/to/client_secret.json < /dev/null
python3 scripts/google_health_auth.py login --redirect-url 'https://www.google.com/?state=...&code=...'
```

Delete the client JSON file once the login succeeds.

## 4. Auth state

The login writes everything the skill needs to one file:

```
~/.fitbit-analytics/google-health.json   (mode 0600)
```

It holds the OAuth client id and secret, the refresh token, the current access
token, its expiry and the granted scopes. The skill refreshes the access token on
demand; no cron job is needed. Environment variables and `secrets.conf` are not
read.

To log in again later (for example after revoking access), run the login without
`--client-secrets`; it reuses the client stored in the state file:

```bash
python3 scripts/google_health_auth.py login
```

## 5. Run

```bash
python3 scripts/fitbit_briefing.py --format text
python3 scripts/fitbit_api.py report --type weekly
```

`fitbit_briefing.py` exit codes:

| Code | Meaning |
|------|---------|
| 0 | The briefing was printed |
| 1 | Another failure; the last stderr line is a one-line `ERROR:` summary naming what failed |
| 2 | Google Health needs a new login; stderr is one line naming the cause and the login command |

A scheduler that alerts on a non-zero exit therefore tells you the same morning
when the login needs renewing.

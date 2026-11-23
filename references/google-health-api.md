# Google Health API v4 Reference

How this skill reads Fitbit data now that the Fitbit Web API is shut down.

## Status of this document

| Part | Source | State |
|------|--------|-------|
| Endpoints, request bodies, filters, field names, enums, limits | Google Health API v4 discovery document (`https://health.googleapis.com/$discovery/rest?version=v4`), read 2026-11-22 | Confirmed against the API definition |
| Publishing setup that keeps a refresh token past 7 days | Live account spike | **Pending** |
| OAuth client type, token response fields, granular-consent behavior | Live account spike | **Pending** |
| Heart-rate zone mapping and Active Zone Minutes credit, checked against the Google Health app | Live account spike | **Pending** |
| Whether today's resting heart rate exists at 08:00 | Live account spike | **Pending** |
| Refresh with the day-0 refresh token on day 8 or later | Live account spike | **Pending** |

The test fixtures in `tests/fixtures/google_health/` are synthetic. They copy the
response shapes from the API definition and contain no real account data.

## Legacy shutdown

Google turned off the Fitbit Web API and Fitbit OAuth; the old token endpoint
answers HTTP 410. The 410 message gives 2026-10-30 as the date, while Google's
about page says 2026-11-02. Fitbit tokens do not carry over: a new Google consent
is required.

## Authentication

- Authorization endpoint: `https://accounts.google.com/o/oauth2/v2/auth`
- Token endpoint: `https://oauth2.googleapis.com/token` (client id and secret in the POST body)
- Flow: authorization code with PKCE (`S256`) and `state`, `access_type=offline`,
  `prompt=consent`, no `include_granted_scopes`.
- Scopes (all Restricted):
  - `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`
  - `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly`
  - `https://www.googleapis.com/auth/googlehealth.sleep.readonly`
- Google's consent screen lets the user uncheck individual scopes, so the login
  rejects a grant that lacks any of the three.
- A refresh response normally has no `refresh_token`; the stored one is kept.
  `invalid_grant` means the grant was revoked or expired and a new login is needed.
- An OAuth client in Testing publishing status gets refresh tokens that expire
  after 7 days. In Production they last until revoked or unused for 6 months.
- The account must have signed in to the Google Health app (the renamed Fitbit
  app); otherwise data calls answer 400 `ACCOUNT_NOT_LINKED`.

## Methods used

All paths are under `https://health.googleapis.com/v4/users/me/dataTypes/{data-type}/dataPoints`.

| Method | HTTP | Used for | Notes |
|--------|------|----------|-------|
| `dailyRollUp` | `POST …:dailyRollUp` | Daily totals | Body: `range` (civil dates, end exclusive), `windowSizeDays: 1`, `pageSize`, `pageToken`. Maximum range 14 days for `total-calories`, `active-minutes`, `heart-rate` and `calories-in-heart-rate-zone`; 90 days for other types |
| `list` | `GET …` | Daily records, exercises, weight samples | `filter` (AIP-160), `pageSize`, `pageToken`. Exercise and sleep pages hold at most 25 items |
| `reconcile` | `GET …:reconcile` | Sleep | `list` does not support sleep with a data-source filter; `reconcile` merges sources |

Filters used:

| Data type | Filter field |
|-----------|--------------|
| `daily-resting-heart-rate` | `daily_resting_heart_rate.date` |
| `daily-heart-rate-zones` | `daily_heart_rate_zones.date` |
| `daily-oxygen-saturation` | `daily_oxygen_saturation.date` |
| `sleep` | `sleep.interval.civil_end_time` |
| `exercise` | `exercise.interval.civil_start_time` |
| `weight` | `weight.sample_time.civil_time` |

Each filter is `field >= "YYYY-MM-DD" AND field < "YYYY-MM-DD"` with an exclusive end.

The client splits a longer date range at the per-type maximum and follows
`nextPageToken`. It splits `time-in-heart-rate-zone` at 14 days as well, because
the definition does not state a limit for it.

## Metric mapping

Dates are civil dates in the host's local time zone. A night's sleep belongs to
the date it ends.

| Report field or client method | Fitbit shape returned | Google Health source |
|-------------------------------|-----------------------|----------------------|
| `get_steps` | `activities-steps[].value` (string) | `steps` dailyRollUp `steps.countSum` |
| `get_calories` | `activities-calories[].value` (whole kcal, string) | `total-calories` dailyRollUp `totalCalories.kcalSum`, rounded |
| `get_distance` | `activities-distance[].value` (km, string) | `distance` dailyRollUp `distance.millimetersSum` / 1,000,000 |
| `get_activity_summary` `summary.distance` | km | as above |
| `get_activity_summary` `summary.floors` | int | `floors` dailyRollUp `floors.countSum` |
| `get_activity_summary` `veryActiveMinutes`, `fairlyActiveMinutes`, `lightlyActiveMinutes` | minutes | `active-minutes` dailyRollUp, `VIGOROUS`, `MODERATE`, `LIGHT` |
| `get_heartrate` `restingHeartRate` | bpm, absent when there is no record | `daily-resting-heart-rate` list `beatsPerMinute` |
| `get_heartrate` `heartRateZones[]` minutes | minutes | `time-in-heart-rate-zone` dailyRollUp durations |
| `get_heartrate` `heartRateZones[]` min/max | bpm | `daily-heart-rate-zones` list; a day without a record uses the latest record from the previous 7 days |
| `get_sleep` `duration`, `timeInBed` | ms, minutes | `sleep` reconcile `summary.minutesInSleepPeriod` |
| `get_sleep` `minutesAsleep`, `minutesAwake` | minutes | `summary.minutesAsleep`, `summary.minutesAwake` |
| `get_sleep` `isMainSleep` | bool; the main sleep is listed first for its date | `metadata.mainSleep` |
| `get_sleep` `efficiency` | **not returned** | none: the API has no efficiency value and the skill does not estimate one |
| `get_sleep_stages` `levels.summary` | `wake`/`light`/`deep`/`rem` (or `awake`/`asleep`/`restless` for classic sleep) | `summary.stagesSummary` |
| `get_logged_activities` | `activities[]` `name`, `duration` (ms), `calories` | `exercise` list `displayName`, `activeDuration` (or the interval), `metricsSummary.caloriesKcal` |
| `get_active_zone_minutes` | four AZM fields | `active-zone-minutes` dailyRollUp `sumInFatBurnHeartZone`, `sumInCardioHeartZone`, `sumInPeakHeartZone`; total is their sum |
| `get_spo2` | `[{dateTime, value: {avg, min, max}}]` | `daily-oxygen-saturation` list: `averagePercentage`, `lowerBoundPercentage`, `upperBoundPercentage` |
| `get_weight` | `body-weight[].value` (kg, string), last sample per date | `weight` list `weightGrams` |

Heart-rate zones: Google reports four zones from the Karvonen formula. The
client maps them to Fitbit's names as `LIGHT` to Out of Range, `MODERATE` to Fat
Burn, `VIGOROUS` to Cardio and `PEAK` to Peak. This mapping still needs checking
against the Google Health app.

Active Zone Minutes: per the API definition, each Active Zone Minutes record
earns 1 in the fat-burn zone and 2 in the cardio and peak zones, so the
per-zone sums are already credited and the total is their sum, as Fitbit
reported it. This still needs checking against the Google Health app.

`get_activity_summary` returns only the `summary` block. The old Fitbit
response also carried the day's logged activities and goals; use
`get_logged_activities` for the activities.

## Error handling

| Response | Client behavior |
|----------|-----------------|
| 401 | Refresh once and retry; a second 401 raises `FitbitAuthError` |
| 403 naming insufficient scopes | `FitbitReauthRequiredError` (login again) |
| 400 `ACCOUNT_NOT_LINKED` | `FitbitAuthError` telling the owner to sign in to the Google Health app |
| Token endpoint `invalid_grant` | `FitbitReauthRequiredError` |
| Token endpoint 429, 5xx, network error or read timeout | Retry after 1, 2 and 4 seconds, then `FitbitAuthError` |
| Other data errors | `GoogleHealthApiError` naming the data type and method |

## Rate limits

300 requests per minute per user. The morning briefing makes about 13 requests.

## Spike findings

Pending: filled in after the live-account spike (publishing branch reached,
client type, token response fields, verified zone mapping and AZM credit,
08:00 resting heart rate, and the day-8 refresh result).

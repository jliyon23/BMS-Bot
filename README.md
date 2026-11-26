# BMS Bot

A Telegram watchlist for BookMyShow. Users pick a region and movie, choose which
alerts they want, and the bot checks every 10 seconds and pushes a message when
something changes.

BookMyShow has no public webhook, so "webhook-like" here means a fast poller plus
an immediate outbox delivery.

BMS requests go through the proxy pool by default. The admin can switch to
**direct mode** for local testing; production blocks it unless explicitly
allowed.

## Features

- PIN code → BMS region → now-showing movie list.
- Per-user, per-movie alert subscriptions with individual toggles:
  - 🏛️ new theatre
  - 🕒 new showtime
  - 📅 bookings opened for a date
  - 🔴 sold out
  - 🟠 seat status change
  - 🔢 exact seat count change (when seat-map data is available)
  - ❌ show removed
- Shared monitors: one scrape serves every user watching the same region + movie + date.
- 10-second scheduler with priority for seat-alert subscribers.
- Proxy hunter that validates each proxy with repeated BMS canaries and keeps a
  latency-prioritised pool.
- **Rotating proxy support**: accepted at a lower per-exit success rate and
  retried per request, with a failure-streak cooldown instead of one-403 death.
- Premium proxies can be added from the admin panel; once one is enabled the
  scraper uses **only** premium proxies and fails closed.
- Admin **Use proxy** toggle for direct-mode testing, with audit log and banner.
- Legacy-style server-rendered admin dashboard (users, watches, monitors, alerts,
  proxies, audit log, settings).
- SQLite + WAL, Alembic migrations, outbox alerts, audit log, structured logs.

## Architecture

```
Telegram Bot (aiogram 3) ──┐
                           │
Monitor Worker (10s) ──────┼── SQLite (WAL) ── Admin (FastAPI + Jinja2)
  ├─ showtimes parser      │
  ├─ seat-layout parser    │
  └─ alert fan-out         │
                           │
Proxy Hunter ──────────────┤
  └─ free + premium pool   │
                           │
Alert Sender (outbox) ─────┘
```

```
src/bmsbot/
  bms/          BMS client + parsers (proxy or direct mode)
  proxies/      sources, multi-iteration validator, DB pool, hunter worker
  monitor/      diff engine, shared-monitor registry, scanner, alert fan-out
  bot/          aiogram handlers, keyboards, texts, SQLite FSM storage
  services/     formatting, PIN lookup, runtime flags, Telegram sender
  workers/      monitor, proxy, alert entrypoints
  admin/        FastAPI app, auth, legacy Jinja templates
  db/           SQLAlchemy models, async session, repositories
  domain/       enums and shared constants
```

## Quick start (Docker, recommended)

```bash
cd /home/liyon/github-projects/BMS-bot/BMS-Bot
cp .env.example .env

# Required in .env:
#   BMS_ADMIN_SECRET_KEY=<48+ random chars>
#   BMS_TELEGRAM_BOT_TOKEN=<token>
#   BMS_PROXY_ENCRYPTION_KEY=<fernet key>
#   BMS_UID / BMS_GID = your local `id -u` / `id -g` (defaults 1000)

docker compose up -d --build
docker compose ps
docker compose logs -f proxy monitor admin
```

The compose file bind-mounts `./data:/data` and runs containers as your host UID,
so the local CLI and Docker use the **same** SQLite database.

Admin: <http://127.0.0.1:8080> (login with the admin user in that DB).

If the DB is fresh, create the admin once:

```bash
docker compose run --rm -it admin bmsbot create-admin
```

Useful:

```bash
docker compose exec admin bmsbot status
docker compose exec admin bmsbot doctor
docker compose exec admin bmsbot reconcile
docker compose run --rm admin bmsbot reset-admin-password
```

`reconcile` repairs active watches that were saved without monitors, for example
after an old database-lock failure. Safe to run repeatedly.

Stop everything with:

```bash
docker compose down
```

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

alembic upgrade head
bmsbot create-admin
bmsbot doctor
```

Run individual workers only when debugging:

```bash
bmsbot run-proxy
bmsbot run-monitor
bmsbot run-alerts
bmsbot run-bot
bmsbot run-admin
```

For normal use, Docker Compose is the one-command path.

## Direct mode (testing without a proxy)

Admin → **Settings** or **Proxies**:

- **Use proxy for BookMyShow requests** toggle.
- When off, the scraper bypasses the pool and calls BMS directly.
- A red banner is shown, and the change is audit-logged.
- In `BMS_ENV=prod`, direct mode is blocked unless `BMS_ALLOW_DIRECT=true`.

The setting is live (5-second cache); no restart needed.

## Proxy policy

- Default: every BMS request acquires a proxy from the pool.
- The hunter fetches Indian candidates from Geonode, Proxifly and ProxyScrape.
  Those list providers are contacted directly (bootstrapping); only BookMyShow
  traffic goes through the proxy pool.
- **Static proxies**: tested with 5 repeated BMS canaries; active at 4/5 and
  p95 ≤ 8s. One failure triggers a 10-minute cooldown.
- **Rotating proxies** (e.g. Webshare `-rotate`): active at 2/5 and p95 ≤ 15s,
  because the client retries and each retry can use a healthy exit IP. Only
  3 consecutive full request failures trigger a 60-second cooldown.
- Priority is lowest p95 latency first.
- If an admin adds an enabled premium proxy, free proxies are ignored and the
  hunter stops fetching free candidates. If all premium proxies are down,
  scraping stops and the dashboard shows the failure.
- Set `BMS_PROXY_FREE_ENABLED=false` to disable free candidates entirely.

Add a premium proxy in **Proxies**:

- URL: `http://user:pass@p.webshare.io:80/`
- Tier: Premium
- **Rotating**: check this for Webshare `-rotate` endpoints.
- Label: `webshare`

Then click **Recheck** if it was previously rejected. Watch `Last check`,
`Status`, `p95` and `Success`.

Free proxies are unreliable (roughly 1–7% success in testing). They are not a
production solution at a 10-second cadence.

## 10-second polling and bandwidth

- Showtime monitors default to a 10-second interval.
- One showtimes response can be 200–600 KB uncompressed. Polling one monitor
  every 10 seconds can consume roughly 0.5–5 GB/day depending on compression
  and theatre count.
- Use 10s for “hot” monitors (today/tomorrow, seat-alert subscribers, just after
  bookings open) and 30–60s for normal monitors.
- Seat-layout primary calls are small (~5 KB) and are capped per scan.
- The scheduler prioritises seat-alert subscribers and records `lag_ms` on every
  monitor. The admin dashboard shows due monitors, pool health and lag.

## Admin

- Single super-admin, created with `bmsbot create-admin`.
- Argon2id password hashes, signed HttpOnly session cookies, CSRF on every form,
  login rate limiting, security headers, audit log.
- Proxies page: add premium/free/rotating proxy, enable/disable, recheck, delete,
  inspect latency, success rate, failure streak and exit IP.
- Settings page: use-proxy toggle, seat threshold, max seat checks.
- Users, monitors, alerts and runtime settings are managed from the UI.
- Startup fails with a clear “Database is not migrated” message instead of a 500.

## Database

- SQLite in WAL mode with a 5-second busy timeout.
- Relative `BMS_DATABASE_URL` / `BMS_DATA_DIR` values resolve against the project
  root, not the current working directory. `bmsbot doctor` prints the resolved
  path.
- Alembic migrations in `migrations/`.
- Alert outbox (`alert_events`) with unique dedupe keys, so retries and restarts
  cannot send the same change twice.
- Monitor leases prevent two workers from scraping the same target.

## Tests and quality

```bash
pytest
ruff check src tests
```

The test suite covers the diff engine, seat thresholds, formatting/escaping,
parsers, proxy normalisation/encryption, rotating failure policy, runtime flags,
config path resolution and alert fan-out deduplication.

## Oracle deployment notes

- Use a multi-arch image; `curl_cffi` needs a wheel for the instance architecture.
- Keep the SQLite file on a Block Volume and back it up with `sqlite3 .backup`.
- Expose only the reverse proxy (443). The bot uses long polling, so no inbound
  port is required unless you switch to Telegram webhooks.
- Put the admin behind HTTPS + an IP allowlist or Tailscale.
- Set `BMS_ENV=prod` and leave `BMS_ALLOW_DIRECT=false`.
- Run `alembic upgrade head` as a one-shot job before starting workers.

## Known limitations

- The primary seat-layout endpoint
  (`/api/movies-data/seatlayout/v1/primary`) returns per-show category
  availability, not exact seat counts. Exact counts live behind the encrypted
  `doTrans.aspx` booking transaction, which is intentionally not replayed by
  this bot. The diff engine supports exact counts, so alert type 6 starts
  working as soon as a seat-map provider supplies `seat_available` values;
  until then, sold-out/status alerts (types 4/5) use the primary endpoint.
- The explore page can vary by region; the bot caches movie listings for 15
  minutes and supports `/region`, `/movies` and `/movie <name>`.
- Free proxies are slow and unstable. The premium proxy path is the production
  recommendation.
- Automated scraping may violate BookMyShow's terms. Keep request rates low and
  only read public showtime data.

## Environment

See `.env.example` for all settings. Secrets (`BMS_TELEGRAM_BOT_TOKEN`,
`BMS_ADMIN_SECRET_KEY`, `BMS_PROXY_ENCRYPTION_KEY`) must never be committed.

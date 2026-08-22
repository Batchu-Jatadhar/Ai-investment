# AI Investment — Personal Intraday Trading System (India)

> ## ⚠️ LIVE TRADING IS NOT IMPLEMENTED
>
> This repository currently contains **Phase 0 (foundation)** and **Phase 1
> (read-only Zerodha market data)**.
>
> There is **no strategy, no indicators, no regime engine, no risk engine, no
> position sizing, no order management, no TradingView webhook and no AI** in
> this build. The Zerodha integration is **read-only**: it authenticates for
> market data, downloads the instrument dump and streams ticks. There is no
> code path that can place, modify or cancel an order, and no HTTP endpoint
> that accepts a write of any kind — both are asserted by tests.
>
> The default trading mode is `paper`. `TRADING_MODE=live` is **rejected at
> configuration load**, by two independent barriers, and the application will
> refuse to start.
>
> **No broker credentials are required to run this project locally.**

---

## Purpose

A personal intraday trading **decision-support and execution** system for Indian
equities, intended to run against Zerodha Kite Connect after extensive
validation. It is not an automated trading bot and not a research paper project.

Priority ordering, applied whenever requirements conflict:

```
capital preservation > risk control > execution correctness
                     > actionability > performance > research fidelity
```

The system is expected to say `NO TRADE` most of the time.

## Architecture summary

Four laws govern the whole design:

1. **The AI is subtractive only.** It may veto or defer a deterministically
   generated setup. It cannot create one, move a price level, or place an order —
   its output schema has no price, quantity or order fields at all.
2. **Protection lives at the broker**, not in software. From the first fill a
   real stop rests at the exchange.
3. **The broker is the source of truth** for position and order state. A
   successful submission is never evidence of a fill.
4. **Decisions are made on closed bars.** Intrabar data drives protection only.

Two loops with very different dependencies: a **decision loop** (bar close, may
call the LLM, may open positions) and a **protection loop** (tick-driven, never
calls the LLM, owns every exit). Runtime infrastructure is PostgreSQL and an
object store — nothing else.

Full implementation reference: [`docs/architecture.md`](docs/architecture.md).

## Repository layout

```
backend/          FastAPI service
  app/
    adapters/     vendor code, isolated behind ports
      zerodha/      REST client, binary tick protocol, streaming provider
      replay/       deterministic offline provider (never presented as live)
    api/          HTTP surface: health + read-only market-data queries
    config/       typed settings, environment and trading-mode profiles
    core/         structured logging, error taxonomy, canonical time and clocks
    domain/
      market/       models, ports, session calendar, data quality, candle
                    engine, universe — implemented, with no vendor import
      (others)      documented extension points, not implemented
    infrastructure/  database engine, ORM models, repositories
    runtime/      process entry points (the market-data streamer)
    services/     instrument master, market-data service
  migrations/     Alembic; every schema change goes through here
  tests/          the backend test suite (tests/market/ covers Phase 1)
frontend/         Next.js + TypeScript + Tailwind status shell
infra/            Docker Compose and Dockerfiles
docs/             architecture reference
.github/workflows CI
```

Backend tests live in `backend/tests/` rather than a repository-root `tests/`
so that pytest's rootdir, import paths and CI working directory stay simple.

## Requirements

| Tool | Version used |
|---|---|
| Python | 3.11+ (developed on 3.13) |
| Node.js | 20+ (developed on 26) |
| Docker | optional — only for PostgreSQL and the containerised run |

## Local setup

```bash
git clone https://github.com/Batchu-Jatadhar/Ai-investment.git
cd Ai-investment
cp .env.example .env          # no credentials needed; defaults are safe
```

`.env` is git-ignored. `.env.example` contains **variable names only**.

### Environment configuration

Two independent axes, deliberately not conflated:

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `APP_ENV` | `development` `test` `production` | `development` | Where the process runs |
| `TRADING_MODE` | `backtest` `paper` `live` | **`paper`** | What it may do with money |

`TRADING_MODE=live` fails at startup with a clear error. It requires the broker
credentials, the full compliance profile, `LIVE_TRADING_ARMED=true` and a
non-zero capital ceiling — **and even with all of those present it is still
refused**, because live trading is not implemented in this build.

Broker, compliance, TradingView and LLM fields are declared in `.env.example` so
configuration is stable across phases. No code reads them yet.

## Run PostgreSQL

```bash
docker compose -f infra/docker-compose.yml up -d db
```

Without Docker, point `DATABASE_URL` at any database SQLAlchemy supports. SQLite
works for local development and is what the test suite uses by default:

```bash
export DATABASE_URL="sqlite:///./local.sqlite3"
```

## Run the backend

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# source .venv/bin/activate        # macOS / Linux
pip install -e ".[dev]"

alembic upgrade head               # every schema change goes through Alembic
uvicorn app.main:app --reload --port 8000
```

* API — <http://localhost:8000>
* OpenAPI docs — <http://localhost:8000/docs>

## Run the frontend

```bash
cd frontend
npm install
npm run dev
```

* UI — <http://localhost:3000> — shows backend and database health and a
  permanent banner stating that live trading is not implemented.

## Run everything in Docker

```bash
docker compose -f infra/docker-compose.yml up --build
```

The API container runs `alembic upgrade head` before starting.

## Zerodha setup (market data only)

Zerodha is reached exclusively through `app/adapters/zerodha/`. The trading
domain depends on the `MarketDataProvider` port and never on Zerodha classes, so
another provider can be substituted without touching domain code.

**No credentials are needed to run, test or develop this project.** Without them
the system reports `ZERODHA_NOT_CONFIGURED` and does not pretend to have a feed.

### What you need

1. A Kite Connect app at <https://developers.kite.trade> — this gives you an
   **API key** and an **API secret**.
2. A daily **access token**. Kite tokens expire at **06:00 IST the next
   morning** by regulation, so minting one is a daily ritual, not a one-off.

### Minting the daily access token

Open the login URL in a browser and log in:

```
https://kite.zerodha.com/connect/login?v=3&api_key=YOUR_API_KEY
```

Kite redirects to your registered URL carrying `?request_token=...`. Exchange
that token (the checksum is `SHA256(api_key + request_token + api_secret)`):

```bash
cd backend
python -c "import asyncio, os; from app.adapters.zerodha.client import ZerodhaRestClient; c = ZerodhaRestClient(api_key=os.environ['ZERODHA_API_KEY'], api_secret=os.environ['ZERODHA_API_SECRET']); s = asyncio.run(c.generate_session('PASTE_REQUEST_TOKEN')); print('ZERODHA_ACCESS_TOKEN=' + s.access_token)"
```

Put the printed value in `.env`, which is git-ignored. The token is never
logged, never persisted to the database and never sent to the browser.

### How authentication state is reported

The provider never continues as though data were flowing when it is not:

| State | Meaning |
|---|---|
| `ZERODHA_NOT_CONFIGURED` | No API key or access token; nothing is attempted |
| `ZERODHA_AUTH_FAILED` | 403 / `TokenException` — expired or revoked. **No retry loop**; only an interactive re-login fixes it |
| `ZERODHA_AUTHENTICATED` | Profile call succeeded; the socket is not open yet |
| `ZERODHA_CONNECTED` | Streaming |
| `ZERODHA_DISCONNECTED` | Dropped; reconnecting, and the resulting **data gap is recorded** |

## Configuring the development universe

The universe is explicit configuration in `.env`, written as
`EXCHANGE:TRADINGSYMBOL` and resolved through the instrument master:

```bash
MARKET_DATA_UNIVERSE=NSE:NIFTY 50,NSE:NIFTY BANK,NSE:RELIANCE,NSE:HDFCBANK,NSE:INFY
```

Instrument tokens are **never hard-coded**: exchanges reuse them after
derivative expiry, so the stable key is `(exchange, tradingsymbol)`.

**Indices are not directly tradable.** `NSE:NIFTY 50` and `NSE:NIFTY BANK`
resolve and stream normally and are flagged `is_index`; taking a position in an
index requires derivatives, which are out of scope for this phase. A malformed
universe entry or an unsupported interval is rejected when configuration loads,
not hours into a session.

## Running the market-data service

```bash
cd backend
alembic upgrade head
aitrade-marketdata                # or: python -m app.runtime.market_data
aitrade-marketdata --no-refresh   # use the stored instrument master as-is
```

On start it authenticates, refreshes the instrument master when it is stale
(older than `INSTRUMENT_MASTER_MAX_AGE_HOURS`), resolves and subscribes to the
universe, then streams. It exits `2` when Zerodha is not configured and `3` on a
provider error — it never runs silently pretending to have data.

## Verifying WebSocket connectivity

```bash
curl -s http://localhost:8000/health/market-data | python -m json.tool
```

```json
{
  "status": "ok",
  "running_in_this_process": true,
  "provider": "zerodha",
  "zerodha": {
    "configured": true, "authenticated": true, "connected": true,
    "state": "ZERODHA_CONNECTED"
  },
  "stream": {
    "subscribed_instruments": 5, "last_tick_age_ms": 120.0,
    "ticks_accepted": 8421, "ticks_rejected": 3, "gaps_recorded": 0
  },
  "instruments": { "count": 89234, "stale": false },
  "candles": { "intervals": ["1m", "5m", "15m"], "open_bars": 15 },
  "session": { "state": "open", "window": "NSE_EQUITY" }
}
```

The streamer normally runs as its own process, so this endpoint returns **503**
with `"running_in_this_process": false` when it is not running in the API
process. That is deliberate: it reports what it can actually observe instead of
inventing a stream status.

## Inspecting market data

All read-only, all `GET`:

```bash
# resolve a symbol to its instrument token
curl -s "http://localhost:8000/market-data/instruments/NSE/RELIANCE"
curl -s "http://localhost:8000/market-data/instruments/by-token/738561"

# most recent completed bars (1m by default; 5m and 15m also available)
curl -s "http://localhost:8000/market-data/candles/738561?interval=5m&limit=20"

# a specific window
curl -s "http://localhost:8000/market-data/candles/738561?interval=1m&start=2026-08-21T03:45:00Z&end=2026-08-21T04:45:00Z"

# latest completed bar, and the latest stored tick
curl -s "http://localhost:8000/market-data/candles/738561/latest?interval=15m"
curl -s "http://localhost:8000/market-data/ticks/738561/latest"
```

Only **completed** candles are persisted. An in-progress bar is a live view that
still changes; storing it would let a later reader mistake it for settled
history. The distinction is explicit in the model (`status`) and enforced by the
repository, which refuses to write an in-progress bar.

## What Phase 1 guarantees about the data

* **Timezone-safe.** Everything is stored and processed as timezone-aware UTC;
  Asia/Kolkata is used only for session logic and display. Naive datetimes are
  rejected, never guessed at.
* **Nothing is discarded silently.** Unknown instruments, invalid prices and
  quantities, duplicates, out-of-order ticks, stale ticks and malformed frames
  are each classified, counted and persisted to `data_quality_event`.
* **Reconnects always record a gap.** A `data_gap` row is written, and the
  candle engine and validator history are reset, because a bar spanning a gap
  would be a fiction.
* **Bars are aggregated, not re-derived.** 5m and 15m bars are built from
  completed 1m bars, so two derivations can never disagree at the edges.

## Order execution is NOT implemented

To be unambiguous: this build **cannot place, modify or cancel an order**.

* No `place_order`, `modify_order` or `cancel_order` exists anywhere in the
  application — a test scans the source tree to keep it that way.
* No HTTP endpoint accepts `POST`, `PUT`, `PATCH` or `DELETE`.
* The Zerodha adapter exposes only session, instrument and streaming calls.
* There is no TradingView webhook endpoint and no LLM client.
* Trading mode still defaults to `paper`, and `live` is refused at startup.

## Health checks

Both endpoints do real work; neither returns a hardcoded value.

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/db
curl http://localhost:8000/health/market-data
```

`GET /health` reports process liveness, environment, trading mode and uptime.
`GET /health/db` opens a connection, executes `SELECT 1`, reports the applied
Alembic revision, and returns **503** with a problem+json body when the database
is unreachable. A dead database does not take `/health` down with it.

## Run the tests

```bash
cd backend
pytest -q                                   # SQLite (default)

# against PostgreSQL, as CI does:
TEST_DATABASE_URL="postgresql+psycopg://aitrade:aitrade@localhost:5432/aitrade" pytest -q
```

The suite runs real Alembic migrations against a real database and covers
configuration loading, invalid configuration, secret handling, database
connectivity and failure, migrations, health endpoints, structured logging and
redaction, and trading-mode safety.

## Lint and format

```bash
cd backend  && ruff check . && ruff format --check .
cd frontend && npm run typecheck && npm run lint && npm run build
```

## Current implementation status

| Area | Status |
|---|---|
| Repository, tooling, CI | ✅ done |
| Typed configuration, environment/mode profiles | ✅ done |
| Trading-mode safety, LIVE blocked | ✅ done |
| Structured logging, correlation IDs, redaction | ✅ done |
| Error taxonomy, problem+json responses | ✅ done |
| PostgreSQL connection layer, Alembic | ✅ done (7 tables, 2 migrations) |
| Health endpoints | ✅ done |
| Docker Compose, Dockerfiles | ✅ authored — **not executed** (Docker is not installed on the development machine) |
| Frontend status shell | ✅ done |
| Test suite | ✅ done — 298 tests |
| Zerodha read-only auth + instrument master | ✅ done |
| WebSocket streaming, tick normalization, data quality | ✅ done |
| Reconnection, resubscription, data-gap recording | ✅ done |
| 1m/5m/15m candle engine, partial vs completed | ✅ done |
| Market session (NSE equity), timezone-safe time | ✅ done |
| Market-data persistence + repository | ✅ done |
| Read-only market-data API | ✅ done |
| Indicators / market regime | ❌ Phase 2 |
| Strategy / backtest | ❌ Phase 3 |
| Risk engine / sizing / portfolio | ❌ Phase 4 |
| OMS / order groups / synthetic OCO | ❌ Phase 5 |
| Paper adapter / supervisor / exits | ❌ Phase 6 |
| Trader UI / alerts / kill switch | ❌ Phase 7 |
| AI trade analyst | ❌ Phase 8 |
| Zerodha **order** connection | ❌ not before the live phase |
| TradingView webhook | ❌ later phase |
| **Live trading** | ❌ **not implemented** |

## Security

* Secrets live only in `.env` (git-ignored) and are typed as `SecretStr`; they
  are never logged, never returned by an endpoint, and never sent to the browser.
* A redaction filter scrubs credential-shaped content from every log record as a
  backstop.
* Only `NEXT_PUBLIC_*` variables reach the client bundle, and the only one
  defined is the API base URL.
* CI fails the build if a `.env` file is ever tracked or if `.env.example`
  acquires a populated credential value.

**Never commit** Zerodha API keys or secrets, access tokens, TradingView webhook
secrets, or LLM API keys.

## Compliance notice

Before live trading is ever enabled, the account's permission to place
API-originated orders, the client/algo classification, exchange registration and
algo ID, order-tagging format, static-IP registration, hosting constraints and
audit-retention obligations **must be confirmed in writing with Zerodha**. Low
order frequency does not make a system compliant. See `docs/architecture.md` §10.

## Licence

Private project. All rights reserved.

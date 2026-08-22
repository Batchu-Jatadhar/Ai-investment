# AI Investment — Personal Intraday Trading System (India)

> ## ⚠️ LIVE TRADING IS NOT IMPLEMENTED
>
> This repository currently contains **Phase 0: the project foundation only**.
>
> There is **no market data, no strategy, no indicators, no regime engine, no
> risk engine, no position sizing, no order management, no broker connection, no
> TradingView webhook and no AI** in this build. There is no code path that can
> place, modify or cancel an order, and no endpoint that accepts a write.
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
    api/          HTTP surface (health only)
    config/       typed settings, environment and trading-mode profiles
    core/         structured logging, error taxonomy
    domain/       one package per bounded context — documented, not implemented
    infrastructure/  database engine, session management, ORM models
    services/     application services
  migrations/     Alembic; every schema change goes through here
  tests/          the backend test suite
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

## Health checks

Both endpoints do real work; neither returns a hardcoded value.

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/db
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
| PostgreSQL connection layer, Alembic | ✅ done (one table: `system_event`) |
| Health endpoints | ✅ done |
| Docker Compose, Dockerfiles | ✅ authored — **not executed** (Docker is not installed on the development machine) |
| Frontend status shell | ✅ done |
| Test suite | ✅ done — 66 tests |
| Market data / bars / instruments | ❌ Phase 1 |
| Indicators / market regime | ❌ Phase 2 |
| Strategy / backtest | ❌ Phase 3 |
| Risk engine / sizing / portfolio | ❌ Phase 4 |
| OMS / order groups / synthetic OCO | ❌ Phase 5 |
| Paper adapter / supervisor / exits | ❌ Phase 6 |
| Trader UI / alerts / kill switch | ❌ Phase 7 |
| AI trade analyst | ❌ Phase 8 |
| Zerodha broker connection | ❌ not before Phase 10 |
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

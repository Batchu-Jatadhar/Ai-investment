# Architecture — implementation reference

Condensed from **Architecture Proposal v0.3** (approved, locked), including the
corrected synthetic-OCO subsystem. This file is the working reference for
implementers; it states what must be built and what must never be built. It
does not restate the full proposal's rationale.

---

## 1. What the system is

A **personal intraday trading decision-support and execution system for Indian
equities**, with Zerodha Kite Connect as the initial broker.

Priority ordering — the tie-breaker whenever requirements conflict:

```
capital preservation > risk control > execution correctness
                     > actionability > performance > research fidelity
```

`NO_TRADE` is the expected output most of the time and is never a failure.

**Not a goal:** predicting prices. The system optimises expected edge × risk
control × execution quality × position management.

---

## 2. The four laws

1. **The AI is subtractive only.** It may veto or defer a deterministically
   generated setup. It can never create one, loosen a limit, move a price level
   or place an order. Its output schema contains **no price, quantity or order
   fields** — inventing an entry is not something it is trusted not to do, it is
   something it cannot express.
2. **Protection lives at the broker.** From the first fill, a real stop order
   rests at the exchange. If the process dies, the stop survives.
3. **The broker is the source of truth** for position and order state. Internal
   state is intent plus cache. Divergence halts new entries. A successful
   submission is never evidence of a fill.
4. **Decisions are made on closed bars.** Intrabar data drives protection and
   monitoring, never entry logic.

---

## 3. The two loops

Keeping these separate is the core structural decision.

| | Decision loop | Protection loop |
|---|---|---|
| Cadence | Bar close (1/5/15 min) | Every tick + 1s timer |
| May call the LLM | Yes, hard timeout | **Never** |
| May place entry orders | Yes | No |
| May place exit orders | No | Yes |
| If the LLM is down | Degrades to `WAIT` | Unaffected |
| If the whole app is down | No new trades | Broker-resting stops still protect |

---

## 4. Pipeline

```
market data → bars (closed only) → indicators → regime → strategy
  → signal arbitration
  → risk pre-gate            [deterministic]
  → AI trade analyst         [advisory, subtractive, timeout → WAIT]
  → trade proposal
  → risk full gate           [deterministic, REJECT is absolute]
  → position sizing          [deterministic]
  → order validation + safety controls
  → OMS (intent-first, idempotent, order group)
  → broker adapter (zerodha | paper | backtest)
  → execution monitor + reconciler
  → portfolio
  → trade supervisor / exit engine   [no LLM, ever]
  → UI action state
```

Every path to the broker passes three deterministic gates after the AI, and
none originates from it.

---

## 5. Process topology

| Process | Owns | Restart rule |
|---|---|---|
| `engine` | Market data, bars, indicators, regime, strategy, signal, risk, sizing, OMS, supervisor. All hot state in memory | Reconcile + orphan sweep **before** enabling any decision loop. Never auto-resumes into LIVE |
| `web` | FastAPI, trader UI, webhook gateway. Reads the database, subscribes to the engine state stream | Stateless |
| `watchdog` | Independent heartbeat monitor, kill switch, flatten-all. Own broker session | Must survive the engine's death |

Runtime infrastructure: **PostgreSQL and an object store. Nothing else.**
Explicitly rejected: Redis, Kafka, Kubernetes, a dedicated vector database, an
agent framework, a third-party backtesting engine.

---

## 6. Synthetic OCO (corrected)

Zerodha discontinued Bracket Orders, so entry, stop and target cannot be one
broker primitive. Two quantities must be defined separately — conflating them
was the error in the earlier draft:

```
protected_qty = Σ remaining qty of PROTECT-role stop orders
                that are CONFIRMED LIVE at the broker

MEEE          = Σ remaining qty of ALL closing-direction orders in
                {ACCEPTED, OPEN, PARTIAL, SUBMITTED, UNKNOWN, TRIGGERED_UNFILLED}

MRE           = max(0, MEEE − open_qty)      "maximum reversal exposure"
```

An unconfirmed order **does not count toward protection** but **does count
toward exposure**. That asymmetry is deliberate.

When protection is exactly full, `MRE == Σ resting non-protective closing qty`.
Reversal exposure is therefore *chosen*, not eliminated by any invariant.

**Order roles** (immutable, assigned at intent creation): `ENTRY`, `PROTECT`,
`TARGET`, `DISCRETIONARY_EXIT`. **A `TARGET` is never protection.**

### Invariants

| ID | Invariant | On violation |
|---|---|---|
| O1 | `protected_qty >= open_qty` | `UNPROTECTED` → restore, else flatten within the bounded window |
| O2 | `MRE <= reversal_budget` (qty and value) | Cancel/resize **non-protective** orders only |
| O3 | No single closing order exceeds `open_qty` at submission; oversized orders resized within `resize_deadline` | Flatten |
| O4 | Every resting broker order belongs to a live group's desired state | Orphan sweep cancels |
| O5 | Internal state matches broker state | `DIVERGENT`, entries blocked |
| O6 | Broker net position sign never opposes intent | Unconditional flatten to zero, alert, block instrument |

### Two ordering rules that carry the safety property

* Protection is **never reduced to satisfy O2**. Over-exposure is resolved by
  removing non-protective quantity; if that is not enough, flatten.
* Protection is **never reduced on an unconfirmed reduction**. Only a confirmed
  fill may shrink a protective order.

### Selected design (v1): **B — protective stop resting, target fired by the supervisor**

No configuration achieves `MRE ≡ 0` at all times; every position reduction opens
a window in which the stop is oversized until the modify confirms. Design B does
not remove the window — it converts an **uncontrolled** window lasting the whole
trade into a **self-initiated** one lasting milliseconds, and deletes the
simultaneous-fill race from the primary path. Targets are fired as
self-terminating IOC-style orders, never rested.

Accepted cost: targets are missed when price grazes the level and retreats; the
engine must be alive for profit-taking, though never for protection.

### Reversal guard (role-aware)

Compares the overflow to a **budget**, not to zero. A naive net-flip check
rejects every correctly protected position and is wrong.

* Guard A — reject any closing order with `qty > open_qty`
* Guard B — reject if `MRE_after > reversal_budget`
* Guard C — reject an opposite-direction `ENTRY` while a position or closing
  order lives
* Guard D — reject cancel/downward-modify of `PROTECT` without a same-cycle
  replacement, a confirmed reduction, or a flat position

---

## 7. Trade state machine

```
NO_TRADE → SIGNAL_DETECTED → ANALYZING → [WAITING_CONFIRMATION] → APPROVED
        → ORDER_PENDING → [PARTIALLY_FILLED] → ACTIVE
        → TP1_REACHED → RUNNER_ACTIVE → EXIT_PENDING → CLOSED

failure/exit paths: REJECTED · ORDER_REJECTED · ORDER_FAILED
                    STOPPED_OUT · EXIT_TRIGGERED
extra transition:   TP1_REACHED → ACTIVE   (fired target expired unfilled)
any state:          RECONCILING · EMERGENCY_FLATTEN
```

Transitions are a closed, tested set; an undefined transition raises rather than
defaulting. There is no state in which the system is uncertain and still trading.

## 8. UI action states (first-class)

Derived deterministically from trade state, group state, position and health.
Never stored, never set by the AI.

```
NO_TRADE · WAIT · BUY · SELL · HOLD · PARTIAL_EXIT · EXIT
precedence: EXIT > PARTIAL_EXIT > HOLD > SELL/BUY > WAIT > NO_TRADE
```

`WAIT` must always name its condition. `EXIT` and `PARTIAL_EXIT` must always
state whether the system is acting automatically or waiting for the human.

---

## 9. Safety

* Modes: `BACKTEST` · `PAPER` · `LIVE`. **Every session starts in PAPER.**
* LIVE requires all of: explicit flag, typed confirmation, valid fresh token,
  clean reconciliation and orphan sweep, passing pre-flight, an explicitly set
  daily capital ceiling, and a **valid `compliance_profile` (§10)**.
* Two-tier kill switch: in-process, and an out-of-process watchdog that can
  flatten and can refuse to let the engine restart into LIVE.
* Bounded windows: `UNPROTECTED` and over-protected states both carry hard
  timers whose expiry action is to flatten.

## 10. Compliance — a gate, not a claim

Order frequency does **not** determine compliance. Before LIVE is ever enabled,
confirm **in writing with Zerodha**: whether the account may place
API-originated orders at all; client vs. algo classification and any exchange
registration with a unique algo ID; white-box vs. black-box treatment of an LLM
in the decision path; the exact order-tagging requirement (the idempotency
design depends on the tag field); static-IP registration; hosting constraints;
order-rate thresholds; self-trade prevention and audit retention.

`compliance_profile` is enforced configuration and LIVE arming fails closed
without it. Traceability is good engineering; it is not a compliance claim.

---

## 11. Phase plan

| Phase | Deliverable |
|---|---|
| 0 | Foundation — repository, config, logging, database, migrations, health, tests, CI, Docker. *(done)* |
| **1** | **Market data — Zerodha read-only auth, instrument master, WebSocket gateway, tick quality, bar engine, session calendar, explicit universe, persistence, repository. *(this build)*** |
| 2 | Indicators and deterministic regime engine |
| 3 | One strategy + backtest with pessimistic fills and the full cost model. **Gate: positive expectancy net of costs out-of-sample** |
| 4 | Risk engine, position sizing, portfolio |
| 5 | OMS, order groups, invariants O1–O6, orphan sweep, reversal guard |
| 6 | Paper adapter, state machine, supervisor, exit engine, reconciliation |
| 7 | Trader UI (seven action states), alerts, kill switch |
| 8 | AI trade analyst + AI-disabled control arm |
| 9 | **Paper soak.** No new features |
| 10 | LIVE, graduated from a tiny capital ceiling |
| 11 | Measure and extend |

---

## 11a. Phase 1 as built — market data

**Read-only.** No order operation exists in the codebase, and no HTTP endpoint
accepts a write. Both are asserted by tests that scan the source tree and the
published OpenAPI surface.

### Layering

```
app/domain/market/          provider-neutral: models, ports, session, quality,
                            candles, universe     (imports no adapter)
        ^
        |  MarketDataProvider · InstrumentSource · MarketDataRepository
        |
app/adapters/zerodha/       REST client, binary protocol, streaming provider
app/adapters/replay/        deterministic offline provider
app/infrastructure/         SqlMarketDataRepository
app/services/               InstrumentMaster, MarketDataService
app/runtime/market_data.py  composition root: aitrade-marketdata
```

### Decisions taken during implementation

| Decision | Reasoning |
|---|---|
| **The official `kiteconnect` package is not used** | Its ticker is built on Twisted, which does not belong inside an asyncio service, and the reconnect/gap semantics this architecture requires are not what it provides. The adapter uses `httpx` for the two REST calls and `websockets` plus an in-house decoder for the documented binary frames. The adapter boundary — the thing the architecture actually mandates — is unchanged. **Worth your explicit sign-off.** |
| Bars align to the UTC epoch | IST is UTC+05:30 and 30 is a multiple of 1, 5 and 15, so epoch-aligned buckets coincide exactly with IST clock boundaries and with the 09:15 session open. No special-casing needed. |
| Larger intervals aggregate completed 1m bars | Two independent derivations of the same bar disagree at the edges, and the disagreement only surfaces when a backtest cannot be reproduced. |
| Only completed candles are persisted | An in-progress bar still changes; storing it invites a later reader to treat it as settled history. |
| A data gap resets the candle engine and validator | Continuity was broken, so a bar spanning the gap is a fiction and the cumulative-volume baseline is meaningless. |
| Authentication failure does not retry | Kite tokens expire at 06:00 IST daily and only an interactive login renews them. A retry loop burns rate limit and hides the real problem. |
| Ticks are stored, with pruning; candles are kept | Retention is a policy, not an accident. |
| SQLite is a supported local backend | Keeps the suite runnable without Docker. Primary keys use `BigInteger().with_variant(Integer, "sqlite")` because SQLite only autoincrements a column declared exactly `INTEGER PRIMARY KEY`. |

### Protocol facts, taken from the official specification

`wss://ws.kite.trade?api_key&access_token`. Big-endian frames: `int16` packet
count, then `int16` length + payload per packet. Packet length selects the
layout — 8 (LTP), 28 (index quote), 32 (index full), 44 (quote), 184 (full:
quote + timestamps + OI + 5×5 depth). The segment is the low byte of the
instrument token; segment 9 is an index. Prices divide by 100, except CDS
(10,000,000) and BCD (10,000). REST uses `Authorization: token key:token` and
`X-Kite-Version: 3`; 403 with `TokenException` means the session is gone.

### Tables added (migration `0002_market_data`)

`instrument`, `market_tick`, `candle`, `connection_event`, `data_gap`,
`data_quality_event`. Prices are `Numeric(20,6)`; every timestamp is
timezone-aware UTC; `candle` is unique on `(instrument_token, interval,
start_at)` so re-ingestion is idempotent.

### Still deferred

Daily universe *eligibility* filters (ADV, spread, F&O ban list, results-day and
corporate-action exclusions, ASM/GSM) are specified in §4.4 but not implemented:
they need liquidity history and an exchange feed this phase does not have. The
universe today is the configured allow-list, resolved through the instrument
master. The Parquet tick store is likewise deferred — ticks currently go to
PostgreSQL with a pruning method available.

## 12. Rules that bind every phase

* Every schema change goes through Alembic. Nothing creates tables at runtime.
* Money is exact decimal in paise. Never float.
* No `datetime.now()` at a call site — time comes from an injected clock.
* Position size is always **filled** quantity, never intended quantity.
* Exits are always permitted, even when entries are blocked.
* A risk `REJECT` has no override path.
* `strategy` and `exits` must never import `broker`, `orders` or `ai`.
* Secrets never reach the browser, the database, or a log line.

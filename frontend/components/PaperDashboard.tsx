import type { ReactNode } from "react";
import type { DashboardState, PaperDashboard as Dashboard } from "@/lib/dashboard";
import { UNAVAILABLE, shown } from "@/lib/dashboard";

type Tone = "ok" | "warn" | "crit" | "muted" | "accent";

const TONE_TEXT: Record<Tone, string> = {
  ok: "text-[var(--color-ok)] border-[var(--color-ok)]",
  warn: "text-[var(--color-warn)] border-[var(--color-warn)]",
  crit: "text-[var(--color-crit)] border-[var(--color-crit)]",
  muted: "text-[var(--color-muted)] border-[var(--color-line)]",
  accent: "text-[var(--color-accent)] border-[var(--color-accent)]",
};

const ACTION_TONE: Record<Dashboard["strategy"]["action"], Tone> = {
  BUY: "ok",
  SELL: "crit",
  HOLD: "accent",
  EXIT: "warn",
  WAIT: "muted",
};

function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 font-mono text-xs uppercase tracking-wider ${TONE_TEXT[tone]}`}
    >
      {children}
    </span>
  );
}

function Figure({ label, value, note }: { label: string; value: string; note?: string }) {
  const missing = value === UNAVAILABLE;
  return (
    <div className="flex flex-col gap-1 rounded border border-[var(--color-line)] p-3">
      <span className="font-mono text-[11px] uppercase tracking-wider text-[var(--color-muted)]">
        {label}
      </span>
      <span
        className={`text-lg tabular-nums ${missing ? "text-[var(--color-muted)] italic" : ""}`}
      >
        {value}
      </span>
      {note ? <span className="text-xs text-[var(--color-muted)]">{note}</span> : null}
    </div>
  );
}

function Panel({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded border border-[var(--color-line)] bg-[var(--color-surface)] p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function PaperBanner({ mode }: { mode: string | null }) {
  return (
    <div
      role="status"
      className="rounded border-2 border-[var(--color-warn)] bg-[color-mix(in_srgb,var(--color-warn)_14%,transparent)] px-4 py-2 text-center font-mono text-sm uppercase tracking-[0.2em] text-[var(--color-warn)]"
    >
      Paper mode · simulated fills only · no live orders
      {mode !== null && mode !== "paper" ? ` · backend reports mode ${mode}` : ""}
    </div>
  );
}

function freshness(data: Dashboard["data"]): { tone: Tone; label: string } {
  if (data.status === "fresh") return { tone: "ok", label: `fresh · ${data.age_seconds}s old` };
  if (data.status === "stale") return { tone: "crit", label: `stale · ${data.age_seconds}s old` };
  return { tone: "crit", label: "no market data" };
}

function Ready({ data }: { data: Dashboard }) {
  const fresh = freshness(data.data);
  const riskTone: Tone =
    data.risk.status === "APPROVED" ? "ok" : data.risk.status === "REJECTED" ? "crit" : "muted";
  const aiTone: Tone =
    data.ai.status === "TAKE_TRADE"
      ? "ok"
      : data.ai.status === "REJECT" || data.ai.status === "INVALID_RESPONSE"
        ? "crit"
        : data.ai.status === "WAIT"
          ? "warn"
          : "muted";
  const entryNote =
    data.plan.entry_basis === "fill"
      ? "actual paper fill"
      : data.plan.entry_basis === "signal_bar_close"
        ? "reference: signal bar close; fills at next bar open"
        : undefined;

  return (
    <>
      {!data.is_paper ? (
        <div role="alert" className="rounded border-2 border-[var(--color-crit)] p-4 text-[var(--color-crit)]">
          Trading mode is {data.trading_mode.toUpperCase()}. This dashboard serves PAPER mode only.
        </div>
      ) : null}

      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="font-mono text-xs uppercase tracking-wider text-[var(--color-muted)]">Instrument</p>
          <h1 className="text-2xl font-semibold">{data.instrument?.symbol ?? "No instrument"}</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={data.session.state === "open" ? "ok" : "muted"}>
            session {data.session.state.replace("_", " ")}
          </Badge>
          <Badge tone={fresh.tone}>{fresh.label}</Badge>
        </div>
      </header>

      <section
        aria-label="Current action"
        className="flex flex-col gap-3 rounded border border-[var(--color-line)] bg-[var(--color-surface)] p-5 sm:flex-row sm:items-center sm:justify-between"
      >
        <div>
          <p className="font-mono text-xs uppercase tracking-wider text-[var(--color-muted)]">Strategy signal</p>
          <p data-testid="action" className={`text-5xl font-bold tracking-tight ${TONE_TEXT[ACTION_TONE[data.strategy.action]].split(" ")[0]}`}>
            {data.strategy.action}
          </p>
          <p className="mt-1 text-sm text-[var(--color-muted)]">{data.strategy.reason ?? "No strategy evaluation yet"}</p>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <Badge tone={data.trade_status === "BLOCKED" || data.trade_status === "UNAVAILABLE" ? "crit" : "accent"}>
            {data.trade_status.replace("_", " ")}
          </Badge>
          {data.data.status === "stale" ? (
            <span className="font-mono text-xs uppercase text-[var(--color-crit)]">stale — do not act</span>
          ) : null}
        </div>
      </section>

      {data.blocked_reasons.length > 0 ? (
        <section role="alert" aria-label="Why no trade" className="rounded border-2 border-[var(--color-crit)] p-4">
          <h2 className="mb-2 font-mono text-xs uppercase tracking-wider text-[var(--color-crit)]">Why no trade</h2>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {data.blocked_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="Risk" aside={<Badge tone={riskTone}>{data.risk.status.replace("_", " ")}</Badge>}>
          {data.risk.status === "REJECTED" ? (
            <ul className="space-y-1 text-sm">
              {data.risk.reasons.map((r) => (
                <li key={r.code}>
                  <span className="font-mono text-[var(--color-crit)]">{r.code}</span> — {r.detail}
                </li>
              ))}
            </ul>
          ) : data.risk.status === "APPROVED" ? (
            <p className="text-sm">
              Size {data.risk.quantity} · limited by {shown(data.risk.limited_by)}
            </p>
          ) : (
            <p className="text-sm text-[var(--color-muted)]">No proposal evaluated on the last bar.</p>
          )}
        </Panel>
        <Panel title="AI filter" aside={<Badge tone={aiTone}>{data.ai.status.replace("_", " ")}</Badge>}>
          <p className="text-sm">
            {data.ai.reason ??
              (data.ai.status === "NOT_CONFIGURED"
                ? "No analyst configured: strategy + risk only."
                : "Not consulted on the last bar.")}
          </p>
          {data.ai.model_id ? (
            <p className="mt-1 font-mono text-xs text-[var(--color-muted)]">
              {data.ai.model_id} · {data.ai.prompt_version}
            </p>
          ) : null}
        </Panel>
      </div>

      <Panel title={`Trade plan${data.plan.direction ? ` · ${data.plan.direction}` : ""}`}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <Figure label="Entry" value={shown(data.plan.entry)} note={entryNote} />
          <Figure label="Stop loss" value={shown(data.plan.stop_loss)} />
          <Figure label="Target" value={shown(data.plan.target)} />
          <Figure
            label="Reward : risk"
            value={data.plan.reward_to_risk === null ? UNAVAILABLE : `${data.plan.reward_to_risk} : 1`}
          />
          <Figure label="Position size" value={shown(data.plan.quantity)} />
        </div>
      </Panel>

      <Panel title="Paper position" aside={<Badge tone="muted">{shown(data.position.side)}</Badge>}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Figure label="Quantity" value={shown(data.position.quantity)} />
          <Figure label="Entry price" value={shown(data.position.entry_price)} />
          <Figure label="Mark" value={shown(data.position.mark_price)} note="last completed bar close" />
          <Figure label="Unrealized P&L" value={shown(data.position.unrealized_pnl)} note="gross, before exit costs" />
          <Figure label="Realized P&L" value={shown(data.position.realized_pnl)} note="net of costs" />
          <Figure label="Closed trades" value={shown(data.position.closed_trades)} />
        </div>
      </Panel>

      <footer className="font-mono text-xs text-[var(--color-muted)]">
        last bar {shown(data.data.last_bar_start)} → {shown(data.data.last_bar_end)} · generated{" "}
        {data.generated_at} · IST {data.session.local_time}
      </footer>
    </>
  );
}

export function PaperDashboard({ state }: { state: DashboardState }) {
  return (
    <main className="mx-auto flex max-w-5xl flex-col gap-4 px-4 py-6 sm:px-6">
      <PaperBanner mode={state.kind === "ready" ? state.data.trading_mode : null} />
      {state.kind === "loading" ? (
        <p className="text-sm text-[var(--color-muted)]">Loading dashboard…</p>
      ) : state.kind === "error" ? (
        <section role="alert" className="rounded border-2 border-[var(--color-crit)] p-4">
          <h2 className="font-mono text-xs uppercase tracking-wider text-[var(--color-crit)]">
            Dashboard unavailable
          </h2>
          <p className="mt-1 text-sm">{state.message}</p>
          <p className="mt-1 text-xs text-[var(--color-muted)]">
            No figures are shown because none could be confirmed.
          </p>
        </section>
      ) : (
        <Ready data={state.data} />
      )}
    </main>
  );
}

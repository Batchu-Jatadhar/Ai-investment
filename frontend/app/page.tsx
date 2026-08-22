"use client";

import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { DatabaseHealth, Health } from "@/lib/api";
import { API_BASE_URL, fetchDatabaseHealth, fetchHealth } from "@/lib/api";

type Status = "loading" | "ok" | "error";

function Pill({ tone, children }: { tone: Status; children: ReactNode }) {
  const colour =
    tone === "ok"
      ? "text-[var(--color-ok)] border-[var(--color-ok)]"
      : tone === "error"
        ? "text-[var(--color-crit)] border-[var(--color-crit)]"
        : "text-[var(--color-muted)] border-[var(--color-line)]";
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 font-mono text-xs uppercase tracking-wider ${colour}`}
    >
      {children}
    </span>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-[var(--color-line)] py-2 last:border-b-0">
      <span className="font-mono text-xs uppercase tracking-wider text-[var(--color-muted)]">
        {label}
      </span>
      <span className="text-sm tabular-nums">{value}</span>
    </div>
  );
}

export default function Page() {
  const [health, setHealth] = useState<Health | null>(null);
  const [db, setDb] = useState<DatabaseHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, d] = await Promise.all([fetchHealth(), fetchDatabaseHealth()]);
      setHealth(h);
      setDb(d);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "backend unreachable");
      setHealth(null);
      setDb(null);
    } finally {
      setCheckedAt(new Date().toLocaleTimeString());
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 10_000);
    return () => clearInterval(timer);
  }, [refresh]);

  const apiTone: Status = error ? "error" : health ? "ok" : "loading";
  const dbTone: Status = db
    ? db.status === "ok"
      ? "ok"
      : "error"
    : error
      ? "error"
      : "loading";

  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-8 px-6 py-16">
      <header className="flex flex-col gap-3">
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-accent)]">
          Phase 0 — foundation
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">
          AI Investment — Intraday Trading System
        </h1>
        <p className="text-sm leading-relaxed text-[var(--color-muted)]">
          Development shell. There is no market data, no strategy, no risk engine and
          no order path in this build.
        </p>
      </header>

      <section className="rounded border border-[var(--color-warn)] bg-[color-mix(in_srgb,var(--color-warn)_12%,transparent)] p-4">
        <p className="font-mono text-xs uppercase tracking-wider text-[var(--color-warn)]">
          Live trading not implemented
        </p>
        <p className="mt-1 text-sm text-[var(--color-muted)]">
          Trading mode is{" "}
          <span className="font-mono text-[var(--color-warn)]">
            {health?.trading_mode ?? "unknown"}
          </span>
          . No order placement, broker write path or execution engine exists.
        </p>
      </section>

      <section className="rounded border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Backend</h2>
          <Pill tone={apiTone}>{apiTone === "ok" ? "reachable" : apiTone}</Pill>
        </div>
        {error ? (
          <p className="font-mono text-xs text-[var(--color-crit)]">
            {error} — is the API running at {API_BASE_URL}?
          </p>
        ) : (
          <div className="flex flex-col">
            <Row label="Service" value={health?.app ?? "—"} />
            <Row label="Version" value={health?.version ?? "—"} />
            <Row label="Environment" value={health?.environment ?? "—"} />
            <Row label="Trading mode" value={health?.trading_mode ?? "—"} />
            <Row
              label="Uptime"
              value={health ? `${health.uptime_seconds.toFixed(1)} s` : "—"}
            />
          </div>
        )}
      </section>

      <section className="rounded border border-[var(--color-line)] bg-[var(--color-surface)] p-5">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Database</h2>
          <Pill tone={dbTone}>{dbTone === "ok" ? "connected" : dbTone}</Pill>
        </div>
        <div className="flex flex-col">
          <Row label="Backend" value={db?.backend ?? "—"} />
          <Row label="Migration" value={db?.migration_revision ?? "not migrated"} />
          <Row
            label="Latency"
            value={db?.latency_ms != null ? `${db.latency_ms} ms` : "—"}
          />
          {db?.error ? (
            <Row
              label="Error"
              value={<span className="text-[var(--color-crit)]">{db.error}</span>}
            />
          ) : null}
        </div>
      </section>

      <footer className="font-mono text-xs text-[var(--color-muted)]">
        checked {checkedAt ?? "—"} · polling every 10s · api {API_BASE_URL}
      </footer>
    </main>
  );
}

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PaperDashboard } from "@/components/PaperDashboard";
import type { DashboardState, PaperDashboard as Dashboard } from "@/lib/dashboard";

function dashboard(overrides: Partial<Dashboard> = {}): Dashboard {
  return {
    trading_mode: "paper",
    is_paper: true,
    generated_at: "2026-08-21T04:10:05+00:00",
    session_running: true,
    instrument: { symbol: "NSE:RELIANCE", instrument_token: 738561 },
    session: { state: "open", is_trading_day: true, local_time: "2026-08-21T09:40:05+05:30" },
    data: {
      status: "fresh",
      last_bar_start: "2026-08-21T04:05:00+00:00",
      last_bar_end: "2026-08-21T04:10:00+00:00",
      last_close: "1008.00",
      age_seconds: 5,
      stale_after_seconds: 360,
    },
    strategy: { action: "BUY", reason_code: "LONG_ORB_BREAKOUT", reason: "Price closed above the opening range" },
    risk: { status: "APPROVED", quantity: 357, limited_by: "risk_budget", reasons: [] },
    ai: { status: "NOT_CONFIGURED", reason: null, model_id: null, prompt_version: null },
    plan: {
      direction: "LONG",
      entry: "1008.00",
      entry_basis: "signal_bar_close",
      stop_loss: "994.00",
      target: "1036.00",
      reward_to_risk: "2.0",
      quantity: 357,
    },
    position: {
      side: "FLAT",
      quantity: null,
      entry_price: null,
      mark_price: "1008.00",
      unrealized_pnl: null,
      realized_pnl: "0.00",
      closed_trades: 0,
    },
    trade_status: "ORDER_WORKING",
    blocked_reasons: [],
    ...overrides,
  };
}

const render = (state: DashboardState) => renderToStaticMarkup(<PaperDashboard state={state} />);
const ready = (overrides: Partial<Dashboard> = {}) => render({ kind: "ready", data: dashboard(overrides) });
const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

describe("PaperDashboard", () => {
  it("always shows PAPER mode, whatever the state", () => {
    for (const html of [render({ kind: "loading" }), render({ kind: "error", message: "x" }), ready()]) {
      expect(text(html)).toContain("Paper mode");
      expect(text(html)).toContain("no live orders");
    }
  });

  it("puts the current action, plan and backend figures on screen as received", () => {
    const t = text(ready());
    expect(t).toContain("NSE:RELIANCE");
    expect(ready()).toMatch(/data-testid="action"[^>]*>BUY</);
    for (const value of ["1008.00", "994.00", "1036.00", "2.0 : 1", "357", "ORDER WORKING", "APPROVED"]) {
      expect(t).toContain(value);
    }
    expect(t).toContain("reference: signal bar close");
    expect(t).not.toContain("Why no trade");
  });

  it("offers no order controls of any kind", () => {
    const html = ready();
    expect(html).not.toMatch(/<button|<form|<input/);
    expect(text(html)).not.toMatch(/place order|submit order|buy now|sell now/i);
  });

  it("states a risk rejection in words, with every reason, even if the AI approved", () => {
    const t = text(
      ready({
        risk: {
          status: "REJECTED",
          quantity: null,
          limited_by: null,
          reasons: [{ code: "insufficient_capital", detail: "cash 500 at 1008.00 buys 0 unit(s)" }],
        },
        ai: { status: "TAKE_TRADE", reason: "looks good", model_id: "fake-local", prompt_version: "fake-v1" },
        trade_status: "BLOCKED",
        blocked_reasons: ["Risk rejected (insufficient_capital): cash 500 at 1008.00 buys 0 unit(s)"],
        plan: { ...dashboard().plan, quantity: null },
      }),
    );
    expect(t).toContain("REJECTED");
    expect(t).toContain("insufficient_capital");
    expect(t).toContain("Why no trade");
    expect(t).toContain("Risk rejected (insufficient_capital)");
    expect(t).toContain("BLOCKED");
  });

  it("marks stale data explicitly", () => {
    const t = text(
      ready({
        data: { ...dashboard().data, status: "stale", age_seconds: 1800 },
        blocked_reasons: ["Market data is stale: the last bar closed 1800s ago; do not act on these values"],
      }),
    );
    expect(t).toContain("stale · 1800s old");
    expect(t).toContain("stale — do not act");
    expect(t).toContain("Market data is stale");
  });

  it("shows Unavailable rather than inventing values when there is no session", () => {
    const t = text(
      ready({
        session_running: false,
        instrument: null,
        data: { ...dashboard().data, status: "unavailable", last_bar_start: null, last_bar_end: null, last_close: null, age_seconds: null },
        strategy: { action: "WAIT", reason_code: null, reason: null },
        risk: { status: "NOT_EVALUATED", quantity: null, limited_by: null, reasons: [] },
        plan: { direction: null, entry: null, entry_basis: null, stop_loss: null, target: null, reward_to_risk: null, quantity: null },
        position: { side: null, quantity: null, entry_price: null, mark_price: null, unrealized_pnl: null, realized_pnl: null, closed_trades: null },
        trade_status: "UNAVAILABLE",
        blocked_reasons: ["No paper session is running in this process"],
      }),
    );
    expect(t).toContain("no market data");
    expect(t).toContain("No paper session is running");
    expect(t.match(/Unavailable/g)?.length).toBeGreaterThanOrEqual(11);
    expect(t).not.toMatch(/1008\.00|994\.00|357/);
  });

  it("renders an error without any stale figures", () => {
    const t = text(render({ kind: "error", message: "dashboard request failed with HTTP 500" }));
    expect(t).toContain("Dashboard unavailable");
    expect(t).toContain("HTTP 500");
    expect(t).not.toMatch(/Entry|Stop loss|BUY/);
  });

  it("warns when the backend is not in paper mode", () => {
    const t = text(ready({ trading_mode: "backtest", is_paper: false }));
    expect(t).toContain("This dashboard serves PAPER mode only");
    expect(t).toContain("backend reports mode backtest");
  });

  it("shows the AI verdict and its provenance", () => {
    const t = text(
      ready({
        ai: { status: "REJECT", reason: "fake rule chose REJECT", model_id: "fake-local", prompt_version: "fake-v1" },
        trade_status: "BLOCKED",
        blocked_reasons: ["AI filter did not approve: fake rule chose REJECT"],
      }),
    );
    expect(t).toContain("REJECT");
    expect(t).toContain("fake-local · fake-v1");
    expect(t).toContain("AI filter did not approve");
  });
});

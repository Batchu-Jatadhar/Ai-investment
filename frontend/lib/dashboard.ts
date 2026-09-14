import { API_BASE_URL } from "@/lib/api";

// Mirrors backend/app/services/paper_dashboard.py. Every figure is computed by
// the backend; this file only fetches and labels. Money and prices arrive as
// exact decimal strings and are displayed as received, never re-computed.

export type StrategyAction = "BUY" | "SELL" | "WAIT" | "HOLD" | "EXIT";
export type TradeStatus =
  | "UNAVAILABLE"
  | "NO_SIGNAL"
  | "BLOCKED"
  | "ORDER_WORKING"
  | "IN_POSITION"
  | "EXITED";

export interface PaperDashboard {
  trading_mode: string;
  is_paper: boolean;
  generated_at: string;
  session_running: boolean;
  instrument: { symbol: string; instrument_token: number } | null;
  session: { state: string; is_trading_day: boolean; local_time: string };
  data: {
    status: "fresh" | "stale" | "unavailable";
    last_bar_start: string | null;
    last_bar_end: string | null;
    last_close: string | null;
    age_seconds: number | null;
    stale_after_seconds: number;
  };
  strategy: { action: StrategyAction; reason_code: string | null; reason: string | null };
  risk: {
    status: "APPROVED" | "REJECTED" | "NOT_EVALUATED";
    quantity: number | null;
    limited_by: string | null;
    reasons: { code: string; detail: string }[];
  };
  ai: {
    status:
      | "TAKE_TRADE"
      | "WAIT"
      | "REJECT"
      | "INVALID_RESPONSE"
      | "NOT_CONSULTED"
      | "NOT_CONFIGURED";
    reason: string | null;
    model_id: string | null;
    prompt_version: string | null;
  };
  plan: {
    direction: "LONG" | "SHORT" | null;
    entry: string | null;
    entry_basis: "fill" | "signal_bar_close" | null;
    stop_loss: string | null;
    target: string | null;
    reward_to_risk: string | null;
    quantity: number | null;
  };
  position: {
    side: "FLAT" | "LONG" | "SHORT" | null;
    quantity: number | null;
    entry_price: string | null;
    mark_price: string | null;
    unrealized_pnl: string | null;
    realized_pnl: string | null;
    closed_trades: number | null;
  };
  trade_status: TradeStatus;
  blocked_reasons: string[];
}

export type DashboardState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; data: PaperDashboard };

export const UNAVAILABLE = "Unavailable";

/** A backend value, or an explicit "Unavailable" - never a placeholder number. */
export function shown(value: string | number | null | undefined): string {
  return value === null || value === undefined ? UNAVAILABLE : String(value);
}

export async function fetchPaperDashboard(): Promise<PaperDashboard> {
  const response = await fetch(`${API_BASE_URL}/dashboard/paper`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`dashboard request failed with HTTP ${response.status}`);
  }
  return (await response.json()) as PaperDashboard;
}

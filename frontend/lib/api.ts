export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export interface Health {
  status: string;
  app: string;
  version: string;
  environment: string;
  trading_mode: string;
  live_trading_implemented: boolean;
  uptime_seconds: number;
}

export interface DatabaseHealth {
  status: string;
  backend: string;
  latency_ms: number | null;
  migration_revision: string | null;
  migrated: boolean;
  error: string | null;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store" });
  // /health/db answers 503 with a body when the database is down, and that body
  // is the useful part — a non-2xx status is not a transport failure here.
  return (await response.json()) as T;
}

export const fetchHealth = () => getJson<Health>("/health");
export const fetchDatabaseHealth = () => getJson<DatabaseHealth>("/health/db");

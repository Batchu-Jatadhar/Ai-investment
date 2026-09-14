"use client";

import { useEffect, useState } from "react";
import { PaperDashboard } from "@/components/PaperDashboard";
import type { DashboardState } from "@/lib/dashboard";
import { fetchPaperDashboard } from "@/lib/dashboard";

const POLL_MS = 5_000;

export default function Page() {
  const [state, setState] = useState<DashboardState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const data = await fetchPaperDashboard();
        if (!cancelled) setState({ kind: "ready", data });
      } catch (err) {
        // Replace, never keep, the last snapshot: old figures must not look current.
        if (!cancelled) {
          setState({
            kind: "error",
            message: err instanceof Error ? err.message : "backend unreachable",
          });
        }
      }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return <PaperDashboard state={state} />;
}

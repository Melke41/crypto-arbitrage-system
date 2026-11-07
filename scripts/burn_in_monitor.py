#!/usr/bin/env python3
"""Live Burn-In Diagnostic & Opportunity Funnel Inspector CLI.

Polls the active ArbitrageSystemOrchestrator control plane (FastAPI) and prints
real-time terminal health, dual-queue telemetry status, opportunity funnel conversions,
risk manager rejection breakdowns, and execution financial performance.

Usage:
    python scripts/burn_in_monitor.py [--url http://127.0.0.1:8000] [--watch] [--interval 3]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path when script is executed directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ==============================================================================
# Metric Parser & Aggregator Functions (Pure & Unit-Testable)
# ==============================================================================


def parse_health_metrics(health: dict[str, Any]) -> dict[str, Any]:
    """Extract operational health and dual-queue persistence metrics."""
    q_depth = health.get("persistence_queue_depth", {})
    return {
        "status": health.get("status", "UNKNOWN"),
        "execution_mode": health.get("execution_mode", "UNKNOWN"),
        "kill_switch_active": bool(health.get("kill_switch_active", False)),
        "kill_switch_reason": health.get("kill_switch_reason"),
        "active_connections": int(health.get("active_connections", 0)),
        "critical_queue_depth": int(q_depth.get("critical_queue", 0)),
        "telemetry_queue_depth": int(q_depth.get("telemetry_queue", 0)),
        "dropped_telemetry_count": int(q_depth.get("dropped_telemetry_count", 0)),
    }


def parse_opportunity_metrics(opportunities: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate opportunity funnel counts and strategy distributions."""
    total_detected = len(opportunities)
    by_strategy: dict[str, dict[str, int]] = {
        "CROSS_EXCHANGE": {"total": 0, "executable": 0, "theoretical": 0},
        "TRIANGULAR": {"total": 0, "executable": 0, "theoretical": 0},
        "FUNDING_BASIS": {"total": 0, "executable": 0, "theoretical": 0},
        "STAT_ARB": {"total": 0, "executable": 0, "theoretical": 0},
    }
    rejection_reasons: dict[str, int] = {}
    total_executable = 0
    total_theoretical = 0

    for opp in opportunities:
        strat = opp.get("strategy_type", "OTHER")
        if strat not in by_strategy:
            by_strategy[strat] = {"total": 0, "executable": 0, "theoretical": 0}

        by_strategy[strat]["total"] += 1
        state = opp.get("lifecycle_state", "")

        if state == "EXECUTABLE_OPPORTUNITY":
            by_strategy[strat]["executable"] += 1
            total_executable += 1
        elif state == "THEORETICAL_OPPORTUNITY":
            by_strategy[strat]["theoretical"] += 1
            total_theoretical += 1

        # Track rejection reasons if present in metadata
        meta = opp.get("metadata", {})
        if "rejection_reason" in meta:
            reason = meta["rejection_reason"]
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        elif opp.get("net_edge_bps", 0) and Decimal(str(opp.get("net_edge_bps", 0))) <= Decimal("0"):
            reason = "Negative Net Edge After Fees & VWAP"
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    conversion_pct = (
        round((total_executable / total_detected) * 100, 2) if total_detected > 0 else 0.0
    )

    return {
        "total_detected": total_detected,
        "total_executable": total_executable,
        "total_theoretical": total_theoretical,
        "conversion_pct": conversion_pct,
        "by_strategy": by_strategy,
        "rejection_reasons": rejection_reasons,
    }


def parse_execution_metrics(executions: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute realized execution statistics, status counts, and PnL attribution."""
    total_plans = len(executions)
    completed = 0
    unwound = 0
    failed = 0
    executing = 0
    total_realized_pnl = Decimal("0")
    total_fees = Decimal("0")

    for plan in executions:
        status = plan.get("status", "")
        if status == "COMPLETED":
            completed += 1
        elif status == "UNWOUND":
            unwound += 1
        elif status in ("FAILED", "PARTIALLY_FAILED"):
            failed += 1
        elif status == "EXECUTING":
            executing += 1

        pnl = Decimal(str(plan.get("realized_pnl", "0")))
        fees = Decimal(str(plan.get("estimated_fees", "0")))
        total_realized_pnl += pnl
        total_fees += fees

    unwind_rate = round((unwound / total_plans) * 100, 2) if total_plans > 0 else 0.0

    return {
        "total_plans": total_plans,
        "completed": completed,
        "unwound": unwound,
        "failed": failed,
        "executing": executing,
        "unwind_rate_pct": unwind_rate,
        "total_realized_pnl_usdt": str(total_realized_pnl),
        "total_fees_usdt": str(total_fees),
    }


# ==============================================================================
# Terminal Report Formatter
# ==============================================================================


def format_burn_in_report(
    health: dict[str, Any],
    portfolio: dict[str, Any],
    opp_metrics: dict[str, Any],
    exec_metrics: dict[str, Any],
) -> str:
    """Format operational metrics into an ASCII terminal report."""
    status_str = f"[{health['status']}]"
    if health["kill_switch_active"]:
        status_str += f" - KILL SWITCH ACTIVE: {health['kill_switch_reason']}"

    lines = [
        "=" * 78,
        "      CRYPTO ARBITRAGE SYSTEM -- LIVE BURN-IN & TELEMETRY MONITOR       ",
        "=" * 78,
        f"  Engine Status      : {status_str}",
        f"  Execution Mode     : {health['execution_mode']}",
        f"  Active WS Streams  : {health['active_connections']} exchanges synchronized",
        f"  Critical Queue     : {health['critical_queue_depth']} pending writes",
        f"  Telemetry Queue    : {health['telemetry_queue_depth']} pending writes",
        f"  Dropped Telemetry  : {health['dropped_telemetry_count']} (Target: 0)",
        "-" * 78,
        "  PORTFOLIO & RISK OVERVIEW",
        "-" * 78,
        f"  Daily Realized PnL : ${portfolio.get('daily_realized_pnl_usdt', '0.00')} USDT",
        f"  High-Water Mark    : ${portfolio.get('high_water_mark_usdt', '0.00')} USDT",
        f"  Open Hedged Pos    : {len(portfolio.get('open_positions', []))} active positions",
        "-" * 78,
        "  OPPORTUNITY FUNNEL CONVERSION PER STRATEGY",
        "-" * 78,
        f"  {'Strategy':<18} | {'Total':<8} | {'Theoretical':<13} | {'Executable':<11} | {'Conv %':<8}",
        f"  {'-'*18} | {'-'*8} | {'-'*13} | {'-'*11} | {'-'*8}",
    ]

    for strat, counts in opp_metrics["by_strategy"].items():
        tot = counts["total"]
        theo = counts["theoretical"]
        exe = counts["executable"]
        pct = f"{(exe / tot * 100):.1f}%" if tot > 0 else "0.0%"
        lines.append(f"  {strat:<18} | {tot:<8} | {theo:<13} | {exe:<11} | {pct:<8}")

    lines.append(f"  {'-'*18} | {'-'*8} | {'-'*13} | {'-'*11} | {'-'*8}")
    lines.append(
        f"  {'TOTAL':<18} | {opp_metrics['total_detected']:<8} | "
        f"{opp_metrics['total_theoretical']:<13} | {opp_metrics['total_executable']:<11} | "
        f"{opp_metrics['conversion_pct']:.1f}%"
    )

    if opp_metrics["rejection_reasons"]:
        lines.append("-" * 78)
        lines.append("  TOP RISK & EVALUATOR REJECTION REASONS")
        lines.append("-" * 78)
        for reason, count in sorted(
            opp_metrics["rejection_reasons"].items(), key=lambda x: x[1], reverse=True
        )[:5]:
            lines.append(f"  - {reason:<52} : {count} rejected")

    lines.extend(
        [
            "-" * 78,
            "  EXECUTION STATE & SLIPPAGE ATTRIBUTION",
            "-" * 78,
            f"  Dispatched Plans   : {exec_metrics['total_plans']}",
            f"  Completed Plans    : {exec_metrics['completed']}",
            f"  Unwound Plans      : {exec_metrics['unwound']} (Unwind Rate: {exec_metrics['unwind_rate_pct']}%)",
            f"  Failed Plans       : {exec_metrics['failed']}",
            f"  Executing Plans    : {exec_metrics['executing']}",
            f"  Total Net Realized : ${exec_metrics['total_realized_pnl_usdt']} USDT",
            f"  Taker Fees Paid    : ${exec_metrics['total_fees_usdt']} USDT",
            "=" * 78,
        ]
    )

    return "\n".join(lines)


# ==============================================================================
# HTTP Client & Polling Loop
# ==============================================================================


def fetch_api_endpoint(url: str, timeout_sec: float = 3.0) -> Any:
    """Fetch and decode JSON from an HTTP endpoint."""
    req = urllib.request.Request(url, headers={"User-Agent": "BurnInMonitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_burn_in_poll(base_url: str) -> str:
    """Execute single poll cycle against orchestrator API and return report string."""
    try:
        health_raw = fetch_api_endpoint(f"{base_url}/health")
        portfolio_raw = fetch_api_endpoint(f"{base_url}/portfolio")
        opps_raw = fetch_api_endpoint(f"{base_url}/opportunities?limit=200")
        execs_raw = fetch_api_endpoint(f"{base_url}/executions?limit=200")
    except urllib.error.URLError as e:
        return (
            f"[!] Could not connect to Arbitrage Orchestrator at {base_url}: {e.reason}\n"
            f"    Ensure the engine is running: '.venv/bin/python -m src.main'"
        )
    except Exception as e:
        return f"[!] Error polling API at {base_url}: {e}"

    health = parse_health_metrics(health_raw)
    portfolio = portfolio_raw
    opp_metrics = parse_opportunity_metrics(opps_raw)
    exec_metrics = parse_execution_metrics(execs_raw)

    return format_burn_in_report(health, portfolio, opp_metrics, exec_metrics)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real-time CLI Burn-In Diagnostic & Opportunity Funnel Inspector."
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="FastAPI orchestrator root URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously poll and refresh the terminal monitor",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Polling interval in seconds for --watch mode (default: 3.0)",
    )
    args = parser.parse_args()

    if not args.watch:
        print(run_burn_in_poll(args.url))
        return

    try:
        while True:
            # Clear screen ANSI escape
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(run_burn_in_poll(args.url) + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[*] Burn-in monitor stopped.")


if __name__ == "__main__":
    main()

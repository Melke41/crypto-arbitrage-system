"""Unit tests for the CLI Live Burn-In Diagnostic & Funnel Inspector."""

from scripts.burn_in_monitor import (
    format_burn_in_report,
    parse_execution_metrics,
    parse_health_metrics,
    parse_opportunity_metrics,
    run_burn_in_poll,
)


def test_parse_health_metrics() -> None:
    raw_health = {
        "status": "HEALTHY",
        "execution_mode": "PAPER",
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "active_connections": 2,
        "persistence_queue_depth": {
            "critical_queue": 5,
            "telemetry_queue": 12,
            "dropped_telemetry_count": 0,
        },
    }
    metrics = parse_health_metrics(raw_health)
    assert metrics["status"] == "HEALTHY"
    assert metrics["execution_mode"] == "PAPER"
    assert metrics["kill_switch_active"] is False
    assert metrics["active_connections"] == 2
    assert metrics["critical_queue_depth"] == 5
    assert metrics["telemetry_queue_depth"] == 12
    assert metrics["dropped_telemetry_count"] == 0


def test_parse_opportunity_metrics() -> None:
    raw_opps = [
        {
            "id": "opp1",
            "strategy_type": "CROSS_EXCHANGE",
            "lifecycle_state": "EXECUTABLE_OPPORTUNITY",
            "net_edge_bps": "25.0",
        },
        {
            "id": "opp2",
            "strategy_type": "CROSS_EXCHANGE",
            "lifecycle_state": "THEORETICAL_OPPORTUNITY",
            "net_edge_bps": "-5.0",
        },
        {
            "id": "opp3",
            "strategy_type": "TRIANGULAR",
            "lifecycle_state": "THEORETICAL_OPPORTUNITY",
            "metadata": {"rejection_reason": "Order book age 300ms exceeds threshold"},
        },
        {
            "id": "opp4",
            "strategy_type": "STAT_ARB",
            "lifecycle_state": "EXECUTABLE_OPPORTUNITY",
            "net_edge_bps": "40.0",
        },
    ]

    metrics = parse_opportunity_metrics(raw_opps)
    assert metrics["total_detected"] == 4
    assert metrics["total_executable"] == 2
    assert metrics["total_theoretical"] == 2
    assert metrics["conversion_pct"] == 50.0

    assert metrics["by_strategy"]["CROSS_EXCHANGE"]["total"] == 2
    assert metrics["by_strategy"]["CROSS_EXCHANGE"]["executable"] == 1
    assert metrics["by_strategy"]["CROSS_EXCHANGE"]["theoretical"] == 1

    assert metrics["by_strategy"]["TRIANGULAR"]["total"] == 1
    assert metrics["by_strategy"]["STAT_ARB"]["executable"] == 1

    assert "Order book age 300ms exceeds threshold" in metrics["rejection_reasons"]
    assert "Negative Net Edge After Fees & VWAP" in metrics["rejection_reasons"]


def test_parse_execution_metrics() -> None:
    raw_execs = [
        {
            "plan_id": "p1",
            "status": "COMPLETED",
            "realized_pnl": "1.50",
            "estimated_fees": "0.30",
        },
        {
            "plan_id": "p2",
            "status": "UNWOUND",
            "realized_pnl": "-0.20",
            "estimated_fees": "0.10",
        },
        {
            "plan_id": "p3",
            "status": "FAILED",
            "realized_pnl": "0.0",
            "estimated_fees": "0.0",
        },
    ]

    metrics = parse_execution_metrics(raw_execs)
    assert metrics["total_plans"] == 3
    assert metrics["completed"] == 1
    assert metrics["unwound"] == 1
    assert metrics["failed"] == 1
    assert metrics["unwind_rate_pct"] == 33.33
    assert metrics["total_realized_pnl_usdt"] == "1.30"
    assert metrics["total_fees_usdt"] == "0.40"


def test_format_burn_in_report() -> None:
    health = {
        "status": "HEALTHY",
        "execution_mode": "PAPER",
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "active_connections": 2,
        "critical_queue_depth": 0,
        "telemetry_queue_depth": 3,
        "dropped_telemetry_count": 0,
    }
    portfolio = {
        "daily_realized_pnl_usdt": "15.40",
        "high_water_mark_usdt": "20.00",
        "open_positions": [],
    }
    opp_metrics = {
        "total_detected": 10,
        "total_executable": 1,
        "total_theoretical": 9,
        "conversion_pct": 10.0,
        "by_strategy": {
            "CROSS_EXCHANGE": {"total": 6, "executable": 1, "theoretical": 5},
            "TRIANGULAR": {"total": 4, "executable": 0, "theoretical": 4},
            "FUNDING_BASIS": {"total": 0, "executable": 0, "theoretical": 0},
            "STAT_ARB": {"total": 0, "executable": 0, "theoretical": 0},
        },
        "rejection_reasons": {"Negative Net Edge After Fees": 8},
    }
    exec_metrics = {
        "total_plans": 1,
        "completed": 1,
        "unwound": 0,
        "failed": 0,
        "executing": 0,
        "unwind_rate_pct": 0.0,
        "total_realized_pnl_usdt": "1.25",
        "total_fees_usdt": "0.35",
    }

    report = format_burn_in_report(health, portfolio, opp_metrics, exec_metrics)
    assert "CRYPTO ARBITRAGE SYSTEM -- LIVE BURN-IN" in report
    assert "HEALTHY" in report
    assert "CROSS_EXCHANGE" in report
    assert "10.0%" in report
    assert "$1.25 USDT" in report


def test_run_burn_in_poll_unreachable() -> None:
    # Port 59999 should not be running an HTTP server
    report = run_burn_in_poll("http://127.0.0.1:59999")
    assert "[!] Could not connect to Arbitrage Orchestrator" in report

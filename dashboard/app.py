"""Interactive monitoring and control dashboard in Streamlit.

Visualizes system health, kill-switch status, portfolio balances, inventory skew,
stateful hedged positions, opportunity conversion funnel, and performance metrics.
"""

from __future__ import annotations

import os
from typing import Any, Optional
import pandas as pd


# ==============================================================================
# Decoupled, Unit-Testable Dashboard Helper Functions
# ==============================================================================


def build_dashboard_state(
    health_data: dict[str, Any],
    portfolio_data: dict[str, Any],
    opportunities: list[dict[str, Any]],
    executions: list[dict[str, Any]],
    report: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble normalized state dictionary for UI presentation."""
    return {
        "status": health_data.get("status", "UNKNOWN"),
        "execution_mode": health_data.get("execution_mode", "PAPER"),
        "kill_switch_active": health_data.get("kill_switch_active", False),
        "kill_switch_reason": health_data.get("kill_switch_reason"),
        "queue_depth": health_data.get("persistence_queue_depth", {}),
        "portfolio": portfolio_data,
        "opportunities_count": len(opportunities),
        "executions_count": len(executions),
        "report": report or {},
    }


def format_portfolio_summary(portfolio_data: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    """Format raw portfolio dictionary into tabular DataFrame, skew dictionary, and open positions."""
    balances = portfolio_data.get("balances", [])
    if balances:
        df_balances = pd.DataFrame(balances)
        if "updated_at_ms" in df_balances.columns:
            df_balances = df_balances.drop(columns=["updated_at_ms"])
    else:
        df_balances = pd.DataFrame(columns=["exchange", "asset", "free", "locked", "total"])

    skew = portfolio_data.get("skew", {})
    open_positions = portfolio_data.get("open_positions", [])
    return df_balances, skew, open_positions


def format_opportunity_table(opportunities: list[dict[str, Any]]) -> pd.DataFrame:
    """Format opportunities list into presentation DataFrame."""
    if not opportunities:
        return pd.DataFrame(
            columns=["id", "strategy_type", "lifecycle_state", "gross_edge_bps", "net_edge_bps", "target_notional", "expected_net_profit"]
        )

    rows = []
    for opp in opportunities:
        rows.append({
            "id": opp.get("id"),
            "strategy": opp.get("strategy_type"),
            "state": opp.get("lifecycle_state"),
            "gross_bps": float(opp.get("gross_edge_bps", 0)),
            "net_bps": float(opp.get("net_edge_bps", 0)),
            "notional_usdt": float(opp.get("target_notional", 0)),
            "profit_usdt": float(opp.get("expected_net_profit", 0)),
        })
    return pd.DataFrame(rows)


def format_execution_log(executions: list[dict[str, Any]]) -> pd.DataFrame:
    """Format execution plans list into log DataFrame."""
    if not executions:
        return pd.DataFrame(
            columns=["plan_id", "strategy", "status", "target_notional", "realized_pnl", "legs_count"]
        )

    rows = []
    for plan in executions:
        rows.append({
            "plan_id": plan.get("plan_id"),
            "strategy": plan.get("strategy_type"),
            "status": plan.get("status"),
            "notional_usdt": float(plan.get("target_notional", 0)),
            "realized_pnl": float(plan.get("realized_pnl", 0)),
            "legs_count": len(plan.get("legs", [])),
        })
    return pd.DataFrame(rows)


# ==============================================================================
# Streamlit Application Layout
# ==============================================================================


def main() -> None:
    """Streamlit UI entrypoint."""
    import streamlit as st
    import requests

    st.set_page_config(
        page_title="Crypto Arbitrage Intelligence System",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    api_url = os.getenv("API_URL", "http://localhost:8000")

    # Fetch live data from FastAPI backend
    health_data = {}
    portfolio_data = {}
    opportunities = []
    executions = []

    try:
        r_health = requests.get(f"{api_url}/health", timeout=2)
        if r_health.status_code == 200:
            health_data = r_health.json()

        r_port = requests.get(f"{api_url}/portfolio", timeout=2)
        if r_port.status_code == 200:
            portfolio_data = r_port.json()

        r_opps = requests.get(f"{api_url}/opportunities", timeout=2)
        if r_opps.status_code == 200:
            opportunities = r_opps.json()

        r_execs = requests.get(f"{api_url}/executions", timeout=2)
        if r_execs.status_code == 200:
            executions = r_execs.json()
    except Exception:
        pass

    state = build_dashboard_state(health_data, portfolio_data, opportunities, executions)

    # 1. Top Safety & Health Banner
    st.title("⚡ Institutional Crypto Arbitrage Intelligence System")
    st.markdown("---")

    col_h1, col_h2, col_h3, col_h4 = st.columns(4)
    with col_h1:
        st.metric("Execution Mode", state["execution_mode"])
    with col_h2:
        is_halted = state["kill_switch_active"]
        st.metric("Kill Switch", "🚨 HALTED" if is_halted else "✅ NORMAL")
    with col_h3:
        q_depth = state["queue_depth"]
        crit_q = q_depth.get("critical_queue", 0)
        telem_q = q_depth.get("telemetry_queue", 0)
        st.metric("Write Queues", f"Crit: {crit_q} | Telem: {telem_q}")
    with col_h4:
        st.metric("Daily Realized PnL", f"${portfolio_data.get('daily_realized_pnl_usdt', '0.00')} USDT")

    # Kill-Switch Trigger Button
    with st.expander("🛡️ Emergency Risk & Kill-Switch Controls"):
        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("🚨 ACTIVATE GLOBAL KILL SWITCH", type="primary"):
                try:
                    requests.post(
                        f"{api_url}/risk/kill-switch",
                        json={"action": "ACTIVATE", "reason": "Dashboard operator command"},
                        timeout=2,
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to engage kill switch: {e}")
        with col_btn2:
            if st.button("🔄 RESET KILL SWITCH", type="secondary"):
                try:
                    requests.post(
                        f"{api_url}/risk/kill-switch",
                        json={"action": "RESET", "reason": "Dashboard operator resume"},
                        timeout=2,
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to reset kill switch: {e}")

    # 2. Portfolio, Skew & Positions
    st.subheader("💼 Portfolio & Inventory Balancer")
    df_bal, skew_dict, open_pos = format_portfolio_summary(portfolio_data)

    col_p1, col_p2 = st.columns([2, 1])
    with col_p1:
        st.markdown("**Exchange Balances**")
        st.dataframe(df_bal, use_container_width=True)
    with col_p2:
        st.markdown("**Cross-Exchange Skew (Binance vs Kraken)**")
        for asset, s in skew_dict.items():
            b_pct = s.get("BINANCE", 50.0)
            st.write(f"**{asset}**: {b_pct:.1f}% Binance / {100.0 - b_pct:.1f}% Kraken")
            st.progress(min(1.0, max(0.0, float(b_pct) / 100.0)))

    # Stateful Positions (Strategies 3 & 4)
    if open_pos:
        st.markdown("**Active Stateful Positions (Funding Carry & StatArb)**")
        st.json(open_pos)

    # 3. Opportunity & Execution Logs
    st.markdown("---")
    col_o1, col_o2 = st.columns(2)
    with col_o1:
        st.subheader("🎯 Opportunity Funnel")
        df_opps = format_opportunity_table(opportunities)
        st.dataframe(df_opps, use_container_width=True)

    with col_o2:
        st.subheader("📋 Multi-Leg Execution Log")
        df_execs = format_execution_log(executions)
        st.dataframe(df_execs, use_container_width=True)


if __name__ == "__main__":
    main()

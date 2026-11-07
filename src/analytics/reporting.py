"""Performance reporting engine generating structured Markdown and JSON reports."""

from __future__ import annotations

import json
from typing import Any
from src.analytics.performance import PerformanceReport


class ReportGenerator:
    """Generates institutional Markdown and JSON performance reports."""

    @staticmethod
    def generate_json_report(report: PerformanceReport) -> dict[str, Any]:
        """Convert PerformanceReport into a JSON-serializable dictionary."""
        return report.model_dump(mode="json")

    @staticmethod
    def generate_markdown_report(report: PerformanceReport) -> str:
        """Format PerformanceReport into GitHub-flavored Markdown tables."""
        pnl = report.overall_pnl
        funnel = report.funnel
        risk = report.risk_metrics

        lines: list[str] = []
        lines.append("# Quantitative Strategy Performance & Execution Report\n")

        # 1. Executive Summary
        lines.append("## 1. Financial & Portfolio Executive Summary\n")
        lines.append("| Metric | Value |")
        lines.append("| :--- | :--- |")
        lines.append(f"| **Initial Capital** | `${report.initial_capital_usdt:,.2f} USDT` |")
        lines.append(f"| **Final Equity** | `${report.final_equity_usdt:,.2f} USDT` |")
        lines.append(f"| **Net Realized Profit** | `${pnl.net_realized_profit:+,.4f} USDT` |")
        lines.append(f"| **Gross Trading Profit** | `${pnl.gross_profit:+,.4f} USDT` |")
        lines.append(f"| **Total Exchange Fees** | `-${pnl.total_trading_fees:,.4f} USDT` |")
        lines.append(f"| **Total Slippage / Unwind Costs** | `-${pnl.total_slippage_unwind_costs:,.4f} USDT` |")
        lines.append(f"| **Funding Cash Flows Accrued** | `${pnl.funding_pnl_accrued:+,.4f} USDT` |")
        lines.append(f"| **Return on Capital** | `{risk.return_on_capital_pct:+.2f}%` |")
        lines.append(f"| **Max Drawdown (USDT)** | `-${risk.max_drawdown_usdt:,.2f} USDT` |")
        lines.append(f"| **Max Drawdown (%)** | `-{risk.max_drawdown_pct:.2f}%` |")
        lines.append("")

        # 2. Risk-Adjusted Statistical Metrics
        lines.append("## 2. Risk-Adjusted & Trade Statistics\n")
        lines.append("| Metric | Value |")
        lines.append("| :--- | :--- |")
        lines.append(f"| **Total Executed Trades** | `{risk.total_trades}` |")
        lines.append(f"| **Winning Trades** | `{risk.winning_trades}` |")
        lines.append(f"| **Losing Trades** | `{risk.losing_trades}` |")
        lines.append(f"| **Win Rate** | `{risk.win_rate_pct:.2f}%` |")
        lines.append(f"| **Profit Factor** | `{risk.profit_factor:.2f}` |")
        lines.append(f"| **Average Return per Trade** | `{risk.avg_net_return_bps:+.2f} bps` |")
        lines.append(f"| **Sharpe Ratio (Trade)** | `{risk.sharpe_ratio if risk.sharpe_ratio is not None else 'N/A'}` |")
        lines.append(f"| **Sortino Ratio (Trade)** | `{risk.sortino_ratio if risk.sortino_ratio is not None else 'N/A'}` |")
        lines.append("")

        # 3. Opportunity Conversion Funnel
        lines.append("## 3. Opportunity & Execution Conversion Funnel\n")
        lines.append("| Funnel Stage | Count | Conversion Rate |")
        lines.append("| :--- | :--- | :--- |")
        lines.append(f"| **1. Theoretical Opportunities Detected** | `{funnel.total_theoretical_detected}` | 100.0% |")

        theo_to_exec = (
            (funnel.total_executable_detected / funnel.total_theoretical_detected * 100.0)
            if funnel.total_theoretical_detected > 0
            else 0.0
        )
        lines.append(f"| **2. Executable Opportunities (> Min Edge)** | `{funnel.total_executable_detected}` | {theo_to_exec:.1f}% |")

        exec_to_appr = (
            (funnel.total_risk_approved / funnel.total_executable_detected * 100.0)
            if funnel.total_executable_detected > 0
            else 0.0
        )
        lines.append(f"| **3. Risk-Approved Execution Plans** | `{funnel.total_risk_approved}` | {exec_to_appr:.1f}% |")
        lines.append(f"| **4. Risk-Rejected Opportunities** | `{funnel.total_risk_rejected}` | - |")
        lines.append(f"| **5. Completed Multi-Leg Trades** | `{funnel.total_completed_plans}` | - |")
        lines.append(f"| **6. Unwound / Emergency Halted Plans** | `{funnel.total_unwound_or_failed_plans}` | - |")
        lines.append(f"| **Execution Failure Rate** | `{funnel.execution_failure_rate_pct:.2f}%` | - |")
        lines.append("")

        # 4. Strategy Breakdown Matrix
        lines.append("## 4. Multi-Strategy Performance Breakdown\n")
        lines.append("| Strategy Type | Total Plans | Completed | Unwound | Net PnL (USDT) | Win Rate | Profit Factor | Avg Edge (bps) |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for st_name, st_perf in report.strategy_breakdown.items():
            lines.append(
                f"| **{st_name}** | {st_perf.total_plans} | {st_perf.completed_plans} | {st_perf.unwound_plans} | "
                f"`${st_perf.net_pnl_usdt:+,.4f}` | {st_perf.win_rate_pct:.1f}% | {st_perf.profit_factor:.2f} | "
                f"`{st_perf.avg_net_edge_bps:+.2f}` |"
            )
        lines.append("")

        # 5. Lifecycle Segmentation (Paper vs Testnet vs Live)
        lines.append("## 5. Execution Mode & Lifecycle Segmentation\n")
        lines.append("| Lifecycle State | Opportunity Count | Total Notional (USDT) | Expected Profit (USDT) |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for state_name, state_data in report.lifecycle_breakdown.items():
            lines.append(
                f"| `{state_name}` | {state_data['opportunity_count']} | "
                f"`${float(state_data['total_target_notional']):,.2f}` | `${float(state_data['expected_profit_usdt']):+,.4f}` |"
            )
        lines.append("")

        return "\n".join(lines)

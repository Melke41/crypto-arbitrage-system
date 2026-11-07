"""FastAPI control and monitoring routes for the Arbitrage Trading System."""

from __future__ import annotations

from typing import Any, Optional
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


class KillSwitchRequest(BaseModel):
    """Payload to engage or disengage the global kill switch."""

    action: str = Field(..., description="'ACTIVATE' or 'RESET'")
    reason: str = Field(default="Manual operator command", description="Reason for status change")


def create_app(orchestrator: Any) -> FastAPI:
    """Factory creating configured FastAPI app bound to active system orchestrator."""
    app = FastAPI(
        title="Crypto Arbitrage Intelligence & Trading System",
        version="1.0.0",
        description="REST Control & Monitoring API for multi-strategy crypto arbitrage",
    )

    @app.get("/health")
    async def get_health() -> dict[str, Any]:
        """System health status, execution mode, kill-switch status, and persistence queue depths."""
        return orchestrator.get_health_status()

    @app.get("/portfolio")
    async def get_portfolio() -> dict[str, Any]:
        """Current account balances across venues, inventory skew, and open hedged positions."""
        return orchestrator.get_portfolio_summary()

    @app.get("/opportunities")
    async def get_opportunities(
        limit: int = Query(50, ge=1, le=500),
        state: Optional[str] = Query(None, description="Optional OpportunityLifecycleState filter"),
    ) -> list[dict[str, Any]]:
        """Recent theoretical vs. executable arbitrage opportunities."""
        return orchestrator.get_recent_opportunities(limit=limit, state=state)

    @app.get("/executions")
    async def get_executions(
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """Recent execution plans, leg states, and emergency unwinds."""
        return orchestrator.get_recent_executions(limit=limit)

    @app.post("/risk/kill-switch")
    async def set_kill_switch(payload: KillSwitchRequest) -> dict[str, Any]:
        """Immediately activate or reset the global emergency kill switch."""
        action_clean = payload.action.upper()
        if action_clean == "ACTIVATE":
            orchestrator.set_kill_switch(active=True, reason=payload.reason)
        elif action_clean in ("RESET", "DEACTIVATE"):
            orchestrator.set_kill_switch(active=False, reason=payload.reason)
        else:
            raise HTTPException(status_code=400, detail="Invalid action: must be 'ACTIVATE' or 'RESET'")

        return {
            "kill_switch_active": orchestrator.risk_manager.is_kill_switch_active(),
            "kill_switch_reason": orchestrator.risk_manager.kill_switch_reason,
            "message": f"Kill switch successfully set to {action_clean}",
        }

    return app

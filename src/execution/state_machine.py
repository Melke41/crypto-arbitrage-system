"""Multi-leg execution state machine managing deterministic plan transitions."""

from __future__ import annotations

import time
from typing import Optional

from src.core.exceptions import ExecutionError
from src.core.models import (
    ExecutionLegState,
    ExecutionLegStatus,
    ExecutionPlan,
    ExecutionPlanStatus,
)


class InvalidStateTransitionError(ExecutionError):
    """Raised when an invalid plan or leg status transition is attempted."""


class ExecutionStateMachine:
    """Deterministic state machine governing ExecutionPlan and ExecutionLeg lifecycles."""

    # Valid plan status transitions: current_status -> set of allowable next statuses
    VALID_PLAN_TRANSITIONS: dict[ExecutionPlanStatus, set[ExecutionPlanStatus]] = {
        ExecutionPlanStatus.PENDING: {
            ExecutionPlanStatus.EXECUTING,
            ExecutionPlanStatus.FAILED,
        },
        ExecutionPlanStatus.EXECUTING: {
            ExecutionPlanStatus.COMPLETED,
            ExecutionPlanStatus.PARTIALLY_FAILED,
            ExecutionPlanStatus.UNWINDING,
            ExecutionPlanStatus.FAILED,
        },
        ExecutionPlanStatus.PARTIALLY_FAILED: {
            ExecutionPlanStatus.UNWINDING,
            ExecutionPlanStatus.FAILED,
        },
        ExecutionPlanStatus.UNWINDING: {
            ExecutionPlanStatus.UNWOUND,
            ExecutionPlanStatus.FAILED,
        },
        ExecutionPlanStatus.UNWOUND: set(),
        ExecutionPlanStatus.COMPLETED: set(),
        ExecutionPlanStatus.FAILED: set(),
    }

    # Valid leg status transitions
    VALID_LEG_TRANSITIONS: dict[ExecutionLegStatus, set[ExecutionLegStatus]] = {
        ExecutionLegStatus.PENDING: {
            ExecutionLegStatus.SUBMITTED,
            ExecutionLegStatus.FAILED,
            ExecutionLegStatus.CANCELLED,
        },
        ExecutionLegStatus.SUBMITTED: {
            ExecutionLegStatus.PARTIALLY_FILLED,
            ExecutionLegStatus.FILLED,
            ExecutionLegStatus.FAILED,
            ExecutionLegStatus.CANCELLED,
        },
        ExecutionLegStatus.PARTIALLY_FILLED: {
            ExecutionLegStatus.FILLED,
            ExecutionLegStatus.FAILED,
            ExecutionLegStatus.CANCELLED,
            ExecutionLegStatus.UNWOUND,
        },
        ExecutionLegStatus.FILLED: {
            ExecutionLegStatus.UNWOUND,
        },
        ExecutionLegStatus.FAILED: set(),
        ExecutionLegStatus.CANCELLED: set(),
        ExecutionLegStatus.UNWOUND: set(),
    }

    @classmethod
    def transition_plan(
        cls,
        plan: ExecutionPlan,
        new_status: ExecutionPlanStatus,
        reason: Optional[str] = None,
    ) -> ExecutionPlan:
        """Validate and transition an execution plan to a new lifecycle status."""
        current = plan.status
        allowed = cls.VALID_PLAN_TRANSITIONS.get(current, set())

        if new_status not in allowed:
            raise InvalidStateTransitionError(
                f"Invalid ExecutionPlan transition from {current.value} to {new_status.value}."
            )

        now_ms = int(time.time() * 1000)
        plan.status = new_status
        plan.updated_at_ms = now_ms
        return plan

    @classmethod
    def transition_leg(
        cls,
        leg: ExecutionLegState,
        new_status: ExecutionLegStatus,
        error_message: Optional[str] = None,
    ) -> ExecutionLegState:
        """Validate and transition an individual execution leg state."""
        current = leg.status
        allowed = cls.VALID_LEG_TRANSITIONS.get(current, set())

        if new_status not in allowed:
            raise InvalidStateTransitionError(
                f"Invalid ExecutionLeg transition from {current.value} to {new_status.value}."
            )

        now_ms = int(time.time() * 1000)
        leg.status = new_status

        if new_status == ExecutionLegStatus.SUBMITTED and not leg.submission_time_ms:
            leg.submission_time_ms = now_ms
        elif new_status in (
            ExecutionLegStatus.FILLED,
            ExecutionLegStatus.FAILED,
            ExecutionLegStatus.CANCELLED,
            ExecutionLegStatus.UNWOUND,
        ):
            leg.completion_time_ms = now_ms

        if error_message:
            leg.error_message = error_message

        return leg

"""Asynchronous dual-queue persistence engine package."""

from src.persistence.database import create_db_engine, drop_db, get_session_factory, init_db
from src.persistence.models import (
    BalanceSnapshotRecord,
    Base,
    ExecutionLegRecord,
    ExecutionPlanRecord,
    HedgedPositionRecord,
    OpportunityRecord,
    OrderBookSnapshotRecord,
    RiskEventRecord,
)
from src.persistence.repositories import ArbitrageRepository
from src.persistence.writer_worker import DualQueuePersistenceWriter

__all__ = [
    "Base",
    "OpportunityRecord",
    "ExecutionPlanRecord",
    "ExecutionLegRecord",
    "HedgedPositionRecord",
    "BalanceSnapshotRecord",
    "OrderBookSnapshotRecord",
    "RiskEventRecord",
    "create_db_engine",
    "get_session_factory",
    "init_db",
    "drop_db",
    "ArbitrageRepository",
    "DualQueuePersistenceWriter",
]

"""Rolling Time-Series Ring Buffer for Statistical Arbitrage and Basis Analysis.

Zero Pandas on hot path. Uses collections.deque and numpy arrays for O(1) rolling statistics.
"""

from __future__ import annotations

import math
from collections import deque
from decimal import Decimal
from typing import Optional

import numpy as np

from src.core.interfaces import TimeSeriesBuffer


class RollingTimeSeriesBuffer:
    """Pre-allocated ring buffer storing synchronized price series and basis history."""

    def __init__(self, maxlen: int = 600) -> None:
        self.maxlen = maxlen
        self.timestamps: deque[int] = deque(maxlen=maxlen)
        self.series_a: deque[float] = deque(maxlen=maxlen)
        self.series_b: deque[float] = deque(maxlen=maxlen)

    def append(self, timestamp_ms: int, val_a: Decimal, val_b: Optional[Decimal] = None) -> None:
        """Append price ticks to ring buffer."""
        self.timestamps.append(timestamp_ms)
        self.series_a.append(float(val_a))
        if val_b is not None:
            self.series_b.append(float(val_b))

    def __len__(self) -> int:
        return len(self.timestamps)

    def is_ready(self, min_periods: int = 30) -> bool:
        """Check if buffer has accumulated sufficient ticks for statistical validity."""
        return len(self.timestamps) >= min_periods

    def get_spread_series(
        self, beta: Decimal
    ) -> tuple[list[Decimal], Decimal, Decimal, Decimal]:
        """Compute rolling spread series S_t = ln(P_a) - beta * ln(P_b), mean, and std.

        Returns:
            tuple: (spread_list, mean_spread, std_spread, current_spread)
        """
        if len(self.series_a) < 2 or len(self.series_b) < 2:
            return [], Decimal("0"), Decimal("0"), Decimal("0")

        n = min(len(self.series_a), len(self.series_b))
        arr_a = np.array(list(self.series_a)[-n:], dtype=np.float64)
        arr_b = np.array(list(self.series_b)[-n:], dtype=np.float64)

        b = float(beta)
        # Avoid log of zero or negative numbers
        arr_a = np.clip(arr_a, 1e-8, None)
        arr_b = np.clip(arr_b, 1e-8, None)

        spreads = np.log(arr_a) - b * np.log(arr_b)
        mean_val = float(np.mean(spreads))
        std_val = float(np.std(spreads))
        curr_val = float(spreads[-1])

        spreads_dec = [Decimal(str(round(s, 8))) for s in spreads]
        return (
            spreads_dec,
            Decimal(str(round(mean_val, 8))),
            Decimal(str(round(std_val, 8))),
            Decimal(str(round(curr_val, 8))),
        )

    def calculate_z_score(
        self, beta: Decimal, current_spread: Optional[Decimal] = None
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Calculate Z-score: (current_spread - rolling_mean) / rolling_std.

        Returns:
            tuple: (z_score, mean, std)
        """
        _, mean_dec, std_dec, last_spread = self.get_spread_series(beta)
        spread_to_use = current_spread if current_spread is not None else last_spread

        if std_dec == Decimal("0") or std_dec < Decimal("1e-8"):
            return Decimal("0"), mean_dec, std_dec

        z = (spread_to_use - mean_dec) / std_dec
        return Decimal(str(round(float(z), 4))), mean_dec, std_dec

    def estimate_half_life(self, beta: Decimal) -> Optional[Decimal]:
        """Estimate Ornstein-Uhlenbeck mean-reversion half-life via lag regression.

        Delta S_t = theta * (mu - S_{t-1}) + eps
        tau = -ln(2) / lambda where lambda is AR(1) slope
        """
        if len(self.series_a) < 10 or len(self.series_b) < 10:
            return None

        n = min(len(self.series_a), len(self.series_b))
        arr_a = np.array(list(self.series_a)[-n:], dtype=np.float64)
        arr_b = np.array(list(self.series_b)[-n:], dtype=np.float64)
        b = float(beta)

        spreads = np.log(np.clip(arr_a, 1e-8, None)) - b * np.log(np.clip(arr_b, 1e-8, None))
        lag_spreads = spreads[:-1]
        delta_spreads = spreads[1:] - lag_spreads

        # Regress delta_spreads on lag_spreads: delta_spreads = alpha + lambda * lag_spreads
        X = np.column_stack([np.ones_like(lag_spreads), lag_spreads])
        try:
            # OLS
            theta = np.linalg.lstsq(X, delta_spreads, rcond=None)[0][1]
            if theta >= 0:
                # Process is not mean-reverting (explosive or random walk)
                return None
            half_life = -math.log(2.0) / theta
            return Decimal(str(round(half_life, 2)))
        except (np.linalg.LinAlgError, ValueError):
            return None

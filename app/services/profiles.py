"""Intraday demand profiles shared by the synthetic data generator and the live ad simulator (IST hours 0-23)."""
from __future__ import annotations

import numpy as np

_RAW = np.array([0.6, 0.35, 0.2, 0.15, 0.12, 0.15, 0.3, 0.6, 1.0, 1.3, 1.5, 1.7,
                 1.9, 1.9, 1.7, 1.6, 1.6, 1.8, 2.1, 2.5, 2.7, 2.6, 2.0, 1.2])
INTRADAY = _RAW / _RAW.sum()  # share of a day's orders in each hour
AD_CURVE = INTRADAY ** 0.7 / (INTRADAY ** 0.7).sum()  # ad delivery is flatter than shopping

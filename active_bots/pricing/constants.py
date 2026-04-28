"""Constants for BTC variance estimation and fair price computation."""

# Crypto markets trade 24/7 -- use calendar year for annualization.
SECONDS_PER_YEAR = 365.25 * 24 * 3600  # 31_557_600

# Default EWMA decay factor (RiskMetrics daily = 0.94; reasonable for 5s crypto returns)
DEFAULT_EWMA_LAMBDA = 0.94

# Default sampling interval -- 5s avoids microstructure noise from bid-ask bounce
DEFAULT_DELTA_SECONDS = 5.0

# Minimum observations before EWMA emits a sigma estimate
DEFAULT_MIN_WARMUP = 10

# BTC 5m market duration
MARKET_DURATION_S = 300

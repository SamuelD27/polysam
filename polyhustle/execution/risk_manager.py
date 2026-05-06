"""Shim: re-export ``RiskConfig`` and ``RiskManager`` from ``active_bots.execution.risk_manager``."""

from active_bots.execution.risk_manager import RiskConfig, RiskManager

__all__ = ["RiskConfig", "RiskManager"]

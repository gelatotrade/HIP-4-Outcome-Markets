"""Centralised env-var config.

Loads `.env` if present (no python-dotenv dependency required) and
exposes typed accessors. Used by the dashboard and the renderer; the
Rust executor reads the same env vars natively.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass


def _load_dotenv(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val and val.strip().lstrip("-").isdigit() else default


def _float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    user_address: str | None
    api_wallet_key: str | None
    network: str
    vault_address: str | None

    log_level: str
    dashboard_host: str
    dashboard_port: int
    dashboard_refresh_ms: int

    risk_max_open_legs: int
    risk_max_gross_notional_usd: float
    risk_max_perp_btc: float
    risk_per_leg_notional_usd: float

    executor_signal_socket: str
    executor_control_host: str
    executor_control_port: int

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            user_address=os.environ.get("HYPERLIQUID_USER_ADDRESS") or None,
            api_wallet_key=os.environ.get("HYPERLIQUID_API_WALLET_KEY") or None,
            network=os.environ.get("HYPERLIQUID_NETWORK", "mainnet"),
            vault_address=os.environ.get("HYPERLIQUID_VAULT_ADDRESS") or None,
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            dashboard_host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_int("DASHBOARD_PORT", 8050),
            dashboard_refresh_ms=_int("DASHBOARD_REFRESH_MS", 2000),
            risk_max_open_legs=_int("RISK_MAX_OPEN_LEGS", 20),
            risk_max_gross_notional_usd=_float("RISK_MAX_GROSS_NOTIONAL_USD", 250_000.0),
            risk_max_perp_btc=_float("RISK_MAX_PERP_BTC", 10.0),
            risk_per_leg_notional_usd=_float("RISK_PER_LEG_NOTIONAL_USD", 10_000.0),
            executor_signal_socket=os.environ.get("EXECUTOR_SIGNAL_SOCKET",
                                                  "/tmp/hip4-exec.sock"),
            executor_control_host=os.environ.get("EXECUTOR_CONTROL_HOST", "127.0.0.1"),
            executor_control_port=_int("EXECUTOR_CONTROL_PORT", 8765),
        )


CONFIG = Config.from_env()

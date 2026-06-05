"""MetaTrader5 client wrapper with initialization guards and timeout protection."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

import MetaTrader5 as mt5

from constants import (
    FILLING_MAP,
    MAX_LOT_SIZE,
    MIN_LOT_SIZE,
    ORDER_TYPE_MAP,
    PENDING_ORDER_TYPES,
    TIME_MAP,
    TIMEFRAME_MAP,
)

T = TypeVar("T")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._#-]{1,32}$")

_suspended = False
_config: dict[str, Any] | None = None


class MT5Error(Exception):
    """Raised when an MT5 operation fails or the client is suspended."""

    def __init__(self, message: str, *, suspend: bool = False) -> None:
        super().__init__(message)
        self.suspend = suspend


def is_suspended() -> bool:
    return _suspended


def clear_suspend() -> None:
    global _suspended
    _suspended = False


def _load_config() -> dict[str, Any]:
    global _config
    if _config is not None:
        return _config

    if not CONFIG_PATH.exists():
        raise MT5Error(f"Config file not found: {CONFIG_PATH}")

    with CONFIG_PATH.open(encoding="utf-8") as handle:
        _config = json.load(handle)

    required = ("login", "password", "server")
    missing = [key for key in required if not _config.get(key)]
    if missing:
        raise MT5Error(
            f"Missing required config keys: {', '.join(missing)}. "
            "Populate config.json with OANDA demo credentials."
        )

    return _config


def _run_with_timeout(
    func: Callable[..., T],
    *args: Any,
    timeout: float | None = None,
    health_check: bool = False,
    **kwargs: Any,
) -> T | None:
    """Execute an MT5 call in a daemon thread; return None on timeout."""
    global _suspended

    config = _load_config()
    if timeout is None:
        timeout = (
            config.get("health_timeout_sec", 15)
            if health_check
            else config.get("default_timeout_sec", 60)
        )

    result: list[T | None] = [None]
    error: list[BaseException | None] = [None]

    def target() -> None:
        try:
            result[0] = func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — propagate any MT5 failure
            error[0] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        _suspended = True
        return None

    if error[0] is not None:
        raise error[0]

    return result[0]


def _last_error() -> str:
    code, message = mt5.last_error()
    return f"MT5 error {code}: {message}"


def ensure_initialized(*, health_check: bool = False) -> None:
    """
    Three-step initialization guard called before every tool:
    terminal_info → account_info → mt5.login()
    """
    global _suspended

    if _suspended:
        raise MT5Error("MT5 client suspended after timeout — action: SUSPEND", suspend=True)

    config = _load_config()
    timeout = config.get("health_timeout_sec", 15) if health_check else None

    terminal = _run_with_timeout(mt5.terminal_info, timeout=timeout, health_check=health_check)
    if terminal is None:
        raise MT5Error("MT5 terminal_info timed out — action: SUSPEND", suspend=True)

    if not terminal.connected:
        init_kwargs: dict[str, Any] = {
            "login": int(config["login"]),
            "password": str(config["password"]),
            "server": str(config["server"]),
            "timeout": int(config.get("timeout_ms", 60000)),
        }
        mt5_path = config.get("mt5_path")
        if mt5_path:
            init_kwargs["path"] = str(mt5_path)

        initialized = _run_with_timeout(mt5.initialize, **init_kwargs, timeout=timeout, health_check=health_check)
        if initialized is None:
            raise MT5Error("MT5 initialize timed out — action: SUSPEND", suspend=True)
        if not initialized:
            raise MT5Error(f"MT5 initialize failed: {_last_error()}")

    account = _run_with_timeout(mt5.account_info, timeout=timeout, health_check=health_check)
    if account is None:
        raise MT5Error("MT5 account_info timed out — action: SUSPEND", suspend=True)

    if account.login != int(config["login"]):
        logged_in = _run_with_timeout(
            mt5.login,
            int(config["login"]),
            str(config["password"]),
            str(config["server"]),
            timeout=timeout,
            health_check=health_check,
        )
        if logged_in is None:
            raise MT5Error("MT5 login timed out — action: SUSPEND", suspend=True)
        if not logged_in:
            raise MT5Error(f"MT5 login failed: {_last_error()}")


def run_mt5(
    func: Callable[..., T],
    *args: Any,
    health_check: bool = False,
    **kwargs: Any,
) -> T:
    """Run an MT5 API call with timeout protection after ensure_initialized."""
    ensure_initialized(health_check=health_check)
    result = _run_with_timeout(func, *args, health_check=health_check, **kwargs)
    if result is None:
        raise MT5Error("MT5 operation timed out — action: SUSPEND", suspend=True)
    return result


def validate_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not _SYMBOL_PATTERN.match(normalized):
        raise MT5Error(f"Invalid symbol: {symbol!r}")
    return normalized


def validate_timeframe(timeframe: str) -> int:
    key = timeframe.strip().upper()
    if key not in TIMEFRAME_MAP:
        allowed = ", ".join(sorted(TIMEFRAME_MAP))
        raise MT5Error(f"Invalid timeframe {timeframe!r}. Allowed: {allowed}")
    return TIMEFRAME_MAP[key]


def validate_order_type(order_type: str) -> tuple[str, int]:
    key = order_type.strip().upper()
    if key not in ORDER_TYPE_MAP:
        allowed = ", ".join(sorted(ORDER_TYPE_MAP))
        raise MT5Error(f"Invalid order_type {order_type!r}. Allowed: {allowed}")
    return key, ORDER_TYPE_MAP[key]


def validate_filling(filling: str | None) -> int | None:
    if filling is None:
        return None
    key = filling.strip().upper()
    if key not in FILLING_MAP:
        allowed = ", ".join(sorted(FILLING_MAP))
        raise MT5Error(f"Invalid filling {filling!r}. Allowed: {allowed}")
    return FILLING_MAP[key]


def validate_time_type(time_type: str | None) -> int:
    if time_type is None:
        return TIME_MAP["GTC"]
    key = time_type.strip().upper()
    if key not in TIME_MAP:
        allowed = ", ".join(sorted(TIME_MAP))
        raise MT5Error(f"Invalid time_type {time_type!r}. Allowed: {allowed}")
    return TIME_MAP[key]


def validate_lot_size(lot_size: float) -> float:
    if lot_size < MIN_LOT_SIZE or lot_size > MAX_LOT_SIZE:
        raise MT5Error(f"lot_size must be between {MIN_LOT_SIZE} and {MAX_LOT_SIZE}")
    return round(lot_size, 2)


def validate_price(price: float, *, field: str) -> float:
    if price <= 0:
        raise MT5Error(f"{field} must be positive")
    return price


def validate_ticket(ticket: int) -> int:
    if ticket <= 0:
        raise MT5Error("ticket must be a positive integer")
    return ticket


def named_tuple_to_dict(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    return obj._asdict()


def named_tuple_list_to_dicts(items: Any) -> list[dict[str, Any]]:
    if items is None:
        return []
    return [item._asdict() for item in items]


def resolve_filling_mode(symbol: str, preferred: str | None = None) -> int:
    info = run_mt5(mt5.symbol_info, symbol)
    if info is None:
        raise MT5Error(f"Symbol not found: {symbol}")

    filling_mode = info.filling_mode
    if preferred:
        mode = validate_filling(preferred)
        if mode is not None and filling_mode & mode:
            return mode

    for candidate in (FILLING_MAP["FOK"], FILLING_MAP["IOC"], FILLING_MAP["RETURN"]):
        if filling_mode & candidate:
            return candidate

    raise MT5Error(f"No supported filling mode for {symbol}")


def is_pending_order_type(order_type: str) -> bool:
    return order_type in PENDING_ORDER_TYPES

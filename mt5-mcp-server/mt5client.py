"""MetaTrader5 client wrapper with initialization guards and timeout protection."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
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
LOCAL_CONFIG_PATH = Path(__file__).resolve().parent / "config.local.json"

# Credentials may come from the environment (highest precedence) so they never
# touch a file at all. config.local.json (gitignored) is the file-based home
# for secrets; the tracked config.json holds placeholders + defaults only.
_ENV_OVERRIDES = {
    "login": "MT5_LOGIN",
    "password": "MT5_PASSWORD",
    "server": "MT5_SERVER",
}

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._#-]{1,32}$")

# stdio dispatch is effectively serial today, but the lock keeps the suspend
# state machine correct if the server ever moves to a concurrent transport.
_state_lock = threading.Lock()
_suspended = False
_last_write_timeout: dict[str, Any] | None = None
_config: dict[str, Any] | None = None


class MT5Error(Exception):
    """Raised when an MT5 operation fails or the client is suspended."""

    def __init__(self, message: str, *, suspend: bool = False) -> None:
        super().__init__(message)
        self.suspend = suspend


def is_suspended() -> bool:
    with _state_lock:
        return _suspended


def _set_suspended() -> None:
    global _suspended
    with _state_lock:
        _suspended = True


def clear_suspend() -> dict[str, Any]:
    """Clear suspend + orphan marker. Returns what was cleared for the audit log."""
    global _suspended, _last_write_timeout
    with _state_lock:
        cleared = {
            "was_suspended": _suspended,
            "last_write_timeout": _last_write_timeout,
        }
        _suspended = False
        _last_write_timeout = None
    return cleared


def last_write_timeout() -> dict[str, Any] | None:
    with _state_lock:
        return dict(_last_write_timeout) if _last_write_timeout else None


def _record_write_timeout(context: str, symbol: str | None) -> None:
    global _last_write_timeout
    with _state_lock:
        _last_write_timeout = {
            "context": context,
            "symbol": symbol,
            "timed_out_at": datetime.now(timezone.utc).isoformat(),
        }


def reset_config_cache() -> None:
    """Testing hook — force the next _load_config() to re-read files/env."""
    global _config
    _config = None


def _load_config() -> dict[str, Any]:
    global _config
    if _config is not None:
        return _config

    if not CONFIG_PATH.exists():
        raise MT5Error(f"Config file not found: {CONFIG_PATH}")

    with CONFIG_PATH.open(encoding="utf-8") as handle:
        merged = json.load(handle)

    # Gitignored local overlay — the file-based home for real credentials.
    if LOCAL_CONFIG_PATH.exists():
        with LOCAL_CONFIG_PATH.open(encoding="utf-8") as handle:
            merged.update(json.load(handle))

    # Environment variables win over both files.
    for key, env_name in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            merged[key] = int(value) if key == "login" else value

    required = ("login", "password", "server")
    missing = [key for key in required if not merged.get(key)]
    if missing:
        raise MT5Error(
            f"Missing required config keys: {', '.join(missing)}. "
            "Put credentials in config.local.json (gitignored) or the "
            "MT5_LOGIN/MT5_PASSWORD/MT5_SERVER environment variables — "
            "never in the tracked config.json."
        )

    _config = merged
    return _config


# Sentinel distinguishing "the call timed out" from "the call returned None"
# (mt5.symbol_info legitimately returns None for unknown symbols — that must
# not be mistaken for a hung terminal).
TIMEOUT = object()


def _run_with_timeout(
    func: Callable[..., T],
    *args: Any,
    timeout: float | None = None,
    health_check: bool = False,
    **kwargs: Any,
) -> T | None | object:
    """Execute an MT5 call in a daemon thread; return TIMEOUT sentinel on timeout.

    NOTE: a timed-out thread is abandoned, not killed — the underlying MT5
    call may still complete later. For writes this means an order can reach
    the broker after we reported failure; run_mt5_write records that risk.
    """
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
        _set_suspended()
        return TIMEOUT

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
    if is_suspended():
        raise MT5Error("MT5 client suspended after timeout — action: SUSPEND", suspend=True)

    config = _load_config()
    timeout = config.get("health_timeout_sec", 15) if health_check else None

    terminal = _run_with_timeout(mt5.terminal_info, timeout=timeout, health_check=health_check)
    if terminal is TIMEOUT:
        raise MT5Error("MT5 terminal_info timed out — action: SUSPEND", suspend=True)

    if terminal is None or not terminal.connected:
        # terminal_info returns None before mt5.initialize() — not a hang.
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
        if initialized is TIMEOUT:
            raise MT5Error("MT5 initialize timed out — action: SUSPEND", suspend=True)
        if not initialized:
            raise MT5Error(f"MT5 initialize failed: {_last_error()}")

    account = _run_with_timeout(mt5.account_info, timeout=timeout, health_check=health_check)
    if account is TIMEOUT:
        raise MT5Error("MT5 account_info timed out — action: SUSPEND", suspend=True)

    if account is None or account.login != int(config["login"]):
        logged_in = _run_with_timeout(
            mt5.login,
            int(config["login"]),
            str(config["password"]),
            str(config["server"]),
            timeout=timeout,
            health_check=health_check,
        )
        if logged_in is TIMEOUT:
            raise MT5Error("MT5 login timed out — action: SUSPEND", suspend=True)
        if not logged_in:
            raise MT5Error(f"MT5 login failed: {_last_error()}")


def run_mt5(
    func: Callable[..., T],
    *args: Any,
    health_check: bool = False,
    **kwargs: Any,
) -> T:
    """Run an MT5 API call with timeout protection after ensure_initialized.

    A legitimate None result (e.g. symbol_info for an unknown symbol) is
    returned as-is — only a real timeout raises and suspends.
    """
    ensure_initialized(health_check=health_check)
    result = _run_with_timeout(func, *args, health_check=health_check, **kwargs)
    if result is TIMEOUT:
        raise MT5Error("MT5 operation timed out — action: SUSPEND", suspend=True)
    return result


def run_mt5_write(
    func: Callable[..., T],
    *args: Any,
    context: str,
    symbol: str | None = None,
    **kwargs: Any,
) -> T:
    """Like run_mt5, but a timeout records an orphaned-write marker.

    The abandoned thread may still deliver the order to the broker after we
    report failure — callers (and humans) must reconcile broker state before
    clearing suspend.
    """
    ensure_initialized()
    result = _run_with_timeout(func, *args, **kwargs)
    if result is TIMEOUT:
        _record_write_timeout(context, symbol)
        raise MT5Error(
            f"MT5 write '{context}' timed out — the order MAY STILL HAVE "
            "EXECUTED at the broker. Reconcile open positions/orders against "
            "decision logs before clearing suspend. action: SUSPEND",
            suspend=True,
        )
    return result


def config_magic() -> int:
    """Magic number stamped on gateway-originated orders (config key 'magic')."""
    return int(_load_config().get("magic", 260605))


def gateway_status() -> dict[str, Any]:
    """Suspend/orphan state — readable even while suspended."""
    return {
        "success": True,
        "suspended": is_suspended(),
        "last_write_timeout": last_write_timeout(),
        "allow_real_account": bool(_load_config().get("allow_real_account", False)),
    }


# MT5 account trade modes (mt5.account_info().trade_mode)
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2


def ensure_write_allowed() -> None:
    """Refuse write operations on a real-money account unless explicitly enabled.

    Live trading requires config.json to set "allow_real_account": true — an
    allow-list gate so pointing the gateway at real credentials by mistake
    cannot place orders. Read tools are unaffected.
    """
    config = _load_config()
    account = _run_with_timeout(mt5.account_info)
    if account is TIMEOUT:
        raise MT5Error("MT5 account_info timed out — action: SUSPEND", suspend=True)
    if account is None:
        raise MT5Error("Write refused: account_info unavailable")

    if account.trade_mode != ACCOUNT_TRADE_MODE_DEMO and not config.get(
        "allow_real_account", False
    ):
        raise MT5Error(
            f"Write refused: account {account.login} is not a demo account "
            f"(trade_mode={account.trade_mode}) and allow_real_account is not "
            "enabled in config.json"
        )


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
    if key in {"SPECIFIED", "SPECIFIED_DAY"}:
        # MT5 rejects these without an expiration timestamp, which this
        # gateway does not expose — fail here with a clear message instead.
        raise MT5Error(
            f"time_type {key} requires an expiration parameter the gateway "
            "does not support — use GTC or DAY"
        )
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

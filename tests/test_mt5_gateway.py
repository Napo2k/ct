"""Cross-platform unit tests for the MT5 MCP gateway.

Installs a fake MetaTrader5 module into sys.modules before importing the
gateway, so the handler/validation/suspend logic runs under pytest on any OS
(the real package only imports on Windows).
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fake MetaTrader5 module (must exist before gateway imports)
# ---------------------------------------------------------------------------

class Record:
    """Named-tuple stand-in: attribute access + _asdict()."""

    def __init__(self, **fields):
        self._fields = fields
        for key, value in fields.items():
            setattr(self, key, value)

    def _asdict(self):
        return dict(self._fields)


def _build_fake_mt5() -> types.ModuleType:
    fake = types.ModuleType("MetaTrader5")
    constant_names = [
        "TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_M30",
        "TIMEFRAME_H1", "TIMEFRAME_H4", "TIMEFRAME_D1", "TIMEFRAME_W1",
        "TIMEFRAME_MN1",
        "ORDER_TYPE_BUY", "ORDER_TYPE_SELL", "ORDER_TYPE_BUY_LIMIT",
        "ORDER_TYPE_SELL_LIMIT", "ORDER_TYPE_BUY_STOP", "ORDER_TYPE_SELL_STOP",
        "ORDER_TIME_GTC", "ORDER_TIME_DAY", "ORDER_TIME_SPECIFIED",
        "ORDER_TIME_SPECIFIED_DAY",
        "TRADE_ACTION_DEAL", "TRADE_ACTION_PENDING", "TRADE_ACTION_SLTP",
        "TRADE_ACTION_MODIFY", "TRADE_ACTION_REMOVE", "TRADE_ACTION_CLOSE_BY",
        "POSITION_TYPE_BUY", "POSITION_TYPE_SELL",
    ]
    for i, name in enumerate(constant_names):
        setattr(fake, name, i + 1)
    # Filling-mode constants are bit flags probed with `&`
    fake.ORDER_FILLING_FOK = 1
    fake.ORDER_FILLING_IOC = 2
    fake.ORDER_FILLING_RETURN = 4
    fake.TRADE_RETCODE_DONE = 10009

    def _unset(*args, **kwargs):
        raise AssertionError("fake mt5 function not configured for this test")

    for fn in ("terminal_info", "account_info", "initialize", "login", "last_error",
               "positions_get", "orders_get", "symbol_info", "symbol_info_tick",
               "symbol_select", "order_send", "copy_rates_from_pos", "symbols_get",
               "history_deals_get", "history_orders_get"):
        setattr(fake, fn, _unset)
    return fake


fake_mt5 = _build_fake_mt5()
sys.modules.setdefault("MetaTrader5", fake_mt5)
sys.path.insert(0, str(ROOT / "mt5-mcp-server"))

import mt5client  # noqa: E402
from handlers import orders, positions, symbols  # noqa: E402

DEMO = 0
REAL = 2
TEST_CONFIG = {
    "login": 12345,
    "password": "x",
    "server": "TEST",
    "default_timeout_sec": 5,
    "health_timeout_sec": 5,
    "allow_real_account": False,
    "magic": 777,
}


@pytest.fixture(autouse=True)
def gateway_state(monkeypatch):
    """Healthy, initialized gateway with a demo account; tests override pieces."""
    mt5client.clear_suspend()
    mt5client._config = dict(TEST_CONFIG)

    monkeypatch.setattr(fake_mt5, "terminal_info", lambda: Record(connected=True))
    monkeypatch.setattr(
        fake_mt5, "account_info",
        lambda: Record(login=12345, trade_mode=DEMO),
    )
    monkeypatch.setattr(fake_mt5, "symbol_select", lambda *a: True)
    yield
    mt5client.clear_suspend()
    mt5client._config = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_symbol_validation():
    assert mt5client.validate_symbol(" eurusd ") == "EURUSD"
    with pytest.raises(mt5client.MT5Error):
        mt5client.validate_symbol("EUR USD")
    with pytest.raises(mt5client.MT5Error):
        mt5client.validate_symbol("EUR;DROP")


def test_lot_size_bounds():
    assert mt5client.validate_lot_size(0.011) == 0.01
    with pytest.raises(mt5client.MT5Error):
        mt5client.validate_lot_size(0.001)
    with pytest.raises(mt5client.MT5Error):
        mt5client.validate_lot_size(1000)


def test_time_type_specified_rejected():
    assert mt5client.validate_time_type(None) == fake_mt5.ORDER_TIME_GTC
    assert mt5client.validate_time_type("DAY") == fake_mt5.ORDER_TIME_DAY
    with pytest.raises(mt5client.MT5Error, match="expiration"):
        mt5client.validate_time_type("SPECIFIED")
    with pytest.raises(mt5client.MT5Error, match="expiration"):
        mt5client.validate_time_type("SPECIFIED_DAY")


# ---------------------------------------------------------------------------
# Config overlay precedence
# ---------------------------------------------------------------------------

def test_config_local_overlay_and_env_win(tmp_path, monkeypatch):
    base = tmp_path / "config.json"
    base.write_text('{"login": 0, "password": "", "server": "", "magic": 1}')
    local = tmp_path / "config.local.json"
    local.write_text('{"login": 111, "password": "from-local", "server": "LOCAL"}')

    monkeypatch.setattr(mt5client, "CONFIG_PATH", base)
    monkeypatch.setattr(mt5client, "LOCAL_CONFIG_PATH", local)
    monkeypatch.setenv("MT5_PASSWORD", "from-env")
    mt5client.reset_config_cache()

    config = mt5client._load_config()
    assert config["login"] == 111            # local overlay over tracked file
    assert config["password"] == "from-env"  # env beats both files
    assert config["server"] == "LOCAL"
    assert config["magic"] == 1              # untouched keys survive


def test_config_missing_credentials_mentions_local_file(tmp_path, monkeypatch):
    base = tmp_path / "config.json"
    base.write_text('{"login": 0, "password": "", "server": ""}')
    monkeypatch.setattr(mt5client, "CONFIG_PATH", base)
    monkeypatch.setattr(mt5client, "LOCAL_CONFIG_PATH", tmp_path / "absent.json")
    monkeypatch.delenv("MT5_LOGIN", raising=False)
    monkeypatch.delenv("MT5_PASSWORD", raising=False)
    monkeypatch.delenv("MT5_SERVER", raising=False)
    mt5client.reset_config_cache()

    with pytest.raises(mt5client.MT5Error, match="config.local.json"):
        mt5client._load_config()


# ---------------------------------------------------------------------------
# Real-account write gate
# ---------------------------------------------------------------------------

def test_write_gate_allows_demo():
    mt5client.ensure_write_allowed()  # no raise


def test_write_gate_blocks_real_account(monkeypatch):
    monkeypatch.setattr(
        fake_mt5, "account_info", lambda: Record(login=12345, trade_mode=REAL)
    )
    with pytest.raises(mt5client.MT5Error, match="allow_real_account"):
        mt5client.ensure_write_allowed()


def test_write_gate_real_account_optin(monkeypatch):
    mt5client._config = dict(TEST_CONFIG, allow_real_account=True)
    monkeypatch.setattr(
        fake_mt5, "account_info", lambda: Record(login=12345, trade_mode=REAL)
    )
    mt5client.ensure_write_allowed()  # no raise


# ---------------------------------------------------------------------------
# Order request construction
# ---------------------------------------------------------------------------

def _capture_order_send(monkeypatch, retcode=None):
    sent = []

    def order_send(request):
        sent.append(request)
        return Record(retcode=retcode or fake_mt5.TRADE_RETCODE_DONE, order=42)

    monkeypatch.setattr(fake_mt5, "order_send", order_send)
    return sent


def test_place_pending_order_uses_config_magic(monkeypatch):
    sent = _capture_order_send(monkeypatch)
    monkeypatch.setattr(fake_mt5, "symbol_info_tick", lambda s: Record(bid=1.08, ask=1.0801))
    monkeypatch.setattr(
        fake_mt5, "symbol_info", lambda s: Record(filling_mode=fake_mt5.ORDER_FILLING_FOK)
    )
    result = orders.place_order(
        "EURUSD", "BUY_LIMIT", 0.01, price=1.0750, stop_loss=1.0720, take_profit=1.0825,
    )
    assert result["success"]
    request = sent[0]
    assert request["magic"] == 777
    assert request["action"] == fake_mt5.TRADE_ACTION_PENDING
    assert request["price"] == 1.0750


def test_place_pending_order_requires_price(monkeypatch):
    monkeypatch.setattr(fake_mt5, "symbol_info_tick", lambda s: Record(bid=1.08, ask=1.0801))
    result = orders.place_order("EURUSD", "SELL_LIMIT", 0.01)
    assert not result["success"]
    assert "price is required" in result["error"]


def test_market_order_entry_window_rejection(monkeypatch):
    sent = _capture_order_send(monkeypatch)
    monkeypatch.setattr(fake_mt5, "symbol_info_tick", lambda s: Record(bid=1.0900, ask=1.0901))
    result = orders.place_order(
        "EURUSD", "BUY", 0.01, entry_window=[1.0790, 1.0810],
    )
    assert not result["success"]
    assert "entry_window" in result["error"]
    assert sent == []  # never reached the broker


# ---------------------------------------------------------------------------
# Partial close validation
# ---------------------------------------------------------------------------

def _open_position(monkeypatch, volume=0.02):
    pos = Record(
        ticket=99, symbol="EURUSD", volume=volume, type=fake_mt5.POSITION_TYPE_BUY,
        sl=1.07, tp=1.10, magic=777,
    )
    monkeypatch.setattr(fake_mt5, "positions_get", lambda **kw: [pos])
    monkeypatch.setattr(fake_mt5, "symbol_info_tick", lambda s: Record(bid=1.085, ask=1.0851))
    monkeypatch.setattr(
        fake_mt5, "symbol_info", lambda s: Record(filling_mode=fake_mt5.ORDER_FILLING_FOK)
    )


def test_partial_close_over_volume_rejected(monkeypatch):
    sent = _capture_order_send(monkeypatch)
    _open_position(monkeypatch, volume=0.02)
    result = positions.close_position(99, lot_size=0.05)
    assert not result["success"]
    assert "exceeds position volume" in result["error"]
    assert sent == []


def test_partial_close_within_volume_ok(monkeypatch):
    sent = _capture_order_send(monkeypatch)
    _open_position(monkeypatch, volume=0.02)
    result = positions.close_position(99, lot_size=0.01)
    assert result["success"]
    assert sent[0]["volume"] == 0.01
    assert sent[0]["position"] == 99
    assert sent[0]["type"] == fake_mt5.ORDER_TYPE_SELL  # closes a long


def test_modify_position_keeps_existing_sl(monkeypatch):
    sent = _capture_order_send(monkeypatch)
    _open_position(monkeypatch)
    result = positions.modify_position(99, take_profit=1.12)
    assert result["success"]
    assert sent[0]["sl"] == 1.07   # untouched
    assert sent[0]["tp"] == 1.12


# ---------------------------------------------------------------------------
# Timeout → suspend → orphan marker → clear_suspend
# ---------------------------------------------------------------------------

def test_write_timeout_records_orphan_and_suspends(monkeypatch):
    mt5client._config = dict(TEST_CONFIG, default_timeout_sec=0.05)

    def slow_order_send(request):
        time.sleep(0.5)
        return Record(retcode=fake_mt5.TRADE_RETCODE_DONE)

    with pytest.raises(mt5client.MT5Error, match="MAY STILL HAVE EXECUTED"):
        mt5client.run_mt5_write(
            slow_order_send, {"symbol": "EURUSD"}, context="place_order", symbol="EURUSD",
        )

    assert mt5client.is_suspended()
    marker = mt5client.last_write_timeout()
    assert marker["context"] == "place_order"
    assert marker["symbol"] == "EURUSD"

    # All subsequent calls are refused while suspended
    with pytest.raises(mt5client.MT5Error, match="suspended"):
        mt5client.ensure_initialized()

    status = mt5client.gateway_status()
    assert status["suspended"] is True
    assert status["last_write_timeout"]["context"] == "place_order"

    cleared = mt5client.clear_suspend()
    assert cleared["was_suspended"] is True
    assert cleared["last_write_timeout"]["context"] == "place_order"
    assert not mt5client.is_suspended()
    assert mt5client.last_write_timeout() is None
    mt5client.ensure_initialized()  # healthy again


def test_read_timeout_suspends_without_orphan_marker(monkeypatch):
    mt5client._config = dict(TEST_CONFIG, default_timeout_sec=0.05)

    def slow_read():
        time.sleep(0.5)

    with pytest.raises(mt5client.MT5Error, match="timed out"):
        mt5client.run_mt5(slow_read)
    assert mt5client.is_suspended()
    assert mt5client.last_write_timeout() is None


def test_gateway_status_reports_allow_real_account():
    status = mt5client.gateway_status()
    assert status["success"]
    assert status["allow_real_account"] is False
    assert status["suspended"] is False


# ---------------------------------------------------------------------------
# get_tick None-info guard
# ---------------------------------------------------------------------------

def test_get_tick_survives_missing_symbol_info(monkeypatch):
    monkeypatch.setattr(
        fake_mt5, "symbol_info_tick",
        lambda s: Record(bid=1.08, ask=1.0801, time=1718000000),
    )
    monkeypatch.setattr(fake_mt5, "symbol_info", lambda s: None)
    result = symbols.get_tick("EURUSD")
    assert result["success"]
    assert result["spread_points"] is None
    assert result["tick"]["bid"] == 1.08

"""
Seven gateway tests for MCP Inspector validation.

Run on Windows with a live MT5 terminal and populated config.json:

    python tests/gateway_tests.py

Or via MCP Inspector:

    npx @modelcontextprotocol/inspector python server.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handlers import account, orders, positions, symbols  # noqa: E402
from mt5client import MT5Error, clear_suspend, ensure_initialized  # noqa: E402

TEST_SYMBOL = "EURUSD"


def _assert_success(name: str, result: dict) -> None:
    if not result.get("success", False):
        raise AssertionError(f"{name} failed: {json.dumps(result, indent=2)}")


def test_01_health_check() -> None:
    ensure_initialized(health_check=True)
    result = account.get_account_info()
    _assert_success("health_check/account", result)
    print("PASS 01 health_check")


def test_02_get_account_info() -> None:
    result = account.get_account_info()
    _assert_success("get_account_info", result)
    assert "balance" in result["summary"]
    print("PASS 02 get_account_info")


def test_03_get_symbols() -> None:
    result = symbols.get_symbols()
    _assert_success("get_symbols", result)
    assert result["count"] > 0
    print(f"PASS 03 get_symbols ({result['count']} symbols)")


def test_04_get_symbol_info() -> None:
    result = symbols.get_symbol_info(TEST_SYMBOL)
    _assert_success("get_symbol_info", result)
    assert result["symbol"] == TEST_SYMBOL
    print("PASS 04 get_symbol_info")


def test_05_get_rates() -> None:
    result = symbols.get_rates(TEST_SYMBOL, "H1", count=10)
    _assert_success("get_rates", result)
    assert result["count"] > 0
    print(f"PASS 05 get_rates ({result['count']} bars)")


def test_06_get_tick() -> None:
    result = symbols.get_tick(TEST_SYMBOL)
    _assert_success("get_tick", result)
    assert result["tick"]["bid"] > 0
    assert result["tick"]["ask"] > 0
    print(f"PASS 06 get_tick (spread={result['spread']:.5f})")


def test_07_read_positions_and_orders() -> None:
    positions_result = positions.get_open_positions()
    orders_result = orders.get_pending_orders()
    _assert_success("get_open_positions", positions_result)
    _assert_success("get_pending_orders", orders_result)
    print(
        "PASS 07 get_open_positions + get_pending_orders "
        f"(positions={positions_result['count']}, orders={orders_result['count']})"
    )


TESTS = [
    test_01_health_check,
    test_02_get_account_info,
    test_03_get_symbols,
    test_04_get_symbol_info,
    test_05_get_rates,
    test_06_get_tick,
    test_07_read_positions_and_orders,
]


def main() -> int:
    clear_suspend()
    passed = 0
    for test in TESTS:
        try:
            test()
            passed += 1
        except (AssertionError, MT5Error) as exc:
            print(f"FAIL {test.__name__}: {exc}")
            break

    print(f"\n{passed}/{len(TESTS)} gateway tests passed")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

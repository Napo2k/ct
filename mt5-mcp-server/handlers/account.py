"""Account information handlers."""

from __future__ import annotations

from typing import Any

import MetaTrader5 as mt5

from mt5client import named_tuple_to_dict, run_mt5


def get_account_info() -> dict[str, Any]:
    """Return live account balance, equity, margin, and trading permissions."""
    account = run_mt5(mt5.account_info)
    if account is None:
        return {"success": False, "error": "account_info returned None"}

    data = named_tuple_to_dict(account)
    return {
        "success": True,
        "account": data,
        "summary": {
            "login": data["login"],
            "balance": data["balance"],
            "equity": data["equity"],
            "margin": data["margin"],
            "free_margin": data["margin_free"],
            "margin_level": data["margin_level"],
            "profit": data["profit"],
            "currency": data["currency"],
            "leverage": data["leverage"],
            "trade_allowed": data["trade_allowed"],
        },
    }

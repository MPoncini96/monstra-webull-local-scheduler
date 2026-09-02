#!/usr/bin/env python3
"""
Monstra -> Webull portfolio rebalancer (local scheduler edition).

What this does:
  1. Pulls your target holdings (symbol -> weight) from your Monstra portfolio
     via GET /api/v1/portfolio/latest.
  2. Reads your current Webull account value and positions via the official
     Webull OpenAPI (webull-openapi-python-sdk).
  3. Computes the buy/sell orders needed to move your Webull account toward
     the Monstra target weights.
  4. Prints (and logs) the plan and -- only after you set DRY_RUN=false and
     type EXECUTE at an interactive prompt -- submits market orders to
     Webull.

This package is meant to run on YOUR machine (or a machine you control), on a
schedule you set up yourself with Windows Task Scheduler, macOS launchd, or
cron -- the same shape as the Schwab local-scheduler package, and for the
same reason: Webull's OpenAPI ties a new app_key/app_secret pair to your
account through a one-time approval step (the SDK calls it out as a "2FA
token"), and the resulting token is cached to a local file that has to
survive between runs. That's a natural fit for your own machine's disk and a
local scheduler, and a bad fit for a hosted "serverless" cron service that
doesn't keep a persistent disk.

Setup (one time):
  1. Get a Monstra API key at https://www.monstra.bot/dashboard/api and set
     MONSTRA_API_KEY.
  2. Apply for OpenAPI access at https://developer.webull.com (Getting
     Started -> generate an App Key / App Secret). Production approval is a
     manual review that Webull says typically takes 1-2 business days; the
     sandbox environment works immediately with Webull's shared test
     accounts, so start there. Set WEBULL_APP_KEY / WEBULL_APP_SECRET.
  3. Install the official SDK: pip install -r requirements.txt
  4. Run once, interactively, from a terminal, WITHOUT setting
     WEBULL_ACCOUNT_ID, to list your accounts:
       python main.py
     This prints each account's id, type, and label. Copy the account_id you
     want this script to trade into WEBULL_ACCOUNT_ID, then run again.
  5. The first real run may need you to approve the new app/device from your
     Webull app (the SDK polls for that approval for a few minutes). Once
     approved, a token is cached locally and reused on later runs -- including
     unattended scheduled ones -- until it expires.
  6. Once step 4/5 succeeds without needing approval again, wire the script
     up to Task Scheduler / launchd / cron (see README.md).

Safety:
  - WEBULL_ENV defaults to "sandbox" -- it will not touch a real account
    until you explicitly set WEBULL_ENV=production.
  - DRY_RUN defaults to true. No orders are ever sent until you set it to
    false AND type EXECUTE at the confirmation prompt (which only a real
    human running this interactively can do -- an unattended scheduled run
    with DRY_RUN=false will just print the plan and log a warning that it
    needs interactive confirmation).
  - Orders are whole-share MARKET/DAY orders only.
  - Sells are capped at your current share count -- this script never opens
    a short position.
  - Trades below MIN_TRADE_DOLLARS are skipped to avoid dust orders.

This script places real trades with real money once WEBULL_ENV=production,
DRY_RUN=false, and you confirm. Review the printed plan carefully.
"""

import logging
import math
import os
import sys
import urllib.error
import urllib.request
import json
import uuid
from pathlib import Path

try:
    from webull.core.client import ApiClient
    from webull.data.common.category import Category
    from webull.data.data_client import DataClient
    from webull.trade.trade_client import TradeClient
except ImportError:
    sys.exit(
        "The official Webull OpenAPI SDK is required: pip install -r requirements.txt "
        "(package name: webull-openapi-python-sdk)"
    )

# ------------------------------------------------------------------
# 1. CONFIG -- env vars win; edit the fallback constants if you'd rather
#    not use environment variables at all.
# ------------------------------------------------------------------

MONSTRA_API_KEY = os.environ.get("MONSTRA_API_KEY", "PASTE_YOUR_MONSTRA_API_KEY_HERE")
MONSTRA_API_BASE_URL = os.environ.get("MONSTRA_API_BASE_URL", "https://www.monstra.bot")

WEBULL_APP_KEY = os.environ.get("WEBULL_APP_KEY", "PASTE_YOUR_WEBULL_APP_KEY_HERE")
WEBULL_APP_SECRET = os.environ.get("WEBULL_APP_SECRET", "PASTE_YOUR_WEBULL_APP_SECRET_HERE")
WEBULL_ACCOUNT_ID = os.environ.get("WEBULL_ACCOUNT_ID", "")  # leave blank to list accounts and exit
WEBULL_ENV = os.environ.get("WEBULL_ENV", "sandbox").strip().lower()  # "sandbox" or "production"
WEBULL_TOKEN_DIR = os.environ.get("WEBULL_OPENAPI_TOKEN_DIR")  # SDK also reads this env var itself

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() not in ("false", "0", "no")
MIN_TRADE_DOLLARS = float(os.environ.get("MIN_TRADE_DOLLARS", "25"))
LOG_FILE_PATH = Path(os.environ.get("WEBULL_LOG_FILE_PATH", str(Path(__file__).with_name("webull_rebalance.log"))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("webull_rebalance")

# ------------------------------------------------------------------
# 2. Monstra: fetch target holdings (stdlib only)
# ------------------------------------------------------------------


def _monstra_get(path):
    req = urllib.request.Request(
        f"{MONSTRA_API_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {MONSTRA_API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw}
        return err.code, payload


def fetch_monstra_holdings():
    if "PASTE_YOUR" in MONSTRA_API_KEY:
        sys.exit("Set MONSTRA_API_KEY (env var or the constant at the top) first.")

    status, payload = _monstra_get("/api/v1/portfolio/latest")
    if status != 200:
        sys.exit(f"Monstra portfolio fetch failed ({status}): {payload}")

    holdings = (payload.get("data") or {}).get("holdings") or {}
    if not holdings:
        sys.exit("Monstra returned no holdings for this account -- nothing to rebalance.")
    return holdings  # { "AAPL": 0.25, "MSFT": 0.10, ... } fractions summing to ~1


# ------------------------------------------------------------------
# 3. Webull: client setup
# ------------------------------------------------------------------


def build_clients():
    if "PASTE_YOUR" in WEBULL_APP_KEY or "PASTE_YOUR" in WEBULL_APP_SECRET:
        sys.exit("Set WEBULL_APP_KEY / WEBULL_APP_SECRET (env vars or the constants at the top) first.")

    api_client = ApiClient(WEBULL_APP_KEY, WEBULL_APP_SECRET, "us")
    if WEBULL_ENV == "sandbox":
        api_client.add_endpoint("us", "api.sandbox.webull.com")
    elif WEBULL_ENV != "production":
        sys.exit(f"WEBULL_ENV must be 'sandbox' or 'production', got: {WEBULL_ENV!r}")

    if WEBULL_TOKEN_DIR:
        api_client.set_token_dir(WEBULL_TOKEN_DIR)

    return TradeClient(api_client), DataClient(api_client)


def list_accounts_and_exit(trade_client):
    res = trade_client.account_v2.get_account_list()
    if res.status_code != 200:
        sys.exit(f"Could not list Webull accounts ({res.status_code}): {res.text}")

    rows = res.json() or []
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("accounts") or []

    if not rows:
        sys.exit("Webull returned no accounts for this app key.")

    print("\nWEBULL_ACCOUNT_ID is not set. Accounts available to this app key:\n")
    for row in rows:
        print(
            f"  account_id={row.get('account_id')}  type={row.get('account_type')}  "
            f"label={row.get('account_label')}  number={row.get('account_number')}"
        )
    sys.exit("\nSet WEBULL_ACCOUNT_ID to the account_id you want this script to trade, then run again.")


# ------------------------------------------------------------------
# 4. Webull: account snapshot, prices, orders
# ------------------------------------------------------------------


def get_account_snapshot(trade_client, account_id):
    res = trade_client.account_v2.get_account_balance(account_id)
    if res.status_code != 200:
        sys.exit(f"Could not load Webull account balance ({res.status_code}): {res.text}")
    balance = res.json() or {}
    total_value = float(balance.get("total_net_liquidation_value") or 0)
    if total_value <= 0:
        sys.exit(f"Webull returned no usable account value: {balance}")

    res = trade_client.account_v2.get_account_position(account_id)
    if res.status_code != 200:
        sys.exit(f"Could not load Webull positions ({res.status_code}): {res.text}")
    payload = res.json() or []
    rows = payload if isinstance(payload, list) else (payload.get("data") or payload.get("positions") or [])

    positions = {}
    for row in rows:
        if row.get("instrument_type") and row.get("instrument_type") != "EQUITY":
            continue
        symbol = row.get("symbol")
        if symbol:
            positions[symbol] = float(row.get("quantity") or 0)

    return total_value, positions


def get_last_prices(data_client, symbols):
    if not symbols:
        return {}
    res = data_client.market_data.get_snapshot(sorted(symbols), Category.US_STOCK.name)
    if res.status_code != 200:
        sys.exit(f"Could not fetch Webull quotes ({res.status_code}): {res.text}")

    payload = res.json() or []
    rows = payload if isinstance(payload, list) else (payload.get("data") or [])

    prices = {}
    for row in rows:
        symbol = row.get("symbol")
        price = row.get("price")
        if symbol and price:
            prices[symbol] = float(price)
    return prices


def build_trade_plan(holdings, total_value, positions, prices):
    symbols = set(holdings) | set(positions)
    plan = []
    for symbol in sorted(symbols):
        price = prices.get(symbol)
        if not price:
            log.info("  skipping %s: no live price available", symbol)
            continue

        target_dollars = holdings.get(symbol, 0.0) * total_value
        target_shares = math.floor(target_dollars / price)
        current_shares = int(positions.get(symbol, 0))
        delta_shares = target_shares - current_shares

        if delta_shares > 0:
            side, quantity = "BUY", delta_shares
        elif delta_shares < 0:
            # Never sells more than is currently held -- this script does not short.
            side, quantity = "SELL", min(-delta_shares, current_shares)
        else:
            continue

        if quantity <= 0 or quantity * price < MIN_TRADE_DOLLARS:
            continue

        plan.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "current_shares": current_shares,
                "target_shares": target_shares,
            }
        )
    return plan


def place_order(trade_client, account_id, trade):
    order = {
        "combo_type": "NORMAL",
        "client_order_id": uuid.uuid4().hex,
        "symbol": trade["symbol"],
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "entrust_type": "QTY",
        "support_trading_session": "CORE",
        "time_in_force": "DAY",
        "side": trade["side"],
        "quantity": str(trade["quantity"]),
    }
    res = trade_client.order_v3.place_order(account_id, [order])
    if res.status_code != 200:
        log.error("  FAILED %s %s %s: (%s) %s", trade["side"], trade["quantity"], trade["symbol"], res.status_code, res.text)
    else:
        log.info("  submitted %s %s %s", trade["side"], trade["quantity"], trade["symbol"])


# ------------------------------------------------------------------
# 5. Main
# ------------------------------------------------------------------


def main():
    log.info("=== Webull rebalance run starting (env=%s, DRY_RUN=%s) ===", WEBULL_ENV, DRY_RUN)

    trade_client, data_client = build_clients()

    if not WEBULL_ACCOUNT_ID:
        list_accounts_and_exit(trade_client)

    log.info("Fetching target weights from Monstra...")
    holdings = fetch_monstra_holdings()

    log.info("Reading Webull account snapshot...")
    total_value, positions = get_account_snapshot(trade_client, WEBULL_ACCOUNT_ID)
    prices = get_last_prices(data_client, set(holdings) | set(positions))

    plan = build_trade_plan(holdings, total_value, positions, prices)
    if not plan:
        log.info("Account already matches target weights (within MIN_TRADE_DOLLARS). Nothing to do.")
        return

    log.info("Account value: $%.2f", total_value)
    log.info("Proposed trades:")
    for trade in plan:
        log.info(
            "  %-4s %6d %-8s @ ~$%.2f  (%s -> %s shares)",
            trade["side"],
            trade["quantity"],
            trade["symbol"],
            trade["price"],
            trade["current_shares"],
            trade["target_shares"],
        )

    if DRY_RUN:
        log.info("DRY_RUN is true -- no orders sent. Set DRY_RUN=false to enable live trading.")
        return

    if not sys.stdin.isatty():
        log.warning(
            "DRY_RUN is false but this run is non-interactive, so there is no one to type "
            "EXECUTE at a prompt. No orders were sent. Run this manually from a terminal to "
            "confirm and place trades."
        )
        return

    confirm = input("\nType EXECUTE to submit these live market orders to Webull: ").strip()
    if confirm != "EXECUTE":
        log.info("Not confirmed -- no orders sent.")
        return

    log.info("Submitting orders...")
    for trade in plan:
        place_order(trade_client, WEBULL_ACCOUNT_ID, trade)


if __name__ == "__main__":
    main()

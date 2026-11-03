"""
RSM434 -- ALGO2e market-making primer, DMA REST API (Fall 2026)

The same program as "ALGO2e rev PY Script - In-Class.py", carrying both layers
of commentary in one file:

  * Block comments marked "WHAT IT IS FOR" explain the trading logic -- what
    each piece is trying to achieve, and where it is weak.
  * Inline comments explain the Python itself, line by line.

How to run
  1. Install the requests package, once:     pip install requests
  2. Fill in your trader ID and password under "settings" below.
  3. While the ALGO2e practice case is running, run this file -- in VS Code,
     Run > Run Without Debugging; or in a terminal:
         python "RSM434 ALGO2e Primer - DMA.py"
  4. It trades until the case stops. Press Ctrl+C to stop it early.

This is a starting point, not a finished strategy. It uses each security's
maker fee and visible top-of-book depth to size orders, and manages the case's
gross and net share limits across all three securities.
"""

# ---------------------------------------------------------------------------
# A note on indentation, if you are new to Python
#
# Indentation in Python is functional: it is an instruction to Python, not a
# matter of style. Functions, if statements and loops are all defined by how
# far their lines are indented (other languages, such as VBA or R, use explicit
# words or brackets instead).
#
# Every line that starts at the left margin runs when you run the file. Lines
# indented under a def, if or while belong to that clause. For example:
#
#   this level could define a function
#       this level is inside the function
#       this level is inside the function, and starts if statement A
#           this level is inside if statement A, inside the function
#       this level is inside the function again -- moving back to the left
#           ends if statement A
#
#   this level could define another function, and ends the previous one
# ---------------------------------------------------------------------------

import base64
from time import sleep     # SLEEP pauses the program for a number of seconds

import requests            # REQUESTS sends the HTTP messages RIT listens for

# ---------------------------------------------------------------- settings
# The ALGO2e practice case on flserver. DMA talks to the RIT server directly,
# so the RIT Client does not need to be running. Fill in your own trader ID
# and password.
BASE_URL = 'http://flserver.rotman.utoronto.ca:16655/v1'
USERNAME = 'studentid'
PASSWORD = 'pass'

# ---------------------------------------------------------------- connection
# Session() keeps one connection open for every message. Without it Python
# would open and close a socket for each request, which causes errors when the
# next request starts before the last connection has finished closing.
# DMA signs in with HTTP Basic authentication: "Basic " + base64("id:password")
credentials = base64.b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()
s = requests.Session()
s.headers.update({'Authorization': 'Basic ' + credentials})


# ---------------------------------------------------------------- rate limit
def api(s, method, path, params=None):
    """Send one request to RIT, waiting and retrying if we are going too fast.

    WHAT IT IS FOR: RIT limits how many requests you may send each second (the
    DMA server allows 80 per endpoint). Go faster and it rejects the request
    with HTTP 429 and says how long to wait, in the Retry-After header and as
    "wait" in the body. We wait exactly that long and send the same request
    again, so a throttled order is delayed rather than lost. The in-class
    script calls s.get() and s.post() directly and has no such protection.
    """
    while True:
        resp = s.request(method, f'{BASE_URL}/{path}', params=params)
        if resp.status_code != 429:
            return resp
        try:
            wait = float(resp.headers.get('Retry-After') or resp.json()['wait'])
        except (ValueError, KeyError, TypeError):
            wait = 0.1                  # no wait time given: pause briefly
        sleep(wait)


# ---------------------------------------------------------------- settings
# The brief caps each order at 5,000 shares and gross/net positions at 25,000.
# The passive trade fee is a rebate when positive and a fee when negative.
TICKERS = ('CNR', 'RY', 'AC')
MAX_ORDER_SIZE = 5_000
MAX_GROSS_POSITION = 25_000
MAX_NET_POSITION = 25_000
PASSIVE_REBATE = {'CNR': 0.0023, 'RY': -0.0020, 'AC': 0.0011}


# ---------------------------------------------------------------- helpers
def get_tick():
    # WHAT IT IS FOR: asks RIT for the state of the case -- "ACTIVE", "STOPPED"
    # or "PAUSED" -- which the while loop uses to decide whether to keep
    # trading. It also returns the tick, which this code does not use but which
    # is useful if you extend the algo, for example to stop trading near the
    # close.
    resp = api(s, 'GET', 'case')
    if not resp.ok:
        raise RuntimeError(
            f'Could not retrieve case status (HTTP {resp.status_code}): {resp.text}'
        )
    case = resp.json()
    return case['tick'], case['status']


def get_order_book(ticker):
    # WHAT IT IS FOR: reads the best prices and displayed size. Order size is
    # based on this depth, so it responds to the liquidity currently available.
    payload = {'ticker': ticker}      # a dictionary: the key 'ticker' with the value passed in
    resp = api(s, 'GET', 'securities/book', payload)
    if not resp.ok:
        raise RuntimeError(
            f'Could not retrieve {ticker} order book '
            f'(HTTP {resp.status_code}): {resp.text}'
        )
    book = resp.json()
    bids = book['bids']
    asks = book['asks']
    if not bids or not asks:
        raise RuntimeError(f'{ticker} order book is missing a bid or ask.')
    return bids[0], asks[0]


def get_positions():
    # WHAT IT IS FOR: gross and net limits apply across the whole portfolio,
    # not independently to each ticker.
    resp = api(s, 'GET', 'securities')
    if not resp.ok:
        raise RuntimeError(
            f'Could not retrieve positions (HTTP {resp.status_code}): {resp.text}'
        )
    securities = resp.json()
    positions = {}
    for security in securities:
        if security['ticker'] in TICKERS:
            positions[security['ticker']] = int(security['position'])
    missing_tickers = set(TICKERS) - positions.keys()
    if missing_tickers:
        raise RuntimeError(
            f'Position data is missing: {", ".join(sorted(missing_tickers))}.'
        )
    return positions


def get_open_orders():
    # WHAT IT IS FOR: unfilled orders can still increase exposure, so include
    # their remaining quantity when checking the portfolio limits.
    resp = api(s, 'GET', 'orders', {'status': 'OPEN'})
    if not resp.ok:
        raise RuntimeError(
            f'Could not retrieve open orders '
            f'(HTTP {resp.status_code}): {resp.text}'
        )
    return resp.json()


def remaining_quantity(order):
    return max(0, int(order['quantity'] - order.get('quantity_filled', 0)))


def projected_gross(positions, open_orders, ticker, action, quantity):
    pending = {symbol: {'BUY': 0, 'SELL': 0} for symbol in TICKERS}
    for order in open_orders:
        symbol = order['ticker']
        if symbol in pending:
            pending[symbol][order['action']] += remaining_quantity(order)

    gross = 0
    for symbol in TICKERS:
        low = positions[symbol] - pending[symbol]['SELL']
        high = positions[symbol] + pending[symbol]['BUY']
        if symbol == ticker:
            if action == 'BUY':
                high += quantity
            else:
                low -= quantity
        gross += max(abs(low), abs(high))
    return gross


def calculate_order_quantity(ticker, action, level, spread, positions, open_orders):
    # WHAT IT IS FOR: size orders to both the market and our remaining risk
    # capacity. The visible queue sets the starting size; poor economics or
    # limited portfolio headroom reduce it further.
    if spread <= 0:
        return 0

    round_trip_edge = spread + 2 * PASSIVE_REBATE[ticker]
    if round_trip_edge <= 0:
        return 0

    edge_fraction = min(1.0, round_trip_edge / spread)
    quantity = min(
        MAX_ORDER_SIZE,
        int(level['quantity'] * edge_fraction),
    )
    if level['quantity'] > 0 and quantity == 0:
        quantity = 1
    if quantity == 0:
        return 0

    pending_buys = sum(
        remaining_quantity(order) for order in open_orders
        if order['action'] == 'BUY'
    )
    pending_sells = sum(
        remaining_quantity(order) for order in open_orders
        if order['action'] == 'SELL'
    )
    net_low = sum(positions.values()) - pending_sells
    net_high = sum(positions.values()) + pending_buys
    if action == 'BUY':
        quantity = min(quantity, MAX_NET_POSITION - net_high)
    else:
        quantity = min(quantity, MAX_NET_POSITION + net_low)
    quantity = max(0, quantity)

    if quantity == 0 or projected_gross(
        positions, open_orders, ticker, action, 0
    ) > MAX_GROSS_POSITION:
        return 0

    low, high = 0, quantity
    while low < high:
        candidate = (low + high + 1) // 2
        if projected_gross(
            positions, open_orders, ticker, action, candidate
        ) <= MAX_GROSS_POSITION:
            low = candidate
        else:
            high = candidate - 1
    return low


def cancel_orders(ticker):
    resp = api(s, 'POST', 'commands/cancel', {'ticker': ticker})
    if not resp.ok:
        raise RuntimeError(
            f'Could not cancel {ticker} orders '
            f'(HTTP {resp.status_code}): {resp.text}'
        )


# ---------------------------------------------------------------- algorithm
def main():
    # WHAT IT IS FOR: quote the profitable securities using market-specific
    # prices, fee economics, liquidity, and portfolio risk capacity.
    _, status = get_tick()

    while status != 'STOPPED':
        if status != 'ACTIVE':
            sleep(0.5)
            _, status = get_tick()
            continue

        for ticker_symbol in TICKERS:
            # Clear stale quotes before reading current positions and risk.
            cancel_orders(ticker_symbol)
            positions = get_positions()
            open_orders = get_open_orders()
            bid, ask = get_order_book(ticker_symbol)
            spread = ask['price'] - bid['price']

            for action, level in (('BUY', bid), ('SELL', ask)):
                quantity = calculate_order_quantity(
                    ticker_symbol, action, level, spread, positions, open_orders
                )
                if quantity == 0:
                    continue

                resp = api(s, 'POST', 'orders', {
                    'ticker': ticker_symbol,
                    'type': 'LIMIT',
                    'quantity': quantity,
                    'price': level['price'],
                    'action': action,
                })
                if not resp.ok:
                    raise RuntimeError(
                        f'Could not place {action} order for {ticker_symbol} '
                        f'(HTTP {resp.status_code}): {resp.text}'
                    )
                order = resp.json()
                filled = int(order.get('quantity_filled', 0))
                positions[ticker_symbol] += filled if action == 'BUY' else -filled
                if order.get('status') == 'OPEN':
                    open_orders.append(order)

            # Pause to give those passive orders a chance to trade before they
            # are cancelled. This short interval also limits unnecessary API
            # traffic; the next pass reads fresh market and position data.
            sleep(0.5)

            cancel_orders(ticker_symbol)

        _, status = get_tick()


if __name__ == '__main__':   # true when you run this file directly
    try:
        main()               # so pressing Run (or F5) starts the algo
    except KeyboardInterrupt:
        print('Stopped with Ctrl+C.')

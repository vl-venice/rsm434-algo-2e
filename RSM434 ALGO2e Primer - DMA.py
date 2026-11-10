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

The strategy widens quotes when prices move quickly, skews them against
inventory, sizes them to visible depth and maker economics, and checks the
case's gross and net share limits across all three securities.
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
from math import ceil
from time import monotonic, sleep  # SLEEP pauses the program for a number of seconds

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

    WHAT IT IS FOR: RIT applies global and security-specific request limits.
    When a request is rejected with HTTP 429, the response says how long to
    wait in the Retry-After header or "wait" body field. We wait and retry
    rather than treating a throttled request as successful.
    """
    while True:
        started = monotonic()
        resp = s.request(
            method,
            f'{BASE_URL}/{path}',
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        elapsed = monotonic() - started
        previous_latency = API_LATENCIES.get(path)
        API_LATENCIES[path] = (
            elapsed if previous_latency is None
            else 0.75 * previous_latency + 0.25 * elapsed
        )
        if resp.status_code != 429:
            return resp
        try:
            wait = float(resp.headers.get('Retry-After') or resp.json()['wait'])
        except (ValueError, KeyError, TypeError):
            wait = 0.1                  # no wait time given: pause briefly
        sleep(max(0.05, wait))


# ---------------------------------------------------------------- settings
# The brief caps each order at 5,000 shares and gross/net positions at 25,000.
# The passive trade fee is a rebate when positive and a fee when negative.
TICKERS = ('CNR', 'RY', 'AC')
MAX_ORDER_SIZE = 5_000
MAX_GROSS_POSITION = 25_000
MAX_NET_POSITION = 25_000
PASSIVE_REBATE = {'CNR': 0.0023, 'RY': -0.0020, 'AC': 0.0011}
PRICE_TICK = 0.01
MAX_VOLATILITY_BUFFER_TICKS = 3
# Add one protective tick for each 250 ms of observed API latency.
LATENCY_BUFFER_SECONDS_PER_TICK = 0.25
REQUEST_TIMEOUT_SECONDS = 3
API_LATENCIES = {}


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
    # The DMA Swagger uses singular names; the course's REST example uses the
    # plural names. Accept either documented response shape.
    bids = book.get('bid', book.get('bids'))
    asks = book.get('ask', book.get('asks'))
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


def calculate_order_quantity(
    ticker, action, level, spread, positions, open_orders, volatility_ticks=0
):
    # WHAT IT IS FOR: size orders to both the market and our remaining risk
    # capacity. The visible queue sets the starting size; poor economics or
    # limited portfolio headroom reduce it further.
    if spread <= 0:
        return 0

    round_trip_edge = spread + 2 * PASSIVE_REBATE[ticker]
    if round_trip_edge <= 0:
        return 0

    edge_fraction = min(1.0, round_trip_edge / spread)
    position = positions[ticker]
    adds_to_inventory = (
        action == 'BUY' and position >= 0
    ) or (
        action == 'SELL' and position <= 0
    )
    inventory_fraction = min(1.0, abs(position) / MAX_NET_POSITION)
    inventory_factor = (
        max(0.2, 1.0 - 0.8 * inventory_fraction)
        if adds_to_inventory else 1.0
    )
    volatility_factor = 1.0 / (1.0 + 0.5 * volatility_ticks)
    quantity = min(
        MAX_ORDER_SIZE,
        int(
            level['quantity']
            * edge_fraction
            * inventory_factor
            * volatility_factor
        ),
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


def observe_mid(ticker, bid, ask, previous_mids):
    """Track the last mid for this ticker and return its recent move."""
    mid = (float(bid['price']) + float(ask['price'])) / 2
    previous_mid = previous_mids.get(ticker)
    previous_mids[ticker] = mid
    return mid, 0.0 if previous_mid is None else mid - previous_mid


def calculate_quote_prices(
    bid, ask, position, recent_mid_change, request_latency=0.0
):
    """Widen quotes when prices move quickly and skew them to reduce inventory."""
    best_bid = float(bid['price'])
    best_ask = float(ask['price'])
    if best_ask <= best_bid:
        return None

    movement_ticks = ceil(abs(recent_mid_change) / PRICE_TICK)
    latency_ticks = ceil(request_latency / LATENCY_BUFFER_SECONDS_PER_TICK)
    volatility_ticks = min(
        MAX_VOLATILITY_BUFFER_TICKS, max(movement_ticks, latency_ticks)
    )
    spread_ticks = max(1, round((best_ask - best_bid) / PRICE_TICK))
    inventory_shift = round(
        (position / MAX_NET_POSITION) * max(1, spread_ticks // 2)
    )
    trend_shift = round(recent_mid_change / (2 * PRICE_TICK))
    center_shift = trend_shift - inventory_shift

    quote_bid = best_bid + (center_shift - volatility_ticks) * PRICE_TICK
    quote_ask = best_ask + (center_shift + volatility_ticks) * PRICE_TICK
    quote_bid = min(quote_bid, best_ask - PRICE_TICK)
    quote_ask = max(quote_ask, best_bid + PRICE_TICK)
    return round(quote_bid, 2), round(quote_ask, 2), volatility_ticks


def cancel_orders(ticker):
    resp = api(s, 'POST', 'commands/cancel', {'ticker': ticker})
    if not resp.ok:
        raise RuntimeError(
            f'Could not cancel {ticker} orders '
            f'(HTTP {resp.status_code}): {resp.text}'
        )


def cancel_all_orders():
    resp = api(s, 'POST', 'commands/cancel', {'all': 1})
    if not resp.ok:
        raise RuntimeError(
            f'Could not cancel all open orders '
            f'(HTTP {resp.status_code}): {resp.text}'
        )


# ---------------------------------------------------------------- algorithm
def main():
    # WHAT IT IS FOR: quote the profitable securities using current market
    # conditions, fee economics, inventory, and portfolio risk capacity.
    _, status = get_tick()
    if status == 'ACTIVE':
        cancel_all_orders()
    previous_mids = {}
    was_active = status == 'ACTIVE'

    while status != 'STOPPED':
        if status != 'ACTIVE':
            if was_active:
                cancel_all_orders()
            was_active = False
            sleep(0.2)
            _, status = get_tick()
            continue

        was_active = True
        for ticker_symbol in TICKERS:
            _, status = get_tick()
            if status != 'ACTIVE':
                break

            # Cancel this ticker's previous quotes before reading new conditions.
            cancel_orders(ticker_symbol)
            positions = get_positions()
            open_orders = get_open_orders()
            if any(order['ticker'] == ticker_symbol for order in open_orders):
                continue

            bid, ask = get_order_book(ticker_symbol)
            _, recent_mid_change = observe_mid(
                ticker_symbol, bid, ask, previous_mids
            )
            quotes = calculate_quote_prices(
                bid,
                ask,
                positions[ticker_symbol],
                recent_mid_change,
                max(
                    API_LATENCIES.get('securities/book', 0.0),
                    API_LATENCIES.get('orders', 0.0),
                ),
            )
            if quotes is None:
                continue
            bid_price, ask_price, volatility_ticks = quotes
            spread = ask_price - bid_price

            for action, level, price in (
                ('BUY', bid, bid_price),
                ('SELL', ask, ask_price),
            ):
                quantity = calculate_order_quantity(
                    ticker_symbol,
                    action,
                    level,
                    spread,
                    positions,
                    open_orders,
                    volatility_ticks,
                )
                if quantity == 0:
                    continue

                resp = api(s, 'POST', 'orders', {
                    'ticker': ticker_symbol,
                    'type': 'LIMIT',
                    'quantity': quantity,
                    'price': price,
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
                if order.get('status') == 'OPEN' and remaining_quantity(order) > 0:
                    open_orders.append(order)

        if status == 'ACTIVE':
            _, status = get_tick()


if __name__ == '__main__':   # true when you run this file directly
    try:
        main()               # so pressing Run (or F5) starts the algo
    except KeyboardInterrupt:
        print('Stopped with Ctrl+C.')

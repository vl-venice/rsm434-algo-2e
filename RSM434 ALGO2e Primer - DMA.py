"""
RSM434 -- ALGO2e market-making primer, DMA REST API (Fall 2026)

The same program as "ALGO2e rev PY Script - In-Class.py", carrying both layers
of commentary in one file:

  * Block comments marked "WHAT IT IS FOR" explain the trading logic -- what
    each piece is trying to achieve, and where it is weak.
  * Inline comments explain the Python itself, line by line.

How to run
  1. Fill in your trader ID and password under "settings" below.
  2. While the ALGO2e practice case is running, run this file -- in VS Code,
     Run > Run Without Debugging; or in a terminal:
         python "RSM434 ALGO2e Primer - DMA.py"
  3. It trades until the case stops. Press Ctrl+C to stop it early.

No extra packages are required: the HTTP calls use Python's built-in
http.client, so this runs on macOS without installing requests or urllib3.

The strategy posts passive quotes where the spread plus the maker rebate
clears one tick of adverse selection, improves inside wide spreads, and
keeps a resting order when the quote has not changed. CNR and AC can be
quoted on a one-tick market; RY charges a maker fee, so it is quoted only
when the spread is wide enough to pay for that. Inventory is skewed and
capped per name, and the last 30 seconds are used to flatten.
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
import json                # JSON turns RIT's text replies into Python dictionaries
from http.client import HTTPConnection, HTTPException  # built-in HTTP; no requests / urllib3 needed
from math import ceil
from time import monotonic, sleep  # SLEEP pauses the program for a number of seconds
from urllib.parse import urlencode, urlparse

# ---------------------------------------------------------------- settings
# The ALGO2e practice case on flserver. DMA talks to the RIT server directly,
# so the RIT Client does not need to be running. Fill in your own trader ID
# and password.
BASE_URL = 'http://flserver.rotman.utoronto.ca:16625/v1'
USERNAME = '1010305634'
PASSWORD = 'zh'

# ---------------------------------------------------------------- connection
# Session keeps one connection open for every message. Without it Python
# would open and close a socket for each request, which causes errors when the
# next request starts before the last connection has finished closing.
# DMA signs in with HTTP Basic authentication: "Basic " + base64("id:password")
credentials = base64.b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()


class Response:
    """A reply from RIT. .ok, .status_code, .headers, .text and .json() are
    the names the rest of this file uses to read that reply."""

    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = headers
        self.text = body.decode()

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return json.loads(self.text)


class Session:
    """One reusable HTTP connection to the RIT server."""

    def __init__(self):
        parsed = urlparse(BASE_URL)
        self._host = parsed.hostname
        self._port = parsed.port
        self.headers = {}
        self._conn = None

    def _connect(self, timeout):
        self._conn = HTTPConnection(self._host, self._port, timeout=timeout)

    def request(self, method, url, params=None, timeout=30):
        path = urlparse(url).path
        if params:
            path = f'{path}?{urlencode(params)}'
        if self._conn is None:
            self._connect(timeout)
        try:
            self._conn.request(method, path, headers=self.headers)
            http_resp = self._conn.getresponse()
            body = http_resp.read()      # must read before the next request
        except (OSError, HTTPException):
            # The server closed the idle connection; open a new one and retry.
            self._connect(timeout)
            self._conn.request(method, path, headers=self.headers)
            http_resp = self._conn.getresponse()
            body = http_resp.read()
        return Response(http_resp.status, http_resp.headers, body)


s = Session()
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
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            wait = 0.1                  # no wait time given: pause briefly
        sleep(max(0.05, wait))


# ---------------------------------------------------------------- settings
# The brief caps each order at 5,000 shares and gross/net positions at 25,000.
# A positive passive number is a maker rebate. RY's is negative: providing
# liquidity there costs money, and taking liquidity there is paid.
TICKERS = ('CNR', 'RY', 'AC')
MAX_ORDER_SIZE = 5_000
MAX_GROSS_POSITION = 25_000
MAX_NET_POSITION = 25_000
# Soft cap per name so one stock cannot use the whole gross limit.
PER_NAME_LIMIT = 8_000
# Above this, inventory risk is large enough to pay the taker fee and cross.
HARD_INVENTORY = 15_000
PASSIVE_REBATE = {'CNR': 0.0023, 'RY': -0.0020, 'AC': 0.0011}
ACTIVE_FEE = {'CNR': 0.0027, 'RY': -0.0014, 'AC': 0.0015}
PRICE_TICK = 0.01
# A resting round trip must still pay after a one-tick adverse move.
ADVERSE_SELECTION = PRICE_TICK
CASE_LENGTH = 300
FLATTEN_TICK = CASE_LENGTH - 30
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
    # WHAT IT IS FOR: reads the best prices and displayed size. An empty book
    # returns no quote for this pass instead of stopping the whole algo.
    payload = {'ticker': ticker}
    resp = api(s, 'GET', 'securities/book', payload)
    if not resp.ok:
        raise RuntimeError(
            f'Could not retrieve {ticker} order book '
            f'(HTTP {resp.status_code}): {resp.text}'
        )
    book = resp.json()
    # The DMA Swagger uses singular names; the course's REST example uses the
    # plural names. Accept either documented response shape.
    bids = book.get('bid', book.get('bids')) or []
    asks = book.get('ask', book.get('asks')) or []
    if not bids or not asks:
        return None, None
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


def observe_mid(ticker, bid, ask, previous_mids):
    """Track the last mid for this ticker and return its recent move."""
    mid = (float(bid['price']) + float(ask['price'])) / 2
    previous_mid = previous_mids.get(ticker)
    previous_mids[ticker] = mid
    return mid, 0.0 if previous_mid is None else mid - previous_mid


def to_ticks(price):
    return int(round(float(price) / PRICE_TICK))


def from_ticks(ticks):
    return round(ticks * PRICE_TICK, 2)


def minimum_spread_ticks(ticker):
    # WHAT IT IS FOR: the tightest round trip that still pays after one tick
    # of adverse selection. CNR and AC clear this on a one-tick market because
    # their maker rebates help. RY charges 0.0020 per side to make a market,
    # so it needs a two-tick spread before a quote is worth resting.
    needed = ADVERSE_SELECTION - 2 * PASSIVE_REBATE[ticker]
    return max(1, ceil(needed / PRICE_TICK - 1e-9))


def market_edge(ticker, bid, ask):
    spread = float(ask['price']) - float(bid['price'])
    return spread + 2 * PASSIVE_REBATE[ticker]


def limit_quantity(positions, open_orders, ticker, action, quantity):
    # WHAT IT IS FOR: shrink a desired size until both the gross and the net
    # limit still hold after this order and every order already resting.
    if quantity <= 0:
        return 0
    quantity = min(quantity, MAX_ORDER_SIZE)

    pending_buys = sum(
        remaining_quantity(order) for order in open_orders
        if order['action'] == 'BUY' and order['ticker'] in TICKERS
    )
    pending_sells = sum(
        remaining_quantity(order) for order in open_orders
        if order['action'] == 'SELL' and order['ticker'] in TICKERS
    )
    net_low = sum(positions.values()) - pending_sells
    net_high = sum(positions.values()) + pending_buys
    if action == 'BUY':
        quantity = min(quantity, MAX_NET_POSITION - net_high)
    else:
        quantity = min(quantity, MAX_NET_POSITION + net_low)
    if quantity <= 0:
        return 0

    low, high = 0, quantity
    while low < high:
        candidate = (low + high + 1) // 2
        gross = projected_gross(
            positions, open_orders, ticker, action, candidate
        )
        net = net_high + candidate if action == 'BUY' else net_low - candidate
        if gross <= MAX_GROSS_POSITION and abs(net) <= MAX_NET_POSITION:
            low = candidate
        else:
            high = candidate - 1
    return low


def desired_passive_quantity(ticker, action, position, edge):
    # Scale size with how much the spread pays, and stop adding once this
    # name is already at its inventory budget. The reducing side stays up
    # so an existing position can be worked out at the rebate.
    adds = (action == 'BUY' and position >= 0) or (action == 'SELL' and position <= 0)
    if adds and abs(position) >= PER_NAME_LIMIT:
        return 0
    if edge <= ADVERSE_SELECTION and adds:
        return 0

    scale = min(1.0, max(0.0, (max(edge, 0.0) - ADVERSE_SELECTION) / 0.03))
    quantity = int(1_500 + scale * (MAX_ORDER_SIZE - 1_500))
    if adds:
        room = PER_NAME_LIMIT - abs(position)
        held_fraction = min(1.0, abs(position) / PER_NAME_LIMIT)
        quantity = int(quantity * (1.0 - 0.75 * held_fraction))
        quantity = min(quantity, room)
    else:
        quantity = max(quantity, min(abs(position), MAX_ORDER_SIZE))
    return max(0, min(MAX_ORDER_SIZE, quantity))


def quote_prices(ticker, bid, ask, position, recent_mid_change, latency):
    """Choose passive bid and ask prices for one security.

    Wide markets are joined one tick inside, which is where this case's
    variable spreads actually pay. A fast move backs off only the side that
    just became toxic. Inventory shifts both quotes toward flattening.
    """
    bid_tick = to_ticks(bid['price'])
    ask_tick = to_ticks(ask['price'])
    spread_ticks = ask_tick - bid_tick
    if spread_ticks <= 0:
        return None

    min_ticks = minimum_spread_ticks(ticker)
    improve = min(1, max(0, (spread_ticks - min_ticks) // 2))
    if latency > 0.4:
        improve = 0

    quote_bid = bid_tick + improve
    quote_ask = ask_tick - improve

    shock = int(round(recent_mid_change / PRICE_TICK))
    if shock >= 2:
        quote_bid -= min(2, shock - 1)
    elif shock <= -2:
        quote_ask += min(2, -shock - 1)

    skew = int(round(2 * position / PER_NAME_LIMIT))
    skew = max(-3, min(3, skew))
    quote_bid -= skew
    quote_ask -= skew

    # Stay on the passive side of the touch. Crossing would pay the active fee.
    quote_bid = min(quote_bid, ask_tick - 1)
    quote_ask = max(quote_ask, bid_tick + 1)
    if quote_ask - quote_bid < min_ticks:
        if position > 0:
            quote_bid = min(quote_bid, quote_ask - min_ticks)
        elif position < 0:
            quote_ask = max(quote_ask, quote_bid + min_ticks)
        else:
            return None
    if quote_ask <= quote_bid:
        return None
    return from_ticks(quote_bid), from_ticks(quote_ask)


def cancel_orders(ticker):
    resp = api(s, 'POST', 'commands/cancel', {'ticker': ticker})
    if not resp.ok:
        raise RuntimeError(
            f'Could not cancel {ticker} orders '
            f'(HTTP {resp.status_code}): {resp.text}'
        )


def order_id_of(order):
    return order.get('order_id', order.get('id'))


def cancel_order(order):
    # Cancel one resting order. DELETE is the per-order call; commands/cancel
    # with ids is the fallback on servers that do not accept DELETE.
    order_id = order_id_of(order)
    resp = api(s, 'DELETE', f'orders/{order_id}')
    if resp.ok:
        return True
    resp = api(s, 'POST', 'commands/cancel', {'ids': order_id})
    if resp.ok:
        return True
    print(
        f"Could not cancel order {order_id} "
        f'(HTTP {resp.status_code}): {resp.text}'
    )
    return False


def drop_order(open_orders, order):
    order_id = order_id_of(order)
    return [item for item in open_orders if order_id_of(item) != order_id]


def quote_is_current(order, price, quantity):
    # A small size change is not worth losing queue priority.
    if abs(float(order['price']) - price) >= PRICE_TICK / 2:
        return False
    difference = abs(remaining_quantity(order) - quantity)
    return difference < max(500, int(0.25 * quantity))


def sync_side(open_orders, ticker, action, price, quantity, positions):
    """Keep one resting order for this side, or cancel it when quantity is 0."""
    resting = [
        order for order in open_orders
        if order.get('ticker') == ticker and order.get('action') == action
    ]
    if quantity <= 0:
        for order in resting:
            if cancel_order(order):
                open_orders = drop_order(open_orders, order)
        return open_orders

    keeper = next(
        (order for order in resting if quote_is_current(order, price, quantity)),
        None,
    )
    for order in resting:
        if order is not keeper and cancel_order(order):
            open_orders = drop_order(open_orders, order)
    if keeper is not None:
        return open_orders

    quantity = limit_quantity(positions, open_orders, ticker, action, quantity)
    if quantity <= 0:
        return open_orders

    resp = api(s, 'POST', 'orders', {
        'ticker': ticker,
        'type': 'LIMIT',
        'quantity': quantity,
        'price': price,
        'action': action,
    })
    if not resp.ok:
        print(
            f'Could not place {action} order for {ticker} '
            f'(HTTP {resp.status_code}): {resp.text}'
        )
        return open_orders

    order = resp.json()
    filled = int(order.get('quantity_filled', 0))
    positions[ticker] += filled if action == 'BUY' else -filled
    if order.get('status') == 'OPEN' and remaining_quantity(order) > 0:
        open_orders.append(order)
    return open_orders


def flatten_position(ticker, position, bid, ask, open_orders, positions):
    # WHAT IT IS FOR: pay the active fee to get out when inventory is too
    # large or the case is about to end. A passive quote may not fill in time,
    # and an unhedged position is then just market risk.
    if position == 0 or bid is None:
        return open_orders
    action = 'SELL' if position > 0 else 'BUY'
    price = float(bid['price'] if action == 'SELL' else ask['price'])
    quantity = min(MAX_ORDER_SIZE, abs(position))
    other = 'BUY' if action == 'SELL' else 'SELL'
    # Drop the side that would add more inventory before crossing.
    open_orders = sync_side(open_orders, ticker, other, 0, 0, positions)
    return sync_side(open_orders, ticker, action, price, quantity, positions)


def cancel_all_orders():
    resp = api(s, 'POST', 'commands/cancel', {'all': 1})
    if not resp.ok:
        raise RuntimeError(
            f'Could not cancel all open orders '
            f'(HTTP {resp.status_code}): {resp.text}'
        )


# ---------------------------------------------------------------- algorithm
def main():
    # WHAT IT IS FOR: each pass, rank the three stocks by how much their
    # current spread pays after fees, quote the best ones passively, and
    # only cross the market to cut inventory that has become too large or
    # that will otherwise be left open at the 300-second close.
    tick, status = get_tick()
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
            tick, status = get_tick()
            continue

        was_active = True
        positions = get_positions()
        open_orders = get_open_orders()
        books = {}
        changes = {}
        edges = {}
        for ticker_symbol in TICKERS:
            bid, ask = get_order_book(ticker_symbol)
            books[ticker_symbol] = (bid, ask)
            if bid is None:
                edges[ticker_symbol] = -1
                changes[ticker_symbol] = 0.0
                continue
            _, changes[ticker_symbol] = observe_mid(
                ticker_symbol, bid, ask, previous_mids
            )
            edges[ticker_symbol] = market_edge(ticker_symbol, bid, ask)

        # Quote the widest risk-adjusted market first so it gets the shared
        # 25,000-share gross and net capacity before the others.
        latency = API_LATENCIES.get('securities/book', 0.0)
        endgame = tick is not None and tick >= FLATTEN_TICK
        for ticker_symbol in sorted(TICKERS, key=lambda name: edges[name], reverse=True):
            bid, ask = books[ticker_symbol]
            position = positions[ticker_symbol]
            if bid is None:
                continue

            must_flatten = endgame or abs(position) > HARD_INVENTORY
            if must_flatten:
                if position == 0:
                    open_orders = sync_side(
                        open_orders, ticker_symbol, 'BUY', 0, 0, positions
                    )
                    open_orders = sync_side(
                        open_orders, ticker_symbol, 'SELL', 0, 0, positions
                    )
                    continue
                if endgame:
                    target_position = 0
                else:
                    target_position = PER_NAME_LIMIT if position > 0 else -PER_NAME_LIMIT
                open_orders = flatten_position(
                    ticker_symbol,
                    position - target_position,
                    bid,
                    ask,
                    open_orders,
                    positions,
                )
                continue

            quotes = quote_prices(
                ticker_symbol,
                bid,
                ask,
                position,
                changes[ticker_symbol],
                latency,
            )
            edge = edges[ticker_symbol]
            if quotes is None and position > 0:
                bid_price, ask_price = None, float(ask['price'])
            elif quotes is None and position < 0:
                bid_price, ask_price = float(bid['price']), None
            elif quotes is None:
                bid_price, ask_price = None, None
            else:
                bid_price, ask_price = quotes

            buy_qty = 0 if bid_price is None else desired_passive_quantity(
                ticker_symbol, 'BUY', position, edge
            )
            sell_qty = 0 if ask_price is None else desired_passive_quantity(
                ticker_symbol, 'SELL', position, edge
            )
            open_orders = sync_side(
                open_orders, ticker_symbol, 'BUY', bid_price or 0, buy_qty, positions
            )
            open_orders = sync_side(
                open_orders, ticker_symbol, 'SELL', ask_price or 0, sell_qty, positions
            )

        tick, status = get_tick()
        sleep(0.05)


if __name__ == '__main__':   # true when you run this file directly
    try:
        main()               # so pressing Run (or F5) starts the algo
    except KeyboardInterrupt:
        print('Stopped with Ctrl+C.')

"""
RSM434 -- ALGO2e market-making primer, DMA REST API (Fall 2026)

The same program as "ALGO2e rev PY Script - In-Class.py", carrying both layers
of commentary in one file:

  * Block comments marked "WHAT IT IS FOR" explain the trading logic -- what
    each piece is trying to achieve, and where it is weak.
  * Inline comments explain the Python itself, line by line.

How to run
  1. Install the requests package, once:     pip install requests
  2. Set BASE_URL, USERNAME, and PASSWORD in the settings section below.
  3. While the ALGO2e practice case is running, run this file -- in VS Code,
     Run > Run Without Debugging; or in a terminal:
         python "RSM434 ALGO2e Primer - DMA.py"
  4. After a trailing or all-ticker stop, press Ctrl+H to resume if needed.
     The hard session-loss stop is final. Press Ctrl+C to stop at any time.

The strategy preserves valid quote priority, avoids adding inventory against
short-term trends, sizes down as inventory or volatility rises, and uses live
fee data. Trailing and hard loss stops liquidate positions and halt trading.
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
from collections import deque
from math import ceil
import msvcrt
from time import monotonic, sleep  # SLEEP pauses the program for a number of seconds

import requests            # REQUESTS sends the HTTP messages RIT listens for

# ---------------------------------------------------------------- settings
# DMA talks to the RIT server directly. Update these settings for the current run.
BASE_URL = 'http://flserver.rotman.utoronto.ca:16585/v1'
USERNAME = '1010209926'
PASSWORD = 'le'

# ---------------------------------------------------------------- connection
# Session() keeps one connection open for every message. Without it Python
# would open and close a socket for each request, which causes errors when the
# next request starts before the last connection has finished closing.
# DMA signs in with HTTP Basic authentication: "Basic " + base64("id:password")
s = requests.Session()
if USERNAME and PASSWORD:
    credentials = base64.b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()
    s.headers.update({'Authorization': 'Basic ' + credentials})


# ---------------------------------------------------------------- rate limit
class OrderSubmissionUncertain(requests.RequestException):
    """An order POST lost its response, so retrying it could duplicate trades."""


def api(s, method, path, params=None):
    """Send one request to RIT, waiting and retrying if we are going too fast.

    WHAT IT IS FOR: RIT applies global and security-specific request limits.
    When a request is rejected with HTTP 429, the response says how long to
    wait in the Retry-After header or "wait" body field. We wait and retry
    rather than treating a throttled request as successful. Safe requests are
    retried after transient connection errors; order submissions are not,
    because a lost response leaves their outcome uncertain.
    """
    network_retries = 0
    while True:
        started = monotonic()
        try:
            resp = s.request(
                method,
                f'{BASE_URL}/{path}',
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            if method.upper() == 'POST' and path == 'orders':
                raise OrderSubmissionUncertain(
                    'Order submission response was lost; will refresh '
                    'positions and open orders before trading again.'
                ) from error
            network_retries += 1
            if network_retries >= MAX_NETWORK_RETRIES:
                raise
            sleep(NETWORK_RETRY_DELAY_SECONDS * network_retries)
            continue

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
# ALGO2e permits 5,000 shares per order; use smaller children and per-name risk.
# The case also fines gross or net positions over 25,000 shares.
TICKERS = ('CNR', 'RY', 'AC')
MAX_ORDER_SIZE = 1_000
MAX_TICKER_POSITION = 2_000
MAX_GROSS_POSITION = 25_000
MAX_NET_POSITION = 25_000
# Liquidate a symbol and stop quoting it at this drawdown.
MAX_TICKER_LOSS = 2_500.0
# Liquidate all symbols once realized plus unrealized P&L falls this far below
# the value observed when the algorithm first starts trading.
MAX_SESSION_LOSS = 5_000.0
# Also stop after this much giveback from the session's best observed P&L.
MAX_SESSION_DRAWDOWN = 2_500.0
# Try a few depth-limited, marketable limit orders before the market fallback.
STOP_LIMIT_ATTEMPTS = 3
PRICE_TICK = 0.01
# Movement and API latency reduce size, not quote competitiveness.
MAX_VOLATILITY_TICKS = 3
REQUOTE_TOLERANCE_TICKS = 1
TREND_LOOKBACK = 6
TREND_FILTER_TICKS = 3
LATENCY_BUFFER_SECONDS_PER_TICK = 0.25
REQUEST_TIMEOUT_SECONDS = 3
MAX_NETWORK_RETRIES = 3
NETWORK_RETRY_DELAY_SECONDS = 0.25
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
        return None
    return bids[0], asks[0]


def get_positions():
    # WHAT IT IS FOR: collect portfolio positions, current maker rebates, and
    # realized/unrealized P&L from the same API response.
    resp = api(s, 'GET', 'securities')
    if not resp.ok:
        raise RuntimeError(
            f'Could not retrieve positions (HTTP {resp.status_code}): {resp.text}'
        )
    securities = resp.json()
    positions = {}
    passive_rebates = {}
    ticker_pnls = {}
    total_pnl = 0.0
    for security in securities:
        if security['ticker'] in TICKERS:
            ticker = security['ticker']
            positions[ticker] = int(security['position'])
            passive_rebates[ticker] = float(
                security.get('limit_order_rebate', 0.0)
            )
            ticker_pnls[ticker] = (
                float(security.get('realized', 0.0))
                + float(security.get('unrealized', 0.0))
            )
            total_pnl += ticker_pnls[ticker]
    missing_tickers = set(TICKERS) - positions.keys()
    if missing_tickers:
        raise RuntimeError(
            f'Position data is missing: {", ".join(sorted(missing_tickers))}.'
        )
    return positions, passive_rebates, ticker_pnls, total_pnl


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
    ticker, action, level, spread, positions, open_orders, passive_rebate,
    volatility_ticks=0
):
    # WHAT IT IS FOR: size orders to both the market and our remaining risk
    # capacity. The visible queue sets the starting size; poor economics or
    # limited portfolio headroom reduce it further.
    if spread <= 0:
        return 0

    # Both quotes are non-marketable limits, so both fills are passive.
    # RIT reports rebates as signed values; a negative rebate is a maker fee.
    round_trip_edge = spread + 2 * passive_rebate
    if round_trip_edge < PRICE_TICK:
        return 0

    edge_fraction = min(1.0, round_trip_edge / spread)
    position = positions[ticker]
    pending_buys_for_ticker = sum(
        remaining_quantity(order) for order in open_orders
        if order['ticker'] == ticker and order['action'] == 'BUY'
    )
    pending_sells_for_ticker = sum(
        remaining_quantity(order) for order in open_orders
        if order['ticker'] == ticker and order['action'] == 'SELL'
    )
    directional_position = (
        position + pending_buys_for_ticker
        if action == 'BUY'
        else position - pending_sells_for_ticker
    )
    adds_to_inventory = (
        action == 'BUY' and directional_position >= 0
    ) or (
        action == 'SELL' and directional_position <= 0
    )
    inventory_fraction = min(
        1.0, abs(directional_position) / MAX_TICKER_POSITION
    )
    inventory_factor = (
        (1.0 - inventory_fraction) ** 2 if adds_to_inventory else 1.0
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

    ticker_buy_exposure = positions[ticker] + pending_buys_for_ticker
    ticker_sell_exposure = positions[ticker] - pending_sells_for_ticker
    if action == 'BUY':
        quantity = min(
            quantity, MAX_TICKER_POSITION - ticker_buy_exposure
        )
    else:
        quantity = min(
            quantity, MAX_TICKER_POSITION + ticker_sell_exposure
        )
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


def observe_mid(ticker, bid, ask, mid_history):
    """Return one-step and short-window mid moves for a ticker."""
    mid = (float(bid['price']) + float(ask['price'])) / 2
    history = mid_history.setdefault(ticker, deque(maxlen=TREND_LOOKBACK))
    recent_move = 0.0 if not history else mid - history[-1]
    trend_move = (
        mid - history[0] if len(history) == TREND_LOOKBACK else 0.0
    )
    history.append(mid)
    return mid, recent_move, trend_move


def should_quote_side(action, position, trend_move):
    """Avoid counter-trend inventory additions while permitting reductions."""
    reduces_inventory = (action == 'BUY' and position < 0) or (
        action == 'SELL' and position > 0
    )
    if reduces_inventory:
        return True
    if action == 'BUY':
        return trend_move > -TREND_FILTER_TICKS * PRICE_TICK
    return trend_move < TREND_FILTER_TICKS * PRICE_TICK


def session_risk_limit_reached(session_pnl, peak_session_pnl):
    return (
        session_pnl <= -MAX_SESSION_LOSS
        or peak_session_pnl - session_pnl >= MAX_SESSION_DRAWDOWN
    )


def calculate_quote_prices(
    bid, ask, position, recent_mid_change, request_latency=0.0
):
    """Quote at the touch, skewing away from inventory risk."""
    best_bid = float(bid['price'])
    best_ask = float(ask['price'])
    if best_ask <= best_bid:
        return None

    movement_ticks = ceil(abs(recent_mid_change) / PRICE_TICK)
    latency_ticks = ceil(request_latency / LATENCY_BUFFER_SECONDS_PER_TICK)
    volatility_ticks = min(MAX_VOLATILITY_TICKS, max(movement_ticks, latency_ticks))
    spread_ticks = max(1, round((best_ask - best_bid) / PRICE_TICK))
    inventory_shift = round(
        (position / MAX_TICKER_POSITION) * max(1, spread_ticks // 2)
    )
    # Do not chase short-term moves: a price change can signal informed flow
    # picking off the stale side of a market-making quote.
    center_shift = -inventory_shift

    quote_bid = best_bid + center_shift * PRICE_TICK
    quote_ask = best_ask + center_shift * PRICE_TICK
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


def cancel_order_ids(order_ids):
    """Cancel only stale quotes so valid orders keep their queue priority."""
    if not order_ids:
        return
    ids = ','.join(str(order_id) for order_id in order_ids)
    resp = api(s, 'POST', 'commands/cancel', {'ids': ids})
    if not resp.ok:
        raise RuntimeError(
            f'Could not cancel stale orders {ids} '
            f'(HTTP {resp.status_code}): {resp.text}'
        )


def stale_order_ids(
    open_orders, ticker, bid, ask, quote_prices, position, trend_move,
    known_order_prices,
):
    """Return orders that are unsafe, disallowed, or stale by over one tick."""
    best_bid = float(bid['price'])
    best_ask = float(ask['price'])
    permitted = {
        action: should_quote_side(action, position, trend_move)
        for action in ('BUY', 'SELL')
    }
    stale = []
    seen_actions = set()
    for order in open_orders:
        if order['ticker'] != ticker:
            continue
        action = order['action']
        price = order.get('price')
        if price is None:
            price = known_order_prices.get(order.get('order_id'))
        desired_price = quote_prices.get(action)
        is_crossed = (
            price is None
            or (action == 'BUY' and float(price) >= best_ask)
            or (action == 'SELL' and float(price) <= best_bid)
        )
        is_stale = (
            desired_price is None
            or not permitted[action]
            or action in seen_actions
            or is_crossed
            or abs(float(price) - desired_price)
            > REQUOTE_TOLERANCE_TICKS * PRICE_TICK + 1e-9
        )
        if is_stale:
            stale.append(order['order_id'])
        else:
            seen_actions.add(action)
    return stale


def cancel_all_orders():
    resp = api(s, 'POST', 'commands/cancel', {'all': 1})
    if not resp.ok:
        raise RuntimeError(
            f'Could not cancel all open orders '
            f'(HTTP {resp.status_code}): {resp.text}'
        )


def flatten_position(ticker, position):
    """Unwind at displayed depth first, then use capped market-order chunks."""
    if position == 0:
        return 0
    action = 'SELL' if position > 0 else 'BUY'
    limit_attempts = 0
    while position:
        order_type = 'MARKET'
        quantity = min(MAX_ORDER_SIZE, abs(position))
        order_params = {
            'ticker': ticker,
            'type': order_type,
            'quantity': quantity,
            'action': action,
        }
        if limit_attempts < STOP_LIMIT_ATTEMPTS:
            book = get_order_book(ticker)
            if book:
                bid, ask = book
                level = bid if action == 'SELL' else ask
                visible_quantity = int(level['quantity'])
                if visible_quantity > 0:
                    quantity = min(quantity, visible_quantity)
                    order_type = 'LIMIT'
                    order_params = {
                        'ticker': ticker,
                        'type': order_type,
                        'quantity': quantity,
                        'price': float(level['price']),
                        'action': action,
                    }
            limit_attempts += 1
            if order_type == 'MARKET' and limit_attempts < STOP_LIMIT_ATTEMPTS:
                sleep(0.1)
                continue

        resp = api(s, 'POST', 'orders', order_params)
        if not resp.ok:
            if order_type == 'LIMIT' and is_order_rejection(resp):
                print(
                    f'Touch-price stop for {ticker} was rejected '
                    f'(HTTP {resp.status_code}); refreshing the book.'
                )
                sleep(0.1)
                continue
            raise RuntimeError(
                f'Could not stop out {ticker} '
                f'(HTTP {resp.status_code}): {resp.text}'
            )
        order = resp.json()
        filled = int(order.get('quantity_filled', 0))
        if filled < 0 or filled > quantity:
            raise RuntimeError(
                f'Stop-loss order for {ticker} filled {filled} of '
                f'{quantity}; position remains {position}.'
            )
        position += filled if action == 'BUY' else -filled
        if order_type == 'LIMIT' and (
            order.get('status') == 'OPEN'
            and remaining_quantity(order) > 0
        ):
            order_id = order.get('order_id')
            if order_id is None:
                cancel_orders(ticker)
            else:
                cancel_order_ids([order_id])
            positions, _, _, _ = get_positions()
            position = positions[ticker]
        if filled == 0 and order_type == 'MARKET':
            raise RuntimeError(
                f'Market stop-loss order for {ticker} filled 0 of '
                f'{quantity}; position remains {position}.'
            )
        if position and order_type == 'LIMIT':
            sleep(0.1)
    print(f'Stop-loss closed the {ticker} position.')
    return position


def is_order_rejection(resp):
    return resp.status_code in (400, 409, 422)


def wait_for_manual_resume(reason):
    """Wait for Ctrl+H to resume after a soft stop; Ctrl+C exits."""
    print(
        f'{reason} If volume is still needed, press Ctrl+H to resume; '
        'press Ctrl+C to stop.',
        flush=True,
    )
    while True:
        key = msvcrt.getch()
        if key == b'\x08':
            print('Manual resume requested.')
            return
        if key == b'\x03':
            raise KeyboardInterrupt


# ---------------------------------------------------------------- algorithm
def main():
    # WHAT IT IS FOR: quote the selected securities using current market
    # conditions, fee economics, inventory, and portfolio risk capacity.
    if (
        not BASE_URL.endswith('/v1') or not USERNAME or not PASSWORD
    ):
        raise RuntimeError(
            'Set BASE_URL, USERNAME, and PASSWORD in the settings section '
            'before starting.'
        )
    mid_history = {}
    unavailable_books = set()
    was_active = False
    starting_pnl = None
    starting_ticker_pnls = None
    session_peak_pnl = 0.0
    known_order_prices = {}
    stopped_tickers = set()
    session_stop_triggered = False
    session_stop_is_hard = False

    while True:
        try:
            tick, status = get_tick()
            if status == 'STOPPED':
                print(f'Case stopped at tick {tick}; trading has ended.')
                break
            if status != 'ACTIVE':
                if was_active:
                    cancel_all_orders()
                was_active = False
                sleep(0.2)
                continue

            if session_stop_triggered:
                cancel_all_orders()
                positions, _, _, _ = get_positions()
                for ticker in TICKERS:
                    positions[ticker] = flatten_position(
                        ticker, positions[ticker]
                    )
                if all(position == 0 for position in positions.values()):
                    positions, _, ticker_pnls, total_pnl = get_positions()
                    if (
                        session_stop_is_hard
                        or total_pnl - starting_pnl <= -MAX_SESSION_LOSS
                    ):
                        print(
                            'Hard session-loss limit reached; trading halted. '
                            'Manual resume is disabled for this stop.'
                        )
                        return
                    wait_for_manual_resume('Session trailing stop is complete.')
                    session_peak_pnl = total_pnl - starting_pnl
                    starting_ticker_pnls = ticker_pnls.copy()
                    stopped_tickers.clear()
                    session_stop_triggered = False
                    session_stop_is_hard = False
                    continue
                sleep(0.2)
                continue

            if not was_active:
                cancel_all_orders()
            was_active = True
            for ticker_symbol in TICKERS:
                tick, status = get_tick()
                if status != 'ACTIVE':
                    break

                (
                    positions,
                    passive_rebates,
                    ticker_pnls,
                    total_pnl,
                ) = get_positions()
                if starting_pnl is None:
                    starting_pnl = total_pnl
                    starting_ticker_pnls = ticker_pnls.copy()
                session_pnl = total_pnl - starting_pnl
                session_peak_pnl = max(session_peak_pnl, session_pnl)
                session_drawdown = session_peak_pnl - session_pnl
                if session_risk_limit_reached(session_pnl, session_peak_pnl):
                    session_stop_triggered = True
                    session_stop_is_hard = (
                        session_pnl <= -MAX_SESSION_LOSS
                    )
                    cancel_all_orders()
                    stop_reason = (
                        f'hard loss limit ${MAX_SESSION_LOSS:.0f}'
                        if session_pnl <= -MAX_SESSION_LOSS
                        else f'trailing drawdown ${MAX_SESSION_DRAWDOWN:.0f}'
                    )
                    print(
                        f'Session risk stop at tick {tick} ({stop_reason}; '
                        f'P&L {session_pnl:.2f}, peak drawdown '
                        f'{session_drawdown:.2f}); cancelling open orders '
                        'and liquidating positions.'
                    )
                    positions, _, _, _ = get_positions()
                    for ticker in TICKERS:
                        positions[ticker] = flatten_position(
                            ticker, positions[ticker]
                        )
                    if all(position == 0 for position in positions.values()):
                        positions, _, ticker_pnls, total_pnl = get_positions()
                        if (
                            session_stop_is_hard
                            or total_pnl - starting_pnl <= -MAX_SESSION_LOSS
                        ):
                            print(
                                'Hard session-loss limit reached; trading '
                                'halted. Manual resume is disabled for this '
                                'stop.'
                            )
                            return
                        wait_for_manual_resume(
                            'Session trailing stop is complete.'
                        )
                        session_peak_pnl = total_pnl - starting_pnl
                        starting_ticker_pnls = ticker_pnls.copy()
                        stopped_tickers.clear()
                        session_stop_triggered = False
                        session_stop_is_hard = False
                        break
                    break
                if ticker_symbol in stopped_tickers:
                    open_orders = get_open_orders()
                    if any(
                        order['ticker'] == ticker_symbol
                        for order in open_orders
                    ):
                        cancel_orders(ticker_symbol)
                        positions, _, _, _ = get_positions()
                    if positions[ticker_symbol]:
                        flatten_position(ticker_symbol, positions[ticker_symbol])
                    continue
                ticker_session_pnl = (
                    ticker_pnls[ticker_symbol]
                    - starting_ticker_pnls[ticker_symbol]
                )
                if (
                    ticker_session_pnl <= -MAX_TICKER_LOSS
                    and ticker_symbol not in stopped_tickers
                ):
                    stopped_tickers.add(ticker_symbol)
                    print(
                        f'{ticker_symbol} loss limit reached '
                        f'({ticker_session_pnl:.2f}); stopping this symbol.'
                    )
                    cancel_orders(ticker_symbol)
                    positions, _, _, _ = get_positions()
                    positions[ticker_symbol] = flatten_position(
                        ticker_symbol, positions[ticker_symbol]
                    )
                    continue
                book = get_order_book(ticker_symbol)
                if book is None:
                    open_orders = get_open_orders()
                    if any(
                        order['ticker'] == ticker_symbol
                        for order in open_orders
                    ):
                        cancel_orders(ticker_symbol)
                    if ticker_symbol not in unavailable_books:
                        print(
                            f'{ticker_symbol} has no two-sided order book; '
                            'skipping it until both sides return.'
                        )
                    unavailable_books.add(ticker_symbol)
                    continue
                unavailable_books.discard(ticker_symbol)
                bid, ask = book

                _, recent_mid_change, trend_move = observe_mid(
                    ticker_symbol, bid, ask, mid_history
                )
                market_spread = float(ask['price']) - float(bid['price'])
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
                    open_orders = get_open_orders()
                    if any(
                        order['ticker'] == ticker_symbol
                        for order in open_orders
                    ):
                        cancel_orders(ticker_symbol)
                    continue
                bid_price, ask_price, volatility_ticks = quotes
                open_orders = get_open_orders()
                open_order_ids = {
                    order.get('order_id') for order in open_orders
                }
                known_order_prices = {
                    order_id: price
                    for order_id, price in known_order_prices.items()
                    if order_id in open_order_ids
                }
                quote_prices = {'BUY': bid_price, 'SELL': ask_price}
                stale_orders = stale_order_ids(
                    open_orders,
                    ticker_symbol,
                    bid,
                    ask,
                    quote_prices,
                    positions[ticker_symbol],
                    trend_move,
                    known_order_prices,
                )
                if stale_orders:
                    cancel_order_ids(stale_orders)
                    for order_id in stale_orders:
                        known_order_prices.pop(order_id, None)
                    continue

                for action, level, price in (
                    ('BUY', bid, bid_price),
                    ('SELL', ask, ask_price),
                ):
                    if not should_quote_side(
                        action,
                        positions[ticker_symbol],
                        trend_move,
                    ):
                        continue
                    if any(
                        order['ticker'] == ticker_symbol
                        and order['action'] == action
                        for order in open_orders
                    ):
                        continue
                    quantity = calculate_order_quantity(
                        ticker_symbol,
                        action,
                        level,
                        market_spread,
                        positions,
                        open_orders,
                        passive_rebates[ticker_symbol],
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
                        if is_order_rejection(resp):
                            print(
                                f'{action} order for {ticker_symbol} was rejected '
                                f'(HTTP {resp.status_code}): {resp.text}'
                            )
                            continue
                        raise RuntimeError(
                            f'Could not place {action} order for {ticker_symbol} '
                            f'(HTTP {resp.status_code}): {resp.text}'
                        )
                    order = resp.json()
                    filled = int(order.get('quantity_filled', 0))
                    positions[ticker_symbol] += (
                        filled if action == 'BUY' else -filled
                    )
                    if (
                        order.get('status') == 'OPEN'
                        and remaining_quantity(order) > 0
                    ):
                        open_orders.append(order)
                        if order.get('order_id') is not None:
                            known_order_prices[order['order_id']] = price
            if (
                status == 'ACTIVE'
                and not session_stop_triggered
                and stopped_tickers == set(TICKERS)
            ):
                _, _, ticker_pnls, total_pnl = get_positions()
                session_pnl = total_pnl - starting_pnl
                session_peak_pnl = max(session_peak_pnl, session_pnl)
                if session_pnl <= -MAX_SESSION_LOSS:
                    print(
                        'Hard session-loss limit reached after ticker '
                        'liquidation; trading halted.'
                    )
                    return
                if session_risk_limit_reached(session_pnl, session_peak_pnl):
                    session_stop_triggered = True
                    session_stop_is_hard = False
                    continue
                wait_for_manual_resume('All ticker loss stops have triggered.')
                starting_ticker_pnls = ticker_pnls.copy()
                stopped_tickers.clear()
        except OrderSubmissionUncertain as error:
            print(f'{error} Retrying after refreshing market state.')
            sleep(0.5)
        except requests.RequestException as error:
            print(f'Temporary DMA connection error: {error}. Retrying.')
            sleep(1.0)


if __name__ == '__main__':   # true when you run this file directly
    try:
        main()               # so pressing Run (or F5) starts the algo
    except KeyboardInterrupt:
        print('Stopped with Ctrl+C.')

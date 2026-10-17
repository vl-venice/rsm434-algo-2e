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

This is a starting point, not a finished strategy. The Class 3 slides list what
to improve: the position check below, one set of prices for three stocks with
different fees, and no inventory management.
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
BASE_URL = 'http://flserver.rotman.utoronto.ca:16585/v1'
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
        except (ValueError, KeyError):
            wait = 0.1                  # no wait time given: pause briefly
        sleep(wait)


# ---------------------------------------------------------------- settings
# Variables that do not change while the case runs. They sit outside every
# function, which makes them "global": any function can read them. Keeping them
# in one place means a single edit changes every line that uses them.
#
# WHAT THEY ARE FOR: the case sets a gross and a net position limit, and
# charges a fine for breaching either one (check the /limits endpoint for the
# current values). These constants are how the algo tries to stay inside them.
MAX_LONG_EXPOSURE = 25000     # most shares we want to be long at any moment
MAX_SHORT_EXPOSURE = -25000   # most shares we want to be short at any moment
ORDER_LIMIT = 500             # number of shares in each order we send


# ---------------------------------------------------------------- helpers
def get_tick():
    # WHAT IT IS FOR: asks RIT for the state of the case -- "ACTIVE", "STOPPED"
    # or "PAUSED" -- which the while loop uses to decide whether to keep
    # trading. It also returns the tick, which this code does not use but which
    # is useful if you extend the algo, for example to stop trading near the
    # close.
    resp = api(s, 'GET', 'case')
    if resp.ok:                  # .ok is True when the reply's status code is 200
        case = resp.json()       # turns RIT's JSON reply into a dictionary
        return case['tick'], case['status']   # returns two values at once


def get_bid_ask(ticker):
    # WHAT IT IS FOR: reads the order book for one stock and returns the best
    # bid and best ask -- the top of the book. Every decision below depends on
    # these two prices. The same reply also carries the quantity available at
    # each price, which a better algo would use.
    payload = {'ticker': ticker}      # a dictionary: the key 'ticker' with the value passed in
    resp = api(s, 'GET', 'securities/book', payload)
    if resp.ok:
        book = resp.json()            # a dictionary holding two lists, 'bids' and 'asks'
        bid_side_book = book['bids']  # the list of bids, best price first
        ask_side_book = book['asks']  # the list of asks, best price first

        # A list comprehension: "the price of every item in that list".
        bid_prices_book = [item['price'] for item in bid_side_book]
        ask_prices_book = [item['price'] for item in ask_side_book]

        best_bid_price = bid_prices_book[0]   # [0] is the first item in a list
        best_ask_price = ask_prices_book[0]

        return best_bid_price, best_ask_price


def get_time_sales(ticker):
    # WHAT IT IS FOR: the history of trades in a stock. Not used below, but a
    # strategy could read it to see how much volume is actually trading.
    payload = {'ticker': ticker}
    resp = api(s, 'GET', 'securities/tas', payload)
    if resp.ok:
        book = resp.json()
        time_sales_book = [item['quantity'] for item in book]
        return time_sales_book


def get_position():
    # WHAT IT IS FOR: returns the GROSS position across all three stocks -- the
    # sum of the absolute value of each position. The case limits both gross
    # and net, with a separate fine for each, and this function reports only
    # the gross. That matters for the decision below: see the note in main().
    resp = api(s, 'GET', 'securities')
    if resp.ok:
        book = resp.json()
        # abs() makes every position positive before adding: summing without
        # abs() would give the NET position instead.
        return abs(book[0]['position']) + abs(book[1]['position']) + abs(book[2]['position'])


def get_open_orders(ticker):
    # WHAT IT IS FOR: every order this algo places is a limit order, so knowing
    # what is still resting in the book is useful for controlling exposure.
    # Not used below, but it is the natural next step for finer control.
    payload = {'ticker': ticker}
    resp = api(s, 'GET', 'orders', payload)
    if resp.ok:
        orders = resp.json()
        # Keep only the items in the list whose 'action' is BUY, then SELL.
        buy_orders = [item for item in orders if item['action'] == 'BUY']
        sell_orders = [item for item in orders if item['action'] == 'SELL']
        return buy_orders, sell_orders


def get_order_status(order_id):
    # WHAT IT IS FOR: the state of one specific order -- "OPEN", "CANCELLED" or
    # "TRANSACTED". Each order gets a unique id when RIT accepts it.
    resp = api(s, 'GET', f'orders/{order_id}')
    if resp.ok:
        order = resp.json()
        return order['status']


# ---------------------------------------------------------------- algorithm
def main():
    # WHAT IT IS FOR: the decision-making part of the algo. It quotes on both
    # sides of all three stocks, one stock at a time, for as long as the case
    # is running.
    tick, status = get_tick()          # start by asking whether the case is running
    ticker_list = ['CNR', 'RY', 'AC']  # the three stocks; the algo treats them identically

    # The while loop IS the algo: it repeats the instructions below for the
    # whole case. It has to re-read the market each time, because the quotes
    # feeding the decisions change constantly.
    while status == 'ACTIVE':          # == compares; it is case sensitive

        for i in range(3):             # range(3) counts 0, 1, 2 -- one pass per stock

            ticker_symbol = ticker_list[i]   # i = 0 gives 'CNR', and so on

            # Gather the data BEFORE deciding anything. Anything you think
            # matters for a decision has to be queried first.
            position = get_position()
            best_bid_price, best_ask_price = get_bid_ask(ticker_symbol)

            if position < MAX_LONG_EXPOSURE:
                # An if statement is the usual trigger for sending an order:
                # here we join the bid, buying at the best bid price, as long
                # as we have not reached our long limit.
                #
                # NOTE: this condition is flawed and incomplete, and it will
                # cause trouble. get_position() returns the GROSS position,
                # which is never negative, so the short test below can never
                # stop the algo selling. Worse, once gross reaches the limit
                # this test also blocks buying that would REDUCE a short
                # position. Working out what breaks a strategy is the skill
                # worth practising: there is little to learn from watching code
                # behave when everything goes right.
                api(s, 'POST', 'orders', {'ticker': ticker_symbol, 'type': 'LIMIT',
                                          'quantity': ORDER_LIMIT, 'price': best_bid_price,
                                          'action': 'BUY'})

            if position > MAX_SHORT_EXPOSURE:
                # The mirror image: join the ask, selling at the best ask price.
                api(s, 'POST', 'orders', {'ticker': ticker_symbol, 'type': 'LIMIT',
                                          'quantity': ORDER_LIMIT, 'price': best_ask_price,
                                          'action': 'SELL'})

            # Pause to give those passive orders a chance to trade before they
            # are cancelled. This is NOT optimal: a fixed wait is too long when
            # the market is moving and too short when it is not.
            sleep(0.5)

            # Cancel whatever did not fill, so resting orders do not pile up
            # and push the position past its limits. This is NOT optimal
            # either: cancelling every pass also gives up queue priority.
            api(s, 'POST', 'commands/cancel', {'ticker': ticker_symbol})

        # Update the status, or the while loop would run forever.
        tick, status = get_tick()


if __name__ == '__main__':   # true when you run this file directly
    try:
        main()               # so pressing Run (or F5) starts the algo
    except KeyboardInterrupt:
        print('Stopped with Ctrl+C.')

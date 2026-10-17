"""Observe public OKX bars in a bounded, order-free CLI shadow run.

Prerequisites:
    Published ml4t-live package and outbound HTTPS to the OKX public API.
Expected Output:
    At least one ``OKX bar`` line when completed candles are available, followed by
    the CLI's ``Completed shadow run`` summary with ``orders=0``.
Expected Failure:
    Feed or network errors may stop the run; a quiet feed may yield no bars.
Cleanup:
    The CLI stops the feed and closes the shadow broker after the duration.
"""

from ml4t.backtest import Strategy

SYMBOLS = ["BTC-USDT-SWAP"]


class ObserveOKXBars(Strategy):
    def on_data(self, timestamp, data, context, broker) -> None:
        bar = data.get(SYMBOLS[0])
        if bar is not None and "close" in bar:
            print(f"OKX bar {timestamp.isoformat()} {SYMBOLS[0]} close={bar.get('close')}")

"""
Candles from YOUR MT5 terminal (desktop app, Windows) for the lab: `run.bat lab compare --source mt5 ...`.
Usually holds years of history, where the public feed has one, and records the real spread of each bar.
"""
import statistics
import mt5_bot

TF_NAME = {v: k for k, v in mt5_bot.TF_SECONDS.items()}
_connected = False


def mt5_name(symbol):
    """frxAUDJPY (public feed name) -> AUDJPY (MT5 name)."""
    return symbol[3:] if symbol.startswith("frx") else symbol


def _connect():
    global _connected
    if not _connected:
        mt5_bot.connect()
        _connected = True


def load(symbol, tf_seconds, bars=100000):
    """Returns ([(epoch, o, h, l, c)], median_spread_pct or None) for the closed bars MT5 gives us (up to `bars`)."""
    _connect()
    m = mt5_bot.mt5
    name = mt5_name(symbol)
    if tf_seconds not in TF_NAME:
        raise RuntimeError(f"unsupported timeframe {tf_seconds}s (use one of {sorted(TF_NAME)})")
    if not m.symbol_select(name, True):
        raise RuntimeError(f"{name} is not available in your MT5: add it in Market Watch (Ctrl+M, right-click, Symbols)")
    tf = mt5_bot.tf_const(TF_NAME[tf_seconds])
    n, rates = bars, None
    while n >= 500:                                   # the terminal may cap bars; halve until it answers
        rates = m.copy_rates_from_pos(name, tf, 1, n)     # offset 1 = closed bars only
        if rates is not None and len(rates) >= 500:
            break
        n //= 2
    if rates is None or len(rates) < 500:
        raise RuntimeError(f"MT5 returned too little {name} history. Open its chart, press Home a few times to load more, "
                           "and set Tools > Options > Charts > Max bars in chart to Unlimited.")
    si = m.symbol_info(name)
    point = float(getattr(si, "point", 0) or 0)
    rows = [(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])) for r in rates]
    pcts = []
    for r in rates:
        try:
            sp = float(r["spread"])
        except (KeyError, ValueError, IndexError):
            continue
        if sp > 0 and point and r["close"]:
            pcts.append(sp * point / float(r["close"]) * 100)
    return rows, (statistics.median(pcts) if pcts else None)

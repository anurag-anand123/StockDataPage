#!/usr/bin/env python3
"""
fetch_market_data.py

Fetches 10 years of daily OHLC data for every S&P 500 and Nifty 500 stock
from Twelve Data, aggregates it into weekly and monthly candles, computes
multi-period returns, then writes compact JSON files that the Flutter app
reads from GitHub Pages.

Run once per weekday after market close via GitHub Actions.
One API key, one daily run — all users share the pre-built data for free.
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Config ───────────────────────────────────────────────────────────────────

API_KEY  = os.environ.get('TWELVE_DATA_API_KEY', '')
BASE_URL = 'https://api.twelvedata.com'

# 'sp500', 'nifty500', or 'all' (default).
MARKET = os.environ.get('MARKET', 'all').lower()

# Free tier: 8 req/min.  8 s between calls keeps us safely under the limit.
# Set REQUEST_DELAY=0 in the environment if you're on a paid plan.
REQUEST_DELAY = float(os.environ.get('REQUEST_DELAY', '8'))

REPO_ROOT    = Path(__file__).resolve().parent.parent
TICKERS_FILE = REPO_ROOT / 'tickers.json'
DATA_DIR     = REPO_ROOT / 'data'

# Keys must match the sort-mode strings used in the Flutter screener.
RETURN_PERIODS = {
    '1Y Return':  365,
    '2Y Return':  730,
    '5Y Return':  1825,
    '10Y Return': 3650,
}

# ─── API ──────────────────────────────────────────────────────────────────────

def fetch_ohlc(symbol: str, interval: str, outputsize: int = 5000) -> dict | None:
    """Fetch OHLC from Twelve Data for the given interval. Returns None on error."""
    url = (
        f'{BASE_URL}/time_series'
        f'?symbol={symbol}'
        f'&interval={interval}'
        f'&outputsize={outputsize}'
        f'&apikey={API_KEY}'
    )
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get('status') == 'error':
            print(f'  [skip] {symbol} {interval}: {data.get("message", "API error")}')
            return None
        return data
    except Exception as exc:
        print(f'  [error] {symbol} {interval}: {exc}')
        return None


# ─── Parsing ──────────────────────────────────────────────────────────────────

def parse_values(
    data: dict,
) -> tuple[list[int], list[float], list[float], list[float], list[float]]:
    """
    Parse a Twelve Data time_series response into parallel arrays.
    Returns (timestamps_unix, opens, highs, lows, closes) in chronological order.
    """
    values = list(reversed(data.get('values', [])))  # newest-first → oldest-first
    timestamps, opens, highs, lows, closes = [], [], [], [], []
    for v in values:
        try:
            dt = datetime.fromisoformat(v['datetime'].replace(' ', 'T'))
            ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
            timestamps.append(ts)
            opens.append(round(float(v['open']),  4))
            highs.append(round(float(v['high']),  4))
            lows.append(round(float(v['low']),   4))
            closes.append(round(float(v['close']), 4))
        except (KeyError, ValueError):
            continue
    return timestamps, opens, highs, lows, closes


# ─── Aggregation ──────────────────────────────────────────────────────────────

def _aggregate(
    group_key_fn,
    timestamps: list[int],
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> tuple[list[int], list[float], list[float], list[float], list[float]]:
    """Generic OHLC aggregator. group_key_fn(datetime) → comparable sort key."""
    buckets: dict = {}
    for i, ts in enumerate(timestamps):
        dt  = datetime.fromtimestamp(ts, tz=timezone.utc)
        key = group_key_fn(dt)
        if key not in buckets:
            buckets[key] = dict(t=[], o=[], h=[], l=[], c=[])
        b = buckets[key]
        b['t'].append(ts)
        b['o'].append(opens[i])
        b['h'].append(highs[i])
        b['l'].append(lows[i])
        b['c'].append(closes[i])

    rt, ro, rh, rl, rc = [], [], [], [], []
    for key in sorted(buckets):
        b = buckets[key]
        rt.append(b['t'][-1])       # timestamp of the last trading day in the period
        ro.append(b['o'][0])        # open of the first trading day
        rh.append(max(b['h']))      # highest high
        rl.append(min(b['l']))      # lowest low
        rc.append(b['c'][-1])       # close of the last trading day
    return rt, ro, rh, rl, rc


def to_weekly(timestamps, opens, highs, lows, closes):
    def key(dt):
        iso = dt.isocalendar()
        return (iso.year, iso.week)
    return _aggregate(key, timestamps, opens, highs, lows, closes)


def to_monthly(timestamps, opens, highs, lows, closes):
    return _aggregate(lambda dt: (dt.year, dt.month),
                      timestamps, opens, highs, lows, closes)


# ─── Returns ──────────────────────────────────────────────────────────────────

def compute_returns(timestamps: list[int], closes: list[float]) -> dict[str, float]:
    """
    Compute multi-period total returns vs the current last close.
    For each period, finds the earliest candle on or after the cutoff date
    and calculates (last_close - start_close) / start_close * 100.
    """
    if len(closes) < 2:
        return {}
    now_ts    = int(datetime.now(tz=timezone.utc).timestamp())
    last_close = closes[-1]
    result: dict[str, float] = {}
    for label, days in RETURN_PERIODS.items():
        cutoff_ts = now_ts - days * 86400
        for i, ts in enumerate(timestamps):
            if ts >= cutoff_ts:
                start = closes[i]
                if start > 0:
                    result[label] = round((last_close - start) / start * 100, 2)
                break
    return result


# ─── I/O ──────────────────────────────────────────────────────────────────────

def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(',', ':')))


def cleanup_orphaned_ohlc(active_symbols: set) -> None:
    """Delete any data/ohlc/<SYMBOL>/ dirs whose symbol is no longer in tickers.json."""
    ohlc_dir = DATA_DIR / 'ohlc'
    if not ohlc_dir.exists():
        return
    removed = []
    for subdir in ohlc_dir.iterdir():
        if subdir.is_dir() and subdir.name not in active_symbols:
            shutil.rmtree(subdir)
            removed.append(subdir.name)
    if removed:
        print(f'── Cleaned up {len(removed)} removed symbol(s): {", ".join(sorted(removed))}')


# ─── Core loop ────────────────────────────────────────────────────────────────

def process_symbols(symbols: list[str]) -> tuple[dict[str, dict], dict[str, float], dict[str, str]]:
    """
    For each symbol:
      - Fetch daily OHLC (1 credit) → writes 1d / 1wk / 1mo JSON files.
      - Returns ({symbol: returns}, {symbol: day_change_pct}, {symbol: name}).
    """
    ohlc_dir = DATA_DIR / 'ohlc'
    screener:     dict[str, dict]  = {}
    daily_quotes: dict[str, float] = {}
    names:        dict[str, str]   = {}
    total = len(symbols)

    for idx, symbol in enumerate(symbols, 1):
        print(f'  [{idx:>4}/{total}] {symbol}', flush=True)

        # ── Daily (+ weekly/monthly derived) ─────────────────────────────────
        data_1d = fetch_ohlc(symbol, '1day', outputsize=5000)
        time.sleep(REQUEST_DELAY)

        if data_1d is not None:
            # Capture company name from response metadata.
            name = (data_1d.get('meta') or {}).get('name', '')
            if name:
                names[symbol] = name

            ts, o, h, l, c = parse_values(data_1d)
            if ts:
                sym_dir = ohlc_dir / symbol

                write_json(sym_dir / '1d.json', {
                    's': symbol, 'u': ts[-1],
                    't': ts, 'o': o, 'h': h, 'l': l, 'c': c,
                })

                wt, wo, wh, wl, wc = to_weekly(ts, o, h, l, c)
                write_json(sym_dir / '1wk.json', {
                    's': symbol, 'u': wt[-1] if wt else 0,
                    't': wt, 'o': wo, 'h': wh, 'l': wl, 'c': wc,
                })

                mt, mo, mh, ml, mc = to_monthly(ts, o, h, l, c)
                write_json(sym_dir / '1mo.json', {
                    's': symbol, 'u': mt[-1] if mt else 0,
                    't': mt, 'o': mo, 'h': mh, 'l': ml, 'c': mc,
                })

                rets = compute_returns(ts, c)
                if rets:
                    screener[symbol] = rets

                # Day change: last close vs previous close
                if len(c) >= 2 and c[-2] > 0:
                    daily_quotes[symbol] = round((c[-1] - c[-2]) / c[-2] * 100, 2)

    return screener, daily_quotes, names


# ─── Entry point ──────────────────────────────────────────────────────────────

def load_existing_json(path: Path, default):
    """Load an existing JSON file, returning default if missing or invalid."""
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def main() -> None:
    if not API_KEY:
        print('ERROR: TWELVE_DATA_API_KEY environment variable is not set.')
        sys.exit(1)

    now_ts  = int(datetime.now(tz=timezone.utc).timestamp())
    now_str = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f'=== Bullfolio market data fetch ({MARKET}) — {now_str} ===\n')

    tickers  = json.loads(TICKERS_FILE.read_text())
    sp500    = tickers.get('sp500', [])
    nifty500 = tickers.get('nifty500', [])

    run_sp500   = MARKET in ('sp500',   'all')
    run_nifty   = MARKET in ('nifty500', 'all')

    # ── Remove OHLC data for any symbol no longer in tickers.json ─────────────
    cleanup_orphaned_ohlc(set(sp500) | set(nifty500))

    sp500_returns,   sp500_quotes,   sp500_names   = {}, {}, {}
    nifty500_returns, nifty500_quotes, nifty500_names = {}, {}, {}

    # ── S&P 500 ───────────────────────────────────────────────────────────────
    if run_sp500:
        print(f'── S&P 500 ({len(sp500)} symbols) ──')
        sp500_returns, sp500_quotes, sp500_names = process_symbols(sp500)
        write_json(DATA_DIR / 'screener' / 'sp500_returns.json', {
            'updated': now_ts,
            'data': sp500_returns,
        })
        print(f'   → screener/sp500_returns.json ({len(sp500_returns)} symbols)\n')

    # ── Nifty 500 ─────────────────────────────────────────────────────────────
    if run_nifty:
        print(f'── Nifty 500 ({len(nifty500)} symbols) ──')
        nifty500_returns, nifty500_quotes, nifty500_names = process_symbols(nifty500)
        write_json(DATA_DIR / 'screener' / 'nifty500_returns.json', {
            'updated': now_ts,
            'data': nifty500_returns,
        })
        print(f'   → screener/nifty500_returns.json ({len(nifty500_returns)} symbols)\n')

    # ── Daily quotes — merge with existing so a partial run doesn't wipe the other market ──
    existing_quotes = load_existing_json(DATA_DIR / 'daily_quotes.json', {}).get('data', {})
    merged_quotes   = {**existing_quotes, **sp500_quotes, **nifty500_quotes}
    write_json(DATA_DIR / 'daily_quotes.json', {
        'updated': now_ts,
        'data': merged_quotes,
    })
    new_count = len(sp500_quotes) + len(nifty500_quotes)
    print(f'   → daily_quotes.json ({new_count} updated, {len(merged_quotes)} total)\n')

    # ── Symbol search index — merge with existing for the same reason ──────────
    existing_symbols = {e['s']: e['n'] for e in load_existing_json(DATA_DIR / 'symbols.json', [])}
    new_names        = {**sp500_names, **nifty500_names}
    existing_symbols.update(new_names)
    all_symbols      = set(sp500 + nifty500)
    symbols_list     = [{'s': sym, 'n': existing_symbols.get(sym, '')}
                        for sym in (sp500 + nifty500) if sym in all_symbols]
    write_json(DATA_DIR / 'symbols.json', symbols_list)
    print(f'   → symbols.json ({len(symbols_list)} symbols)\n')

    # ── Manifest ──────────────────────────────────────────────────────────────
    existing_meta = load_existing_json(DATA_DIR / 'meta.json', {})
    write_json(DATA_DIR / 'meta.json', {
        'updated':        now_ts,
        'sp500_count':    len(sp500_returns)   if run_sp500 else existing_meta.get('sp500_count',   0),
        'nifty500_count': len(nifty500_returns) if run_nifty else existing_meta.get('nifty500_count', 0),
    })

    print('=== Done ===')


if __name__ == '__main__':
    main()

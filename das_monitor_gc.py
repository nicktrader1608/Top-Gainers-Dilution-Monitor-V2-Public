"""
Gap & Crap — Trade Setup Monitor
---------------------------------
Monitors DAS/thinkorswim for ticker changes. Shows Ask Edgar dilution data
plus Gap & Crap short setup classification with exact price levels.

Based on Ask Edgar Dilution Monitor V2 with Gap & Crap trade panel added.
"""

import os
import ctypes
# Set AppUserModelID so Windows taskbar shows our icon, not Python's
# Must use unicode string and set argtypes for proper Windows API call
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
    ctypes.c_wchar_p("smallcap.monitor.1")
)
import threading
import time
import webbrowser
import requests
import tkinter as tk
import win32gui
import re
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor


class RateLimiter:
    """Thread-safe rate limiter: max `calls` per `period` seconds."""
    def __init__(self, calls: int, period: float):
        self._calls = calls
        self._period = period
        self._timestamps: deque = deque()
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            # Purge timestamps outside the window
            while self._timestamps and self._timestamps[0] <= now - self._period:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._calls:
                sleep_until = self._timestamps[0] + self._period
                wait_time = sleep_until - now
                if wait_time > 0:
                    self._lock.release()
                    time.sleep(wait_time)
                    self._lock.acquire()
                    now = time.monotonic()
                    while self._timestamps and self._timestamps[0] <= now - self._period:
                        self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


# Ask Edgar: 50 req/min limit — keep safe margin at 45
_askedgar_limiter = RateLimiter(calls=45, period=60.0)

# Ask Edgar response cache — avoids re-fetching the same ticker data.
# Key: (url, ticker), Value: (epoch_timestamp, response_dict)
# Persisted to disk so SCM restarts don't wipe the cache. Each fresh AskEdgar fetch
# costs ~$0.02-0.40 depending on news payload size, so cache continuity is real money.
_askedgar_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
_ASKEDGAR_CACHE_TTL = 1800  # 30 minutes

# Balance tracking — every fresh AskEdgar response includes
# `usage.credits_remaining_dollars`. We surface that in the window title and warn
# below the threshold so Nick notices before running out mid-trade.
_LOW_BALANCE_THRESHOLD_DOLLARS = 5.0
_last_credits_remaining: float | None = None
_balance_listener = None  # type: ignore[var-annotated]  # Callable[[float], None] | None


def _parse_credits_remaining(usage_value: object) -> float | None:
    """Coerce the API's credits_remaining_dollars value to a float.

    API has been observed to return either '$16.84', '16.84', or 16.84.
    Returns None on any parse failure rather than throwing.
    """
    if usage_value is None:
        return None
    if isinstance(usage_value, (int, float)):
        return float(usage_value)
    try:
        return float(str(usage_value).lstrip("$").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _askedgar_cache_file_path() -> str:
    """Resolve cache file path — next to script in dev, next to exe under PyInstaller."""
    import sys as _sys
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(_app_dir):  # PyInstaller __file__ → temp dir that may not exist
        _app_dir = os.path.dirname(os.path.abspath(_sys.argv[0]))
    return os.path.join(_app_dir, "askedgar_session_cache.json")


def _load_askedgar_cache() -> None:
    """Load cache from disk on startup, pruning expired entries."""
    import json as _json
    path = _askedgar_cache_file_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = _json.load(f)
    except Exception as e:
        print(f"[CACHE] Failed to load disk cache ({e}) — starting fresh")
        return
    now = time.time()
    loaded = pruned = 0
    for entry in payload.get("entries", []):
        try:
            key_str = entry["key"]
            url, _, ticker = key_str.partition("||")
            ts = float(entry["ts"])
            if now - ts < _ASKEDGAR_CACHE_TTL:
                _askedgar_cache[(url, ticker)] = (ts, entry["data"])
                loaded += 1
            else:
                pruned += 1
        except (KeyError, ValueError, TypeError):
            continue
    print(f"[CACHE] Loaded {loaded} live entries from disk ({pruned} expired and pruned)")


def _save_askedgar_cache() -> None:
    """Persist cache to disk atomically. Called after each successful fresh fetch."""
    import json as _json
    path = _askedgar_cache_file_path()
    now = time.time()
    entries = []
    for (url, ticker), (ts, data) in _askedgar_cache.items():
        if now - ts < _ASKEDGAR_CACHE_TTL:  # only persist still-live entries
            entries.append({"key": f"{url}||{ticker}", "ts": ts, "data": data})
    payload = {"saved_at": now, "ttl_seconds": _ASKEDGAR_CACHE_TTL, "entries": entries}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[CACHE] Failed to save disk cache: {e}")


_load_askedgar_cache()

# Load .env file — canonical Claude/.env first (single source of truth on Nick's
# machine), then fall back to local .env for PyInstaller distribution to other users.
# Critical: under PyInstaller, __file__ resolves to a temp _MEIxxxxx dir, so we
# must ALSO try the canonical lookup relative to the exe's location (argv[0]).
try:
    from dotenv import load_dotenv
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    _argv0_dir = os.path.dirname(os.path.abspath(os.sys.argv[0]))
    # Resolution order (canonical first in BOTH source and PyInstaller modes):
    # 1. Source mode: SCM/../.env  → Claude/.env (canonical)
    # 2. PyInstaller mode: dist/../../.env  → Claude/.env (canonical)
    # 3. Source mode: SCM/.env (legacy local — should not exist on Nick's machine)
    # 4. PyInstaller mode: dist/../.env  → SCM/.env (legacy local fallback)
    # 5. PyInstaller mode: dist/.env (last-resort fallback for distribution to other users)
    _candidates = [
        os.path.normpath(os.path.join(_app_dir, "..", ".env")),
        os.path.normpath(os.path.join(_argv0_dir, "..", "..", ".env")),
        os.path.join(_app_dir, ".env"),
        os.path.normpath(os.path.join(_argv0_dir, "..", ".env")),
        os.path.join(_argv0_dir, ".env"),
    ]
    for _env_path in _candidates:
        if os.path.exists(_env_path):
            load_dotenv(_env_path)
            print(f"[ENV] Loaded: {_env_path}")
            break
    else:
        print("[ENV] WARNING: No .env file found in any of:", _candidates)
except ImportError:
    pass

# ── Config ──────────────────────────────────────────────────────────────────
# API keys – set these as environment variables or in a .env file
# See .env.example for details
ASKEDGAR_API_KEY = os.environ.get("ASKEDGAR_API_KEY", "")

DILUTION_API_URL = "https://eapi.askedgar.io/v1/dilution-rating"
DILUTION_API_KEY = ASKEDGAR_API_KEY
FLOAT_API_URL = "https://eapi.askedgar.io/v1/float-outstanding"
FLOAT_API_KEY = ASKEDGAR_API_KEY
NEWS_API_URL = "https://eapi.askedgar.io/v1/news-basic"  # short version (headlines only, no article body) — Nick clicks through to source. ~4-6x cheaper than /v1/news for news-heavy tickers. Verified 2026-05-06: same summary/title/grok/jmt415 fields, only `body`+`channels`+`tags` omitted.
NEWS_API_KEY = ASKEDGAR_API_KEY
DILDATA_API_URL = "https://eapi.askedgar.io/v1/dilution-data"
DILDATA_API_KEY = ASKEDGAR_API_KEY
SCREENER_API_URL = "https://eapi.askedgar.io/v1/screener"
SCREENER_API_KEY = ASKEDGAR_API_KEY
CHART_ANALYSIS_URL = "https://eapi.askedgar.io/v1/ai-chart-analysis"
CHART_ANALYSIS_KEY = ASKEDGAR_API_KEY
GAP_STATS_URL = "https://eapi.askedgar.io/v1/gap-stats"
GAP_STATS_KEY = ASKEDGAR_API_KEY
OFFERINGS_API_URL = "https://eapi.askedgar.io/v1/offerings"
OFFERINGS_API_KEY = ASKEDGAR_API_KEY
OWNERSHIP_API_URL = "https://eapi.askedgar.io/v1/ownership"
OWNERSHIP_API_KEY = ASKEDGAR_API_KEY
POLL_INTERVAL = 1.0

# FMP (Financial Modeling Prep) API
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FMP_GAINERS_URL = "https://financialmodelingprep.com/stable/biggest-gainers"
FMP_QUOTE_URL = "https://financialmodelingprep.com/stable/quote"
GAINERS_REFRESH_SECS = 30

if not ASKEDGAR_API_KEY:
    print("WARNING: ASKEDGAR_API_KEY not set. Dilution data will be unavailable.")
    print("  Request trial at https://www.askedgar.io/api-trial")
if not FMP_API_KEY:
    print("INFO: FMP_API_KEY not set. Using TradingView for live quotes (no FMP needed).")

# Ticker filter: 2-4 uppercase letters, no periods or special chars
TICKER_RE = re.compile(r'^[A-Z]{2,4}$')

# ── Visual Style ────────────────────────────────────────────────────────────
BG = "#0D1014"
BG_CARD = "#151A20"
BG_ROW = "#1B2128"
BG_ROW_ALT = "#181D24"
BG_SELECTED = "#1A2A3A"
BORDER = "#232A33"
BORDER_INNER = "#20262E"
BORDER_ACCENT = "#63D3FF"
FG = "#E6EAF0"
FG_DIM = "#8B949E"
FG_INFO = "#B7C0CC"
ACCENT = "#63D3FF"
GREEN = "#4CAF50"
RED = "#FF4444"

RISK_BG = {
    "High": "#A93232",
    "Medium": "#B96A16",
    "Low": "#2F7D57",
    "N/A": "#4A525C",
}

# Chart history rating: API color -> (label, badge color)
HISTORY_MAP = {
    "green":  ("Strong", "#2F7D57"),
    "yellow": ("Semi-Strong", "#B9A816"),
    "orange": ("Mixed",      "#B96A16"),
    "red":    ("Fader",  "#A93232"),
}

# Fonts
FONT_UI = ("Segoe UI", 10)
FONT_UI_BOLD = ("Segoe UI Semibold", 10)
FONT_HEADER = ("Segoe UI Semibold", 13)
FONT_TICKER = ("Segoe UI Semibold", 24)
FONT_MONO = ("Consolas", 9)
FONT_MONO_BOLD = ("Consolas", 9, "bold")
FONT_GAINER_TICKER = ("Segoe UI Semibold", 11)
FONT_GAINER_PCT = ("Consolas", 10, "bold")
FONT_GAINER_DETAIL = ("Consolas", 8)

LEFT_PANEL_WIDTH = 195


def risk_bg(level: str) -> str:
    return RISK_BG.get(level, "#555555")


def fmt_millions(val) -> str:
    if val is None:
        return "N/A"
    m = val / 1_000_000
    if m >= 1:
        return f"{m:.1f}M"
    return f"{val / 1000:.0f}K"


def fmt_volume(val) -> str:
    """Format volume with K/M suffix."""
    if val is None or val == 0:
        return "0"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.0f}K"
    return str(int(val))


def fmt_price(val) -> str:
    """Format price with appropriate decimal places."""
    if val is None or val == 0:
        return "$0.00"
    if val >= 1:
        return f"${val:.2f}"
    return f"${val:.4f}"


# ── Window Monitor ──────────────────────────────────────────────────────────
def find_montage_windows() -> dict[int, str]:
    """Return {hwnd: ticker} for all visible DAS montage and chart windows."""
    windows = {}

    def enum_callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        # DAS montage: "TICKER     0 -- 0     Company Name..."
        if re.match(r'^[A-Z]{1,5}\s+\d', title):
            windows[hwnd] = title.split()[0]
        # DAS chart: "TICKER--5 Minute--"
        elif re.match(r'^[A-Z]{1,5}--', title):
            windows[hwnd] = title.split('--')[0]

    win32gui.EnumWindows(enum_callback, None)
    return windows


def find_tos_tickers() -> dict[int, list[str]]:
    """Return {hwnd: [tickers]} for thinkorswim chart windows."""
    windows = {}

    def enum_callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        # "PRSO, MOBX, TURB - Charts - 61612650SCHW Main@thinkorswim [build 1990]"
        if "thinkorswim" in title and " - Charts - " in title:
            ticker_part = title.split(" - Charts - ")[0]
            tickers = [t.strip() for t in ticker_part.split(",") if t.strip()]
            if tickers:
                windows[hwnd] = tickers

    win32gui.EnumWindows(enum_callback, None)
    return windows


# ── Market Data APIs ────────────────────────────────────────────────────────
def _is_premarket() -> bool:
    """Return True if current time is before 9:30 AM Eastern."""
    from datetime import datetime, timezone, timedelta
    eastern = timezone(timedelta(hours=-4))  # EDT (summer), close enough for trading
    now_et = datetime.now(eastern)
    return now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30)


def _tv_cookies():
    """Build TradingView cookie jar for real-time data."""
    jar = requests.cookies.RequestsCookieJar()
    tv_session = os.environ.get("TRADINGVIEW_SESSION_ID", "")
    if tv_session:
        jar.set("sessionid", tv_session, domain=".tradingview.com")
    return jar


def fetch_top_gainers_raw() -> list[dict]:
    """Fetch top gainers from TradingView (real-time with session cookie).
    Works both pre-market and regular hours. Falls back to FMP if TV fails."""
    min_pct = 30
    try:
        from tradingview_screener import Query, col
    except ImportError:
        print("[Gainers] tradingview-screener not installed. Run: pip install tradingview-screener")
        return _fetch_fmp_gainers_fallback()

    try:
        cookies = _tv_cookies()
        # Pre-market: use premarket_change. Regular hours: use change (intraday)
        if _is_premarket():
            min_pm_vol = 50_000
            _, df = (Query()
                .select("name", "close", "premarket_change", "premarket_close",
                        "premarket_volume", "volume", "market_cap_basic")
                .where(
                    col("premarket_change") > min_pct,
                    col("premarket_volume") > min_pm_vol,
                )
                .order_by("premarket_change", ascending=False)
                .limit(30)
                .get_scanner_data(cookies=cookies))
            pct_col, price_col, vol_col = "premarket_change", "premarket_close", "premarket_volume"
        else:
            min_vol = 500_000
            _, df = (Query()
                .select("name", "close", "change", "volume", "market_cap_basic")
                .where(col("change") > min_pct, col("volume") > min_vol)
                .order_by("change", ascending=False)
                .limit(30)
                .get_scanner_data(cookies=cookies))
            pct_col, price_col, vol_col = "change", "close", "volume"

        source = "TradingView-PM" if _is_premarket() else "TradingView"
        print(f"[Gainers] {source}: {len(df)} tickers (>={min_pct}%)")
    except Exception as e:
        print(f"[Gainers] TradingView error: {e} — falling back to FMP")
        return _fetch_fmp_gainers_fallback()

    filtered = []
    for _, row in df.iterrows():
        ticker = row.get("name", "")
        if not TICKER_RE.match(ticker):
            continue
        pct = row.get(pct_col) or 0
        price = row.get(price_col) or row.get("close") or 0
        volume = int(row.get(vol_col) or row.get("volume") or 0)
        mcap = row.get("market_cap_basic") or 0
        # Filter out large caps
        if mcap and mcap >= 500_000_000:
            continue
        filtered.append({
            "ticker": ticker,
            "todaysChangePerc": pct,
            "day": {"c": price, "v": volume},
            "name": "",
            "_tv_mcap": mcap,
        })

    filtered.sort(key=lambda x: x.get("todaysChangePerc", 0), reverse=True)
    return filtered[:30]


def _fetch_fmp_gainers_fallback() -> list[dict]:
    """Fallback: fetch gainers from FMP if TradingView fails."""
    try:
        resp = requests.get(FMP_GAINERS_URL, params={"apikey": FMP_API_KEY}, timeout=15)
        raw = resp.json()
    except Exception as e:
        print(f"[Gainers] FMP fallback error: {e}")
        return []
    filtered = []
    for item in raw:
        symbol = item.get("symbol", "")
        if not TICKER_RE.match(symbol): continue
        pct = item.get("changesPercentage", 0)
        if pct < 30: continue
        filtered.append({
            "ticker": symbol, "todaysChangePerc": pct,
            "day": {"c": item.get("price", 0), "v": 0}, "name": item.get("name", ""),
        })
    return filtered


# ── Ask Edgar APIs ──────────────────────────────────────────────────────────
def _askedgar_get(url: str, params: dict, label: str = "") -> dict | None:
    """Make a rate-limited Ask Edgar GET with automatic 429 retry and response caching."""
    # Check cache first
    ticker = params.get("ticker", "")
    cache_key = (url, ticker)
    cached = _askedgar_cache.get(cache_key)
    if cached:
        cached_time, cached_response = cached
        if time.time() - cached_time < _ASKEDGAR_CACHE_TTL:
            print(f"  [cache hit] {label}")
            return cached_response

    for attempt in range(3):
        _askedgar_limiter.wait()
        try:
            resp = requests.get(
                url,
                headers={"API-KEY": ASKEDGAR_API_KEY, "Content-Type": "application/json"},
                params=params,
                timeout=15,
            )
            if resp.status_code == 429:
                retry_after = resp.json().get("error", {}).get("details", {}).get("retry_after", 20)
                print(f"Ask Edgar 429 for {label or url} — waiting {retry_after}s (attempt {attempt+1}/3)")
                time.sleep(retry_after)
                continue
            result = resp.json()
            # Track remaining balance from usage block — surfaced in window title.
            global _last_credits_remaining
            usage = result.get("usage", {}) if isinstance(result, dict) else {}
            parsed_balance = _parse_credits_remaining(usage.get("credits_remaining_dollars"))
            if parsed_balance is not None:
                _last_credits_remaining = parsed_balance
                if _balance_listener is not None:
                    try:
                        _balance_listener(parsed_balance)
                    except Exception as e:
                        print(f"[BALANCE] listener failed: {e}")
            # Cache successful responses + persist to disk so SCM restarts keep them
            if ticker:
                _askedgar_cache[cache_key] = (time.time(), result)
                _save_askedgar_cache()
            return result
        except Exception as e:
            print(f"Ask Edgar error for {label}: {e}")
            return None
    print(f"Ask Edgar gave up after 3 retries for {label}")
    return None


def fetch_dilution_data(ticker: str) -> dict | None:
    data = _askedgar_get(DILUTION_API_URL, {"ticker": ticker, "offset": 0, "limit": 10}, f"dilution/{ticker}")
    if data and data.get("status") == "success" and data.get("results"):
        return data["results"][0]
    return None


def fetch_float_data(ticker: str) -> dict | None:
    data = _askedgar_get(FLOAT_API_URL, {"ticker": ticker, "offset": 0, "limit": 100}, f"float/{ticker}")
    if data and data.get("status") == "success" and data.get("results"):
        return data["results"][0]
    return None


def fetch_news_and_grok(ticker: str) -> tuple[list[dict], str | None, str | None, str | None, list[dict]]:
    """Fetch recent news/8-K/6-K (top 2), latest grok, and all jmt415 notes."""
    headlines = []
    grok_line = None
    grok_date = None
    grok_url = None
    jmt415_notes = []
    data = _askedgar_get(NEWS_API_URL, {"ticker": ticker, "offset": 0, "limit": 100}, f"news/{ticker}")
    if data and data.get("status") == "success":
        for r in data.get("results", []):
            ft = r.get("form_type")
            if ft in ("news", "8-K", "6-K") and len(headlines) < 2:
                headlines.append(r)
            if ft == "grok" and grok_line is None:
                summary = r.get("summary", "")
                for line in summary.split("\n"):
                    line = line.strip().lstrip("-").strip()
                    if line:
                        grok_line = line
                        break
                grok_date = r.get("created_at") or r.get("filed_at", "")
                grok_url = r.get("url") or r.get("document_url")
            if ft == "jmt415" and len(jmt415_notes) < 3:
                jmt415_notes.append(r)
    return headlines, grok_line, grok_date, grok_url, jmt415_notes


def fetch_last_price(ticker: str) -> float | None:
    """Fetch last price via Ask Edgar screener endpoint."""
    data = _askedgar_get(SCREENER_API_URL, {"ticker": ticker}, f"price/{ticker}")
    if data and data.get("status") == "success" and data.get("results"):
        return data["results"][0].get("price")
    return None


def fetch_reverse_splits(ticker: str) -> dict | None:
    """Fetch reverse split history from Ask Edgar. Returns most recent split or None."""
    data = _askedgar_get("https://eapi.askedgar.io/v1/reverse-splits",
                         {"ticker": ticker, "limit": 3}, f"rsplit/{ticker}")
    if data and data.get("status") == "success" and data.get("results"):
        return data["results"][0]  # Most recent
    return None


def fetch_short_interest(ticker: str) -> dict | None:
    """Fetch short interest from Ask Edgar screener (same endpoint, cached)."""
    data = _askedgar_get(SCREENER_API_URL, {"ticker": ticker}, f"price/{ticker}")
    if data and data.get("status") == "success" and data.get("results"):
        r = data["results"][0]
        si_pct = r.get("short_float")
        si_shares = r.get("short_interest")
        dtc = r.get("days_to_cover")
        if si_pct is not None:
            return {"short_pct": si_pct, "short_shares": si_shares, "days_to_cover": dtc}
    return None


def fetch_in_play_dilution(ticker: str) -> tuple[list[dict], list[dict], float]:
    """Fetch dilution-data and split into in-play warrants and convertibles.
    Returns (warrants, convertibles, stock_price) filtered by price proximity and registration."""
    price = fetch_last_price(ticker)
    if price is None or price <= 0:
        return [], [], 0.0

    max_price = price * 4

    data = _askedgar_get(DILDATA_API_URL, {"ticker": ticker, "offset": 0, "limit": 40}, f"dildata/{ticker}")
    if not data or data.get("status") != "success":
        return [], [], price

    warrants = []
    convertibles = []
    from datetime import datetime, timedelta
    six_months_ago = datetime.now() - timedelta(days=180)

    for item in data.get("results", []):
        registered = item.get("registered") or ""
        details_lower = (item.get("details") or "").lower()
        is_warrant = "warrant" in details_lower or "option" in details_lower

        # Skip "Not Registered" items, but override for convertibles filed >6 months ago
        skip_not_registered = "Not Registered" in registered
        if skip_not_registered and not is_warrant:
            filed_at_str = (item.get("filed_at") or "")[:10]
            if filed_at_str:
                try:
                    if datetime.strptime(filed_at_str, "%Y-%m-%d") < six_months_ago:
                        skip_not_registered = False
                except ValueError:
                    pass
        if skip_not_registered:
            continue

        if is_warrant and item.get("warrants_exercise_price"):
            if item["warrants_exercise_price"] <= max_price:
                remaining = item.get("warrants_remaining", 0) or 0
                if remaining > 0:
                    warrants.append(item)
        elif not is_warrant and item.get("conversion_price"):
            if item["conversion_price"] <= max_price:
                remaining = item.get("underlying_shares_remaining", 0) or 0
                if remaining > 0:
                    convertibles.append(item)

    return warrants, convertibles, price


def fetch_gap_stats(ticker: str) -> list[dict]:
    """Fetch gap-up stats for a ticker. Returns list of gap entries (date descending)."""
    data = _askedgar_get(GAP_STATS_URL, {"ticker": ticker, "page": 1, "limit": 100}, f"gapstats/{ticker}")
    if data and data.get("status") == "success":
        return data.get("results", [])
    return []


def fetch_offerings(ticker: str) -> list[dict]:
    """Fetch recent offerings for the ticker (up to 5)."""
    data = _askedgar_get(OFFERINGS_API_URL, {"ticker": ticker, "limit": 5}, f"offerings/{ticker}")
    if data and data.get("status") == "success":
        return data.get("results", [])
    return []


def fetch_ownership(ticker: str) -> dict | None:
    """Fetch ownership data – returns the latest reported_date group, or None."""
    data = _askedgar_get(OWNERSHIP_API_URL, {"ticker": ticker, "limit": 100}, f"ownership/{ticker}")
    if data and data.get("status") == "success" and data.get("results"):
        return data["results"][0]
    return None


def extract_headline(item: dict) -> str:
    if item.get("title"):
        return item["title"]
    summary = item.get("summary", "")
    if summary.startswith("HEADLINE:"):
        return summary.split("HEADLINE:")[1].split("\n")[0].strip()
    return f"{item.get('form_type', '')} Filing"


# ── Load Setup Config ────────────────────────────────────────────────────
# All setup parameters are in setup_config.json — edit that file when
# backtesting produces new values. No code changes needed.
# Config file: look next to exe first, then next to script
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(os.sys.argv[0])), "setup_config.json")
if not os.path.exists(_CONFIG_FILE):
    _CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_config.json")
_CONFIG = {}
try:
    with open(_CONFIG_FILE, "r") as _f:
        _CONFIG = json.load(_f)
    print(f"[CONFIG] Loaded setup_config.json (updated: {_CONFIG.get('_updated', '?')})")
except Exception as _e:
    print(f"[CONFIG] WARNING: Could not load setup_config.json: {_e}")
    print("[CONFIG] Using hardcoded defaults.")

# ── Gap & Crap Setup Parameters (from config) ───────────────────────────
_gc_cfg = _CONFIG.get("gap_and_crap", {})
GC_MIN_GAP = _gc_cfg.get("min_gap_pct", 40)
GC_REQUIRE_H2PMH = _gc_cfg.get("require_h2pmh_below_zero", True)
GC_STOP_PCT = _gc_cfg.get("stop_pct", 75)
GC_BE_PCT = _gc_cfg.get("be_trail_pct", 20)
GC_T1_PCT = _gc_cfg.get("t1_pct", 24)
GC_T2_PCT = _gc_cfg.get("t2_pct", 30)
GC_T3_PCT = _gc_cfg.get("t3_pct", 36)
GC_GAP_TIERS = _gc_cfg.get("gap_tiers", [
    {"tier": "A", "name": "EXTREME", "gap_min": 200, "gap_max": 99999},
    {"tier": "B", "name": "MID GAP", "gap_min": 100, "gap_max": 200},
    {"tier": "C", "name": "SMALL GAP", "gap_min": 60, "gap_max": 100},
    {"tier": "D", "name": "LOW GAP", "gap_min": 40, "gap_max": 60},
])
GC_SIZING = _gc_cfg.get("position_sizing", [
    {"sizing": "FULL", "float_min": 0, "float_max": 10_000_000},
    {"sizing": "HALF", "float_min": 10_000_000, "float_max": 50_000_000},
    {"sizing": "QUARTER", "float_min": 50_000_000, "float_max": 999_999_999_999},
])

def classify_gap_crap(price: float, prev_close: float, pm_high: float | None,
                      shares_float: float | None) -> dict | None:
    """Classify a ticker into a Gap & Crap setup. All params from setup_config.json."""
    if not price or not prev_close or prev_close <= 0:
        return None
    gap_pct = (price - prev_close) / prev_close * 100
    if gap_pct < GC_MIN_GAP:
        return None

    # Check H2PMH
    h2pmh = None
    qualifies_h2pmh = True
    h2pmh_status = "UNKNOWN"
    if pm_high and pm_high > 0:
        h2pmh = (price - pm_high) / pm_high * 100
        if GC_REQUIRE_H2PMH and h2pmh >= 0:
            qualifies_h2pmh = False
            h2pmh_status = "ABOVE"
        else:
            h2pmh_status = "BELOW"

    # Gap tier (from config)
    tier, tier_name = "?", "UNKNOWN"
    for t in GC_GAP_TIERS:
        if gap_pct > t["gap_min"]:
            tier, tier_name = t["tier"], t["name"]
            break  # First match wins (tiers are sorted highest first)

    # Position sizing by float (from config)
    sizing, sizing_detail = "UNKNOWN", "Float data unavailable"
    if shares_float and shares_float > 0:
        for s in GC_SIZING:
            if s["float_min"] <= shares_float < s["float_max"]:
                sizing = s["sizing"]
                if sizing == "FULL":
                    sizing_detail = f"Float {shares_float/1e6:.1f}M < {s['float_max']/1e6:.0f}M"
                elif sizing == "QUARTER":
                    sizing_detail = f"Float {shares_float/1e6:.1f}M > {s['float_min']/1e6:.0f}M"
                else:
                    sizing_detail = f"Float {shares_float/1e6:.1f}M ({s['float_min']/1e6:.0f}-{s['float_max']/1e6:.0f}M)"
                break

    entry = price
    return {
        "qualifies": qualifies_h2pmh, "tier": tier, "tier_name": tier_name,
        "gap_pct": round(gap_pct, 1), "h2pmh": round(h2pmh, 1) if h2pmh is not None else None,
        "h2pmh_status": h2pmh_status, "pm_high": pm_high,
        "sizing": sizing, "sizing_detail": sizing_detail,
        "entry": round(entry, 4), "prev_close": prev_close,
        "stop": round(entry * (1 + GC_STOP_PCT / 100), 4),
        "be_trigger": round(entry * (1 - GC_BE_PCT / 100), 4),
        "t1": round(entry * (1 - GC_T1_PCT / 100), 4),
        "t2": round(entry * (1 - GC_T2_PCT / 100), 4),
        "t3": round(entry * (1 - GC_T3_PCT / 100), 4),
    }


# ── PM Short Setup Parameters (from config) ─────────────────────────────
_pm_cfg = _CONFIG.get("pm_short", {}).get("setups", {})
# Fallback defaults if config missing
_PM_DEFAULTS = {
    "A": {"name": "Micro Cap Fade", "gap_min": 30, "gap_max": 75, "float_max": 5000000, "mcap_max": 25000000, "pm_vol_max": None, "price_max": None, "avg_fade_is": -18.6, "avg_fade_oos": -19.4, "avg_bounce": 23.8, "first_drop": -11.6, "lower_high": -6.8, "lh_confirmed": 76.1, "time_to_lh": "3-4 min", "vol_declining": 41.7, "full_fade": 19.0, "cost_of_waiting": 8.7, "remaining_profit": 10.5, "fade_5": 95.2, "fade_10": 83.6, "fade_15": 68.9, "fade_20": 46.8},
    "B": {"name": "Low Float Runner", "gap_min": 75, "gap_max": 125, "float_max": 10000000, "mcap_max": 50000000, "pm_vol_max": 10000000, "price_max": None, "avg_fade_is": -27.6, "avg_fade_oos": -31.1, "avg_bounce": 29.5, "first_drop": -13.6, "lower_high": -8.1, "lh_confirmed": 80.7, "time_to_lh": "4 min", "vol_declining": 36.6, "full_fade": 28.4, "cost_of_waiting": 9.8, "remaining_profit": 19.1, "fade_10": 90.1, "fade_15": 85.0, "fade_20": 77.6, "fade_25": 66.4},
    "C": {"name": "Mid Float Parabolic", "gap_min": 125, "gap_max": 200, "float_max": 10000000, "mcap_max": 50000000, "pm_vol_max": 10000000, "price_max": None, "avg_fade_is": -35.7, "avg_fade_oos": -38.4, "avg_bounce": 34.0, "first_drop": -17.2, "lower_high": -10.5, "lh_confirmed": 85.1, "time_to_lh": "4 min", "vol_declining": 46.2, "full_fade": 36.5, "cost_of_waiting": 12.2, "remaining_profit": 24.4, "fade_10": 95.7, "fade_15": 90.8, "fade_20": 87.2, "fade_25": 81.6},
    "D": {"name": "Extreme Gapper", "gap_min": 200, "gap_max": 99999, "float_max": 20000000, "mcap_max": 100000000, "pm_vol_max": None, "price_max": 5.0, "avg_fade_is": -35.3, "avg_fade_oos": -43.9, "avg_bounce": 43.2, "first_drop": -18.5, "lower_high": -11.2, "lh_confirmed": 86.2, "time_to_lh": "3-4 min", "vol_declining": 47.1, "full_fade": 38.9, "cost_of_waiting": 12.9, "remaining_profit": 25.3, "fade_10": 90.4, "fade_15": 82.1, "fade_20": 73.0, "fade_25": 64.2},
}
PM_SHORT_SETUPS = {}
for _k, _default in _PM_DEFAULTS.items():
    PM_SHORT_SETUPS[_k] = _pm_cfg.get(_k, _default)


def classify_pm_short(gap_pct: float, shares_float: float | None,
                      mcap: float | None, pm_volume: float | None,
                      price: float | None, pm_high: float | None = None,
                      h2pmh: float | None = None) -> dict | None:
    """Classify a ticker into PM Short setup A/B/C/D. Returns setup dict or None.
    When no setup matches, returns dict with 'reject_reason' explaining why."""
    if gap_pct < 30:
        return {"reject_reason": f"Gap +{gap_pct:.0f}% < 30% — no setup"}

    # Find which setup the gap range falls into, then check criteria
    reject_reasons = []
    for tier_key in ("D", "C", "B", "A"):  # Check highest gap first
        s = PM_SHORT_SETUPS[tier_key]
        if not (s["gap_min"] <= gap_pct < s["gap_max"]):
            continue
        # Gap matches this tier — check other criteria
        if shares_float and shares_float > 0 and shares_float > s["float_max"]:
            reject_reasons.append(f"Float {shares_float/1e6:.1f}M > {s['float_max']/1e6:.0f}M")
        if mcap and mcap > 0 and mcap > s["mcap_max"]:
            reject_reasons.append(f"MCap {mcap/1e6:.0f}M > {s['mcap_max']/1e6:.0f}M")
        if s["pm_vol_max"] and pm_volume is not None and pm_volume > s["pm_vol_max"]:
            reject_reasons.append(f"PMVol {pm_volume/1e6:.0f}M > {s['pm_vol_max']/1e6:.0f}M")
        if s["price_max"] and price is not None and price > s["price_max"]:
            reject_reasons.append(f"Price ${price:.2f} > ${s['price_max']:.0f}")
        if not shares_float and s["float_max"] < 20_000_000:
            reject_reasons.append("Float unknown")

        if reject_reasons:
            return {"reject_reason": ", ".join(reject_reasons) + " — no setup"}

        # All criteria pass — match found
        result = {"tier": tier_key, **s}
        result["actual_gap"] = round(gap_pct, 1)
        result["actual_float"] = shares_float
        result["actual_mcap"] = mcap
        result["actual_pm_vol"] = pm_volume
        result["actual_price"] = price
        result["actual_pm_high"] = pm_high
        result["actual_h2pmh"] = h2pmh
        return result

    return {"reject_reason": f"Gap +{gap_pct:.0f}% — no matching range"}


def fetch_tv_quote(ticker: str) -> dict | None:
    """Fetch real-time quote data from TradingView for a single ticker.
    Returns dict with: price, high, prev_close, market_cap, volume,
    premarket_high, premarket_close, premarket_volume.
    Returns None on error."""
    try:
        from tradingview_screener import Query, col
    except ImportError:
        return None
    try:
        cookies = _tv_cookies()
        _, df = (Query()
            .select("name", "close", "open", "high", "low", "volume",
                    "market_cap_basic", "change",
                    "premarket_change", "premarket_close", "premarket_high",
                    "premarket_volume")
            .where(col("name") == ticker)
            .limit(1)
            .get_scanner_data(cookies=cookies))
        if len(df) == 0:
            return None
        row = df.iloc[0]
        close_price = row.get("close", 0) or 0
        change_pct = row.get("change", 0) or 0
        prev_close = close_price / (1 + change_pct / 100) if change_pct else 0
        return {
            "price": close_price,
            "previousClose": prev_close,
            "dayHigh": row.get("high", 0) or 0,
            "open": row.get("open", 0) or 0,
            "marketCap": row.get("market_cap_basic", 0) or 0,
            "volume": row.get("volume", 0) or 0,
            "premarket_high": row.get("premarket_high") or None,
            "premarket_close": row.get("premarket_close") or None,
            "premarket_volume": row.get("premarket_volume") or None,
        }
    except Exception as e:
        print(f"[TV Quote] Error for {ticker}: {e}")
        return None


def fetch_fmp_pm_data(ticker: str) -> tuple[float | None, float | None]:
    """Get premarket high AND latest trade price via TradingView.
    Returns (pm_high, last_price). Uses TV premarket_high + premarket_close."""
    q = fetch_tv_quote(ticker)
    if q:
        pm_high = q.get("premarket_high")
        pm_price = q.get("premarket_close")
        return pm_high, pm_price
    return None, None


def fetch_fmp_pm_high_from_quote(ticker: str) -> float | None:
    """Fallback: get PM high or open price from TradingView."""
    q = fetch_tv_quote(ticker)
    if q:
        pm_high = q.get("premarket_high")
        if pm_high and pm_high > 0:
            return pm_high
        open_price = q.get("open")
        if open_price and open_price > 0:
            return open_price
    return None


# ── Overlay UI ──────────────────────────────────────────────────────────────
class DilutionOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self._base_title = "Small Cap Monitor"
        self.root.title(self._base_title)
        # Register the balance listener so fresh AskEdgar responses bubble up
        # to the title bar. Using globals keeps _askedgar_get free of any
        # reference to the overlay class (it's a module-level helper).
        global _balance_listener
        _balance_listener = self._on_balance_update
        # If a previous session already wrote a balance into the cache, surface
        # it immediately on launch so the title isn't blank.
        if _last_credits_remaining is not None:
            self.root.after(100, self._render_title_with_balance, _last_credits_remaining)
        self.root.attributes("-topmost", True)
        self.root.attributes("-toolwindow", False)
        self.root.configure(bg=BG)
        self.root.resizable(True, True)
        # Restore saved window position/size, or use default
        self._geo_file = os.path.join(os.path.dirname(os.path.abspath(os.sys.argv[0])), ".gc_geometry")
        default_geo = "780x700+50+50"
        try:
            if os.path.exists(self._geo_file):
                with open(self._geo_file, "r") as f:
                    saved = f.read().strip()
                    if saved:
                        default_geo = saved
        except Exception:
            pass
        self.root.geometry(default_geo)
        self.root.minsize(650, 400)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Set window/taskbar icon
        icon_dir = os.path.dirname(os.path.abspath(__file__))
        ico_path = os.path.join(icon_dir, "gc_icon.ico")
        if not os.path.exists(ico_path):
            ico_path = os.path.join(icon_dir, "app_icon.ico")
        if os.path.exists(ico_path):
            # Method 1: tkinter iconbitmap (window title bar)
            self.root.iconbitmap(default=ico_path)
            # Method 2: wm_iconphoto (taskbar — most reliable for tkinter)
            png_path = os.path.join(icon_dir, "gc_icon.png")
            if not os.path.exists(png_path):
                png_path = os.path.join(icon_dir, "app_icon.png")
            if os.path.exists(png_path):
                try:
                    icon_img = tk.PhotoImage(file=png_path)
                    self.root.wm_iconphoto(True, icon_img)
                    self._icon_ref = icon_img  # prevent garbage collection
                except Exception as e:
                    print(f"wm_iconphoto failed: {e}")
            # Method 3: Win32 API (force taskbar icon via SendMessage)
            self.root.update_idletasks()
            try:
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x00000010
                user32 = ctypes.windll.user32
                # Set proper arg/return types for 64-bit Windows
                user32.LoadImageW.restype = ctypes.c_void_p
                user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                 ctypes.c_void_p, ctypes.c_void_p]
                user32.GetParent.restype = ctypes.c_void_p
                user32.GetParent.argtypes = [ctypes.c_void_p]
                hicon_big = user32.LoadImageW(0, ico_path, IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
                hicon_small = user32.LoadImageW(0, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                hwnd = user32.GetParent(self.root.winfo_id())
                if hwnd and hicon_big:
                    user32.SendMessageW(hwnd, 0x0080, 1, hicon_big)   # WM_SETICON, ICON_BIG
                    user32.SendMessageW(hwnd, 0x0080, 0, hicon_small)  # WM_SETICON, ICON_SMALL
            except Exception as e:
                print(f"Win32 icon set failed: {e}")

        self._drag_data = {"x": 0, "y": 0}
        self.current_ticker = None
        self._known_windows: dict[int, str] = {}   # DAS: hwnd -> ticker
        self._known_tos: dict[int, list[str]] = {}  # ToS: hwnd -> [tickers]
        self._gainers_data: list[dict] = []
        self._selected_gainer: str | None = None
        self._pm_high_cache: dict[str, float] = {}  # ticker -> PM high (persists across session)
        self._float_cache: dict[str, float] = {}  # ticker -> float shares (from Ask Edgar)
        self._gc_refresh_id = None  # after() ID for auto-refresh cancellation
        self._gc_card_frame = None  # reference to the GC card for in-place updates
        self._pm_short_card_frame = None  # reference to PM Short card for in-place updates
        self._build_ui()
        self._start_monitor()
        self._schedule_gainers_refresh()
        self._cache_pm_highs_from_gainers()

    def _build_ui(self):
        # ── Search bar (top, full width) ──
        search_frame = tk.Frame(self.root, bg=BG_CARD,
                                highlightbackground=BORDER, highlightthickness=1)
        search_frame.pack(fill="x", padx=8, pady=(8, 0))

        search_inner = tk.Frame(search_frame, bg=BG_CARD, padx=10, pady=8)
        search_inner.pack(fill="x")
        search_inner.bind("<Button-1>", self._start_drag)
        search_inner.bind("<B1-Motion>", self._on_drag)

        tk.Label(search_inner, text="TICKER:", fg=FG_DIM, bg=BG_CARD,
                 font=FONT_UI_BOLD).pack(side="left", padx=(0, 6))

        self.search_entry = tk.Entry(
            search_inner, bg=BG_ROW, fg=FG, insertbackground=FG,
            font=FONT_UI_BOLD, width=10, relief="flat",
            highlightbackground=BORDER, highlightthickness=1,
        )
        self.search_entry.pack(side="left", padx=(0, 6), ipady=3)
        self.search_entry.bind("<Return>", self._on_search)

        go_btn = tk.Label(
            search_inner, text="  GO  ", fg=BG, bg=ACCENT,
            font=FONT_UI_BOLD, padx=8, pady=2, cursor="hand2",
        )
        go_btn.pack(side="left")
        go_btn.bind("<Button-1>", self._on_search)

        title_lbl = tk.Label(search_inner, text="Small Cap Monitor",
                             fg=FG_DIM, bg=BG_CARD, font=FONT_UI)
        title_lbl.pack(side="right")
        title_lbl.bind("<Button-1>", self._start_drag)
        title_lbl.bind("<B1-Motion>", self._on_drag)

        # ── Main body (left + right) ──
        main_body = tk.Frame(self.root, bg=BG)
        main_body.pack(fill="both", expand=True)

        # ── Left panel (gainers) ──
        left_panel = tk.Frame(main_body, bg=BG, width=LEFT_PANEL_WIDTH)
        left_panel.pack(side="left", fill="y", padx=(8, 0), pady=(6, 8))
        left_panel.pack_propagate(False)

        # Gainers header
        gh_frame = tk.Frame(left_panel, bg=BG_CARD,
                            highlightbackground=BORDER, highlightthickness=1)
        gh_frame.pack(fill="x")

        gh_inner = tk.Frame(gh_frame, bg=BG_CARD, padx=10, pady=8)
        gh_inner.pack(fill="x")

        tk.Label(gh_inner, text="TOP GAINERS", fg=ACCENT, bg=BG_CARD,
                 font=FONT_HEADER).pack(side="left")

        self._gainers_status = tk.Label(gh_inner, text="", fg=FG_DIM, bg=BG_CARD,
                                        font=FONT_MONO)
        self._gainers_status.pack(side="right")

        refresh_btn = tk.Label(gh_inner, text=" \u21bb ", fg=ACCENT, bg=BG_CARD,
                               font=("Segoe UI", 14), cursor="hand2")
        refresh_btn.pack(side="right", padx=(0, 4))
        refresh_btn.bind("<Button-1>", lambda e: self._trigger_gainers_refresh())

        # Gainers scrollable list
        gainers_container = tk.Frame(left_panel, bg=BG)
        gainers_container.pack(fill="both", expand=True, pady=(2, 0))

        self._gainers_canvas = tk.Canvas(gainers_container, bg=BG,
                                         highlightthickness=0,
                                         width=LEFT_PANEL_WIDTH - 16)
        gainers_sb = tk.Scrollbar(gainers_container, orient="vertical",
                                  command=self._gainers_canvas.yview)
        self._gainers_frame = tk.Frame(self._gainers_canvas, bg=BG)

        self._gainers_frame.bind(
            "<Configure>",
            lambda e: self._gainers_canvas.configure(
                scrollregion=self._gainers_canvas.bbox("all")
            ),
        )
        self._gainers_canvas_window = self._gainers_canvas.create_window(
            (0, 0), window=self._gainers_frame, anchor="nw"
        )
        self._gainers_canvas.configure(yscrollcommand=gainers_sb.set)

        def _on_gainers_canvas_resize(event):
            self._gainers_canvas.itemconfig(self._gainers_canvas_window,
                                            width=event.width)
        self._gainers_canvas.bind("<Configure>", _on_gainers_canvas_resize)

        self._gainers_canvas.pack(side="left", fill="both", expand=True)
        gainers_sb.pack(side="right", fill="y")

        # ── Right panel (Ask Edgar content) ──
        right_panel = tk.Frame(main_body, bg=BG)
        right_panel.pack(side="left", fill="both", expand=True,
                         padx=(4, 8), pady=(6, 8))

        # Header card (draggable)
        header_card = tk.Frame(right_panel, bg=BG_CARD,
                               highlightbackground=BORDER, highlightthickness=1)
        header_card.pack(fill="x")
        header_card.bind("<Button-1>", self._start_drag)
        header_card.bind("<B1-Motion>", self._on_drag)

        header_inner = tk.Frame(header_card, bg=BG_CARD, padx=14, pady=12)
        header_inner.pack(fill="x")
        header_inner.bind("<Button-1>", self._start_drag)
        header_inner.bind("<B1-Motion>", self._on_drag)

        self.ticker_label = tk.Label(
            header_inner, text="Waiting...", fg=ACCENT,
            bg=BG_CARD, font=FONT_TICKER,
        )
        self.ticker_label.pack(side="left")

        self.overall_badge = tk.Label(
            header_inner, text="", fg="white", bg="#4A525C",
            font=FONT_UI_BOLD, padx=12, pady=6,
        )
        self.overall_badge.pack(side="right")

        self.history_badge = tk.Label(
            header_inner, text="", fg="white", bg="#4A525C",
            font=FONT_UI_BOLD, padx=12, pady=6,
        )
        self.history_badge.pack(side="right", padx=(0, 6))
        self.history_badge.pack_forget()  # hidden until data loaded

        self.info_label = tk.Label(
            header_card, text="", fg=FG_INFO, bg=BG_CARD,
            font=FONT_UI, anchor="w",
        )
        self.info_label.pack(fill="x", padx=14, pady=(0, 10))

        # Scrollable content area
        container = tk.Frame(right_panel, bg=BG)
        container.pack(fill="both", expand=True, pady=(4, 0))

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.content_frame = tk.Frame(canvas, bg=BG)

        self.content_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        self._canvas_window = canvas.create_window(
            (0, 0), window=self.content_frame, anchor="nw"
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        def _on_canvas_resize(event):
            canvas.itemconfig(self._canvas_window, width=event.width)
        canvas.bind("<Configure>", _on_canvas_resize)

        # Mouse wheel scrolling — route to correct panel based on cursor position
        def _on_mousewheel(event):
            x = event.x_root - self.root.winfo_rootx()
            if x < LEFT_PANEL_WIDTH + 12:
                self._gainers_canvas.yview_scroll(
                    int(-1 * (event.delta / 120)), "units"
                )
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.canvas = canvas

        self._show_waiting()

    # ── Display states ──────────────────────────────────────────────────────
    def _clear(self):
        for w in self.content_frame.winfo_children():
            w.destroy()

    def _show_waiting(self):
        self._clear()
        tk.Label(
            self.content_frame,
            text="Load a ticker in DAS or thinkorswim,\n"
                 "click a top gainer, or search above.",
            fg="#4A525C", bg=BG, font=("Segoe UI", 12), justify="center",
        ).pack(pady=60)

    def _update_history_badge(self, rating: str, post_url: str = ""):
        """Update the history badge in the header."""
        if rating in HISTORY_MAP:
            label, color = HISTORY_MAP[rating]
            self.history_badge.config(text=f"HISTORY: {label}", bg=color)
            self.history_badge.pack(side="right", padx=(0, 6))
            if post_url:
                self.history_badge.config(cursor="hand2")
                self.history_badge.bind("<Button-1>", lambda e, u=post_url: webbrowser.open(u))
            else:
                self.history_badge.config(cursor="")
                self.history_badge.unbind("<Button-1>")
        else:
            self.history_badge.pack_forget()

    def _show_loading(self, ticker: str):
        self._clear()
        self.ticker_label.config(text=ticker)
        self.overall_badge.config(text="...", bg="#4A525C")
        self.history_badge.pack_forget()
        self.info_label.config(text="Loading...")
        tk.Label(
            self.content_frame,
            text=f"Fetching data for {ticker}...",
            fg=ACCENT, bg=BG, font=("Segoe UI", 12),
        ).pack(pady=60)
        self.root.update_idletasks()

    def _show_no_data(self, ticker: str):
        self._clear()
        self.overall_badge.config(text="NO DATA", bg="#4A525C")
        self.info_label.config(text="")
        tk.Label(
            self.content_frame,
            text=f"No dilution data available for {ticker}.",
            fg="#FF6666", bg=BG, font=("Segoe UI", 11), justify="center",
        ).pack(pady=20)

    def _show_no_data_gc(self, ticker: str, gc_data: dict | None,
                         pm_short_data: dict | None = None):
        """Show no dilution data + PM Short + Gap & Crap panels."""
        self._show_no_data(ticker)
        # Top row: PM Short (left) + Gap & Crap (right)
        top_row = tk.Frame(self.content_frame, bg=BG)
        top_row.pack(fill="x", padx=8, pady=(6, 0))

        pm_outer = tk.Frame(top_row, bg=BG_CARD,
                            highlightbackground=BORDER, highlightthickness=1, width=320)
        pm_outer.pack(side="left", fill="y", padx=(0, 3))
        pm_outer.pack_propagate(False)
        tk.Label(pm_outer, text="PM Short", fg=ACCENT, bg=BG_CARD,
                 font=FONT_HEADER, anchor="w", padx=14, pady=8).pack(fill="x")
        tk.Frame(pm_outer, bg=BORDER, height=1).pack(fill="x")
        self._pm_short_card_frame = pm_outer
        self._build_pm_short_body(pm_outer, pm_short_data)

        gc_outer = tk.Frame(top_row, bg=BG_CARD,
                            highlightbackground=BORDER, highlightthickness=1)
        gc_outer.pack(side="left", fill="both", expand=True, padx=(3, 0))
        tk.Label(gc_outer, text="Gap & Crap", fg=ACCENT, bg=BG_CARD,
                 font=FONT_HEADER, anchor="w", padx=14, pady=8).pack(fill="x")
        tk.Frame(gc_outer, bg=BORDER, height=1).pack(fill="x")
        self._gc_card_frame = gc_outer
        self._build_gc_body(gc_outer, gc_data)
        self._start_gc_refresh()

    def _make_card(self, parent, title: str = None) -> tk.Frame:
        """Create a bordered card frame, optionally with a section header."""
        card = tk.Frame(parent, bg=BG_CARD,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=8, pady=(6, 0))
        if title:
            hdr = tk.Label(card, text=title, fg=ACCENT, bg=BG_CARD,
                           font=FONT_HEADER, anchor="w", padx=14, pady=10)
            hdr.pack(fill="x")
            tk.Frame(card, bg=BORDER, height=1).pack(fill="x")
        return card

    def _show_data(self, ticker: str, dilution: dict, floatdata: dict | None,
                   news: list[dict] | None = None, grok_line: str | None = None,
                   grok_date: str | None = None, grok_url: str | None = None,
                   in_play_warrants: list[dict] | None = None,
                   in_play_converts: list[dict] | None = None,
                   stock_price: float = 0.0,
                   jmt415_notes: list[dict] | None = None,
                   gap_stats: list[dict] | None = None,
                   offerings: list[dict] | None = None,
                   ownership: dict | None = None,
                   gc_data: dict | None = None,
                   pm_short_data: dict | None = None):
        self._clear()

        risk = dilution.get("overall_offering_risk", "N/A")
        self.overall_badge.config(text=f"RISK: {risk}", bg=risk_bg(risk))

        # ── Info line from float data ──
        if floatdata:
            flt = fmt_millions(floatdata.get("float"))
            outs = fmt_millions(floatdata.get("outstanding"))
            mc = fmt_millions(floatdata.get("market_cap_final"))
            sector = floatdata.get("sector", "")
            country = floatdata.get("country", "")
            # Institutional Ownership + Short Interest + Reverse Split
            inst = floatdata.get("institutions_percent")
            io_str = f"  IO: {inst*100:.0f}%" if inst is not None else ""
            si_data = fetch_short_interest(ticker)
            si_str = ""
            if si_data and si_data.get("short_pct") is not None:
                si_str = f"  SI: {si_data['short_pct']*100:.1f}%"
            # Reverse split warning — upcoming/recent
            rs_str = ""
            rs_data = fetch_reverse_splits(ticker)
            if rs_data and rs_data.get("execution_date"):
                from datetime import datetime
                try:
                    rs_date = datetime.strptime(rs_data["execution_date"][:10], "%Y-%m-%d")
                    days_diff = (rs_date - datetime.now()).days  # positive = future
                    ratio = f"{int(rs_data.get('split_from',0))}:1"
                    date_str = rs_data['execution_date'][:10]
                    if days_diff >= 0:
                        rs_str = f"  R/S {ratio} on {date_str}"
                    elif days_diff >= -180:
                        rs_str = f"  R/S {ratio} ({date_str})"
                except Exception:
                    pass
            self.info_label.config(
                text=f"Float/OS: {flt}/{outs}  |  MC: {mc}{si_str}  |  {sector}  |  {country}"
            )
            # Second info line: IO (color-coded) + R/S badge
            info2_frame = tk.Frame(self.content_frame, bg=BG_CARD)
            info2_frame.pack(fill="x", padx=14, pady=(0, 4))

            # IO with color coding
            if inst is not None:
                io_pct = inst * 100
                if io_pct < 5:
                    io_color = "#4CAF50"  # Green
                elif io_pct <= 15:
                    io_color = "#FF9800"  # Orange
                else:
                    io_color = "#FF4444"  # Red
                tk.Label(info2_frame, text=f"IO: {io_pct:.0f}%", fg=io_color, bg=BG_CARD,
                         font=FONT_UI).pack(side="left")

            # R/S badge
            if rs_str:
                from datetime import datetime
                rs_date = datetime.strptime(rs_data["execution_date"][:10], "%Y-%m-%d")
                days_diff = (rs_date - datetime.now()).days
                if days_diff >= 0:
                    rs_color, rs_bg = "white", "#FF0000"
                elif days_diff >= -30:
                    rs_color, rs_bg = "white", "#FF6600"
                else:
                    rs_color, rs_bg = "#FFD600", BG_CARD
                tk.Label(info2_frame, text=f"  {rs_str.strip()}", fg=rs_color, bg=rs_bg,
                         font=FONT_UI, padx=4).pack(side="left")
        else:
            self.info_label.config(text="")

        # ── Top row: PM Short (left) + Gap & Crap (right) side by side ──
        top_row = tk.Frame(self.content_frame, bg=BG)
        top_row.pack(fill="x", padx=8, pady=(6, 0))

        # Left: PM Short card (fixed width)
        pm_outer = tk.Frame(top_row, bg=BG_CARD,
                            highlightbackground=BORDER, highlightthickness=1, width=320)
        pm_outer.pack(side="left", fill="y", padx=(0, 3))
        pm_outer.pack_propagate(False)
        pm_hdr = tk.Label(pm_outer, text="PM Short", fg=ACCENT, bg=BG_CARD,
                          font=FONT_HEADER, anchor="w", padx=14, pady=8)
        pm_hdr.pack(fill="x")
        tk.Frame(pm_outer, bg=BORDER, height=1).pack(fill="x")
        self._pm_short_card_frame = pm_outer
        self._build_pm_short_body(pm_outer, pm_short_data)

        # Right: Gap & Crap card (expanding)
        gc_outer = tk.Frame(top_row, bg=BG_CARD,
                            highlightbackground=BORDER, highlightthickness=1)
        gc_outer.pack(side="left", fill="both", expand=True, padx=(3, 0))
        gc_hdr = tk.Label(gc_outer, text="Gap & Crap", fg=ACCENT, bg=BG_CARD,
                          font=FONT_HEADER, anchor="w", padx=14, pady=8)
        gc_hdr.pack(fill="x")
        tk.Frame(gc_outer, bg=BORDER, height=1).pack(fill="x")
        self._gc_card_frame = gc_outer
        self._build_gc_body(gc_outer, gc_data)
        self._start_gc_refresh()

        # ── News & Catalyst (below top row) ──
        has_feed = news or grok_line
        feed_card = tk.Frame(self.content_frame, bg=BG_CARD,
                             highlightbackground=BORDER, highlightthickness=1)
        feed_card.pack(fill="x", padx=8, pady=(6, 0))
        feed_hdr = tk.Label(feed_card, text="News & Catalyst", fg=ACCENT, bg=BG_CARD,
                            font=FONT_HEADER, anchor="w", padx=14, pady=8)
        feed_hdr.pack(fill="x")
        tk.Frame(feed_card, bg=BORDER, height=1).pack(fill="x")

        if has_feed:
            feed_inner = tk.Frame(feed_card, bg=BG_CARD, padx=6, pady=6)
            feed_inner.pack(fill="x")
            # Horizontal layout for news items
            feed_row = tk.Frame(feed_inner, bg=BG_CARD)
            feed_row.pack(fill="x")

            if news:
                for item in news:
                    headline = extract_headline(item)
                    url = item.get("url") or item.get("document_url")
                    form = item.get("form_type", "")
                    raw_date = item.get("created_at") or item.get("filed_at", "")
                    date = raw_date[:16].replace("T", " ")
                    self._add_feed_item(feed_inner, form, headline, url, date)

            if grok_line:
                grok_date_str = ""
                if grok_date:
                    grok_date_str = grok_date[:16].replace("T", " ")
                self._add_feed_item(feed_inner, "grok", grok_line, grok_url, grok_date_str)
        else:
            tk.Label(feed_card, text="No news available", fg=FG_DIM, bg=BG_CARD,
                     font=FONT_UI, padx=14, pady=20).pack(anchor="w")

        # ── Risk + Offering Ability + Recent Offerings — 3 equal columns ──
        dilution_url = f"https://app.askedgar.io/ticker/{ticker}/dilution"
        offering_desc = dilution.get("offering_ability_desc")

        # Use grid layout for equal columns
        triple_card = tk.Frame(self.content_frame, bg=BG_CARD,
                               highlightbackground=BORDER, highlightthickness=1)
        triple_card.pack(fill="x", padx=8, pady=(6, 0))
        triple_card.columnconfigure((0, 2, 4), weight=1, uniform="col")  # 3 equal data columns
        triple_card.columnconfigure((1, 3), minsize=1)  # separator columns

        # ── Column 0: Risk Badges ──
        c0 = tk.Frame(triple_card, bg=BG_CARD)
        c0.grid(row=0, column=0, sticky="nsew")
        tk.Label(c0, text="Risk", fg=ACCENT, bg=BG_CARD,
                 font=FONT_HEADER, anchor="w", padx=8, pady=6).pack(fill="x")
        tk.Frame(c0, bg=BORDER, height=1).pack(fill="x")
        bi = tk.Frame(c0, bg=BG_CARD, padx=6, pady=8, cursor="hand2")
        bi.pack(fill="both", expand=True)
        bi.bind("<Button-1>", lambda e, u=dilution_url: webbrowser.open(u))
        badge_items = [
            ("Overall", risk),
            ("Offering", dilution.get("offering_ability", "N/A")),
            ("Dilution", dilution.get("dilution", "N/A")),
            ("Frequency", dilution.get("offering_frequency", "N/A")),
            ("Cash Need", dilution.get("cash_need", "N/A")),
            ("Warrants", dilution.get("warrant_exercise", "N/A")),
        ]
        for i, (label, level) in enumerate(badge_items):
            self._add_badge_grid(bi, label, level, dilution_url,
                                 row=i // 2, col=i % 2)
        bi.columnconfigure((0, 1), weight=1)

        # Separator 1
        tk.Frame(triple_card, bg=BORDER, width=1).grid(row=0, column=1, sticky="ns")

        # ── Column 2: Offering Ability ──
        c2 = tk.Frame(triple_card, bg=BG_CARD)
        c2.grid(row=0, column=2, sticky="nsew")
        tk.Label(c2, text="Offering Ability", fg=ACCENT, bg=BG_CARD,
                 font=FONT_HEADER, anchor="w", padx=8, pady=6).pack(fill="x")
        tk.Frame(c2, bg=BORDER, height=1).pack(fill="x")
        ob = tk.Frame(c2, bg=BG_CARD, padx=8, pady=6)
        ob.pack(fill="both", expand=True)
        if offering_desc:
            for part in [p.strip() for p in offering_desc.split(",")]:
                pl = part.lower()
                if "pending s-1" in pl or "pending f-1" in pl:
                    c, b = "#4CAF50", True
                elif any(x in pl for x in ["shelf capacity", "atm capacity", "equity line"]):
                    c = "#FF4444" if "$0.00" in part else "#4CAF50"
                    b = "$0.00" not in part
                else:
                    c, b = FG, False
                tk.Label(ob, text=part, fg=c, bg=BG_CARD,
                         font=FONT_MONO_BOLD if b else FONT_MONO,
                         anchor="w").pack(fill="x", pady=1)
            self._bind_card_click(c2, dilution_url)
        else:
            tk.Label(ob, text="No data", fg=FG_DIM, bg=BG_CARD,
                     font=FONT_MONO).pack(anchor="w")

        # Separator 2
        tk.Frame(triple_card, bg=BORDER, width=1).grid(row=0, column=3, sticky="ns")

        # ── Column 4: Recent Offerings ──
        c4 = tk.Frame(triple_card, bg=BG_CARD)
        c4.grid(row=0, column=4, sticky="nsew")
        tk.Label(c4, text="Recent Offerings", fg=ACCENT, bg=BG_CARD,
                 font=FONT_HEADER, anchor="w", padx=8, pady=6).pack(fill="x")
        tk.Frame(c4, bg=BORDER, height=1).pack(fill="x")
        ob3 = tk.Frame(c4, bg=BG_CARD, padx=6, pady=6)
        ob3.pack(fill="both", expand=True)
        if offerings:
            for i, item in enumerate(offerings[:3]):
                rb = BG_ROW if i % 2 == 0 else BG_ROW_ALT
                r = tk.Frame(ob3, bg=rb, highlightbackground=BORDER_INNER, highlightthickness=1)
                r.pack(fill="x", pady=1)
                ri = tk.Frame(r, bg=rb, padx=6, pady=3)
                ri.pack(fill="x")
                dt = (item.get("filed_at") or "")[:10]
                desc = item.get("offering_type") or item.get("details") or "Offering"
                if len(desc) > 28: desc = desc[:25] + "..."
                tk.Label(ri, text=f"{dt}  {desc}", fg=FG, bg=rb,
                         font=FONT_MONO, anchor="w").pack(fill="x")
                pv = item.get("offering_price") or item.get("price", 0)
                sh = item.get("shares_offered") or item.get("total_shares", 0)
                if pv:
                    tk.Label(ri, text=f"${pv:.2f}  |  {fmt_millions(sh)} shares",
                             fg="#FF9800", bg=rb, font=FONT_MONO_BOLD).pack(anchor="w")
            self._bind_card_click(c4, dilution_url)
        else:
            tk.Label(ob3, text="None", fg=FG_DIM, bg=BG_CARD,
                     font=FONT_MONO).pack(anchor="w")

        # ── In Play Dilution card ──
        if in_play_warrants or in_play_converts:
            self._add_in_play_section(in_play_warrants or [], in_play_converts or [], stock_price, dilution_url)

        # ── Gap Stats card ──
        if gap_stats:
            self._add_gap_stats_card(gap_stats)

        # ── JMT415 Previous Notes card ──
        if jmt415_notes:
            self._add_jmt415_card(jmt415_notes)

        # ── Management Commentary card ──
        commentary = dilution.get("mgmt_commentary")
        if commentary:
            self._add_section_card("Mgmt Commentary", commentary, url=dilution_url)

        # ── Ownership card ──
        if ownership and ownership.get("owners"):
            self._add_ownership_card(ownership)

    def _add_badge_grid(self, parent, label: str, level: str,
                        url: str | None = None, row: int = 0, col: int = 0):
        """Place a badge in a grid layout (3 columns, rows wrap automatically)."""
        frame = tk.Frame(parent, bg=BG_CARD, padx=4, pady=4, cursor="hand2")
        frame.grid(row=row, column=col, padx=4, pady=2, sticky="ew")

        lbl = tk.Label(
            frame, text=label, fg=FG_DIM, bg=BG_CARD,
            font=FONT_MONO, cursor="hand2",
        )
        lbl.pack()

        badge = tk.Label(
            frame, text=f" {level} ", fg="white", bg=risk_bg(level),
            font=FONT_UI_BOLD, padx=8, pady=3, cursor="hand2",
        )
        badge.pack()

        if url:
            for w in (frame, lbl, badge):
                w.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    def _add_feed_item(self, parent, form_type: str, headline: str,
                       url: str | None, date: str = ""):
        """Feed row with source stripe on the left. Entire row is clickable."""
        SOURCE_COLORS = {
            "news": "#1F8FB3",
            "8-K": "#A85C14",
            "6-K": "#A85C14",
            "grok": "#7B3FA0",
        }
        source_color = SOURCE_COLORS.get(form_type, "#555555")
        tag = form_type.upper() if form_type != "news" else "NEWS"

        # Truncate grok output to ~240 chars
        if form_type == "grok" and len(headline) > 240:
            headline = headline[:237] + "..."

        row = tk.Frame(parent, bg=BG_ROW,
                       highlightbackground=BORDER_INNER, highlightthickness=1)
        row.pack(fill="x", pady=2)

        # Source stripe (left column)
        stripe = tk.Label(
            row, text=tag, fg="white", bg=source_color,
            font=("Consolas", 8, "bold"), width=5, padx=4, pady=8,
        )
        stripe.pack(side="left", fill="y")

        # Content area — stacked vertically so text wraps downward
        content = tk.Frame(row, bg=BG_ROW, padx=10, pady=6)
        content.pack(side="left", fill="both", expand=True)

        if date:
            tk.Label(
                content, text=date, fg=FG_DIM, bg=BG_ROW,
                font=FONT_MONO, anchor="w",
            ).pack(fill="x")

        hl_label = tk.Label(
            content, text=headline, fg="white", bg=BG_ROW,
            font=FONT_UI_BOLD, anchor="w", wraplength=200,
            justify="left",
        )
        hl_label.pack(fill="x")

        def _rewrap_hl(event, lbl=hl_label):
            lbl.config(wraplength=max(event.width - 30, 100))
        content.bind("<Configure>", _rewrap_hl)

        # Make entire row clickable if there's a URL
        if url:
            row.config(cursor="hand2")
            def _bind_click(widget, target_url):
                widget.bind("<Button-1>", lambda e, u=target_url: webbrowser.open(u))
                widget.config(cursor="hand2")
            for w in (row, stripe, content, hl_label):
                _bind_click(w, url)
            for child in content.winfo_children():
                _bind_click(child, url)

    def _bind_card_click(self, card, url: str):
        """Make an entire card and all its descendants clickable."""
        def _bind(w, u=url):
            w.bind("<Button-1>", lambda e, u=u: webbrowser.open(u))
            w.config(cursor="hand2")
        def _bind_all(widget):
            _bind(widget)
            for child in widget.winfo_children():
                _bind_all(child)
        _bind_all(card)

    def _add_section_card(self, title: str, text: str, url: str = ""):
        """Section card with header + bottom border + wrapped text content."""
        card = self._make_card(self.content_frame, title=title)
        body = tk.Frame(card, bg=BG_CARD, padx=14, pady=14)
        body.pack(fill="x")
        text_label = tk.Label(
            body, text=text, fg=FG, bg=BG_CARD,
            font=FONT_UI, justify="left", anchor="w",
        )
        text_label.pack(fill="x")
        def _rewrap(event, lbl=text_label):
            lbl.config(wraplength=max(event.width - 4, 100))
        body.bind("<Configure>", _rewrap)
        if url:
            self._bind_card_click(card, url)

    def _add_offering_ability_card(self, desc: str, url: str = ""):
        """Offering Ability card with color-coded capacity values."""
        card = self._make_card(self.content_frame, title="Offering Ability")
        body = tk.Frame(card, bg=BG_CARD, padx=14, pady=14)
        body.pack(fill="x")

        # Parse and color individual segments — stacked vertically
        parts = [p.strip() for p in desc.split(",")]

        for part in parts:
            part_lower = part.lower()
            if "pending s-1" in part_lower or "pending f-1" in part_lower:
                color = "#4CAF50"
                bold = True
            elif ("shelf capacity" in part_lower or "atm capacity" in part_lower
                  or "equity line capacity" in part_lower):
                if "$0.00" in part:
                    color = "#FF4444"
                    bold = False
                else:
                    color = "#4CAF50"
                    bold = True
            else:
                color = FG
                bold = False

            font = ("Segoe UI Semibold", 10) if bold else FONT_UI
            tk.Label(
                body, text=part, fg=color, bg=BG_CARD,
                font=font, anchor="w",
            ).pack(fill="x")

        if url:
            self._bind_card_click(card, url)

    def _add_gap_stats_card(self, gaps: list[dict]):
        """Gap Stats summary card."""
        from datetime import datetime
        card = self._make_card(self.content_frame, title="Gap Stats")
        body = tk.Frame(card, bg=BG_CARD, padx=14, pady=10)
        body.pack(fill="x")

        n = len(gaps)
        last_date = gaps[0].get("date", "N/A") if gaps else "N/A"

        # Compute averages
        gap_pcts = [g["gap_percentage"] for g in gaps if g.get("gap_percentage") is not None]
        avg_gap = sum(gap_pcts) / len(gap_pcts) if gap_pcts else 0

        oh_spikes = []
        ol_drops = []
        for g in gaps:
            o = g.get("market_open")
            h = g.get("high_price")
            lo = g.get("low_price")
            if o and o > 0:
                if h is not None:
                    oh_spikes.append((h - o) / o * 100)
                if lo is not None:
                    ol_drops.append((lo - o) / o * 100)

        avg_oh = sum(oh_spikes) / len(oh_spikes) if oh_spikes else 0
        avg_ol = sum(ol_drops) / len(ol_drops) if ol_drops else 0

        # % new high after 11am EST (high_time is already EST, e.g. "2026-03-27T12:34:00")
        high_after_11 = 0
        for g in gaps:
            ht = g.get("high_time", "")
            if ht:
                try:
                    t = datetime.fromisoformat(ht)
                    if t.hour >= 11:
                        high_after_11 += 1
                except Exception:
                    pass
        pct_high_after_11 = (high_after_11 / n * 100) if n else 0

        # % closed below VWAP (API gives closed_over_vwap boolean)
        below_vwap = sum(1 for g in gaps if g.get("closed_over_vwap") is False)
        pct_below_vwap = (below_vwap / n * 100) if n else 0

        # % closed below open
        below_open = sum(1 for g in gaps if g.get("market_close") and g.get("market_open")
                         and g["market_close"] < g["market_open"])
        pct_below_open = (below_open / n * 100) if n else 0

        # Display stats as label-value rows
        stats = [
            ("Last Gap Date", last_date),
            ("Number of Gaps", str(n)),
            ("Avg Gap %", f"{avg_gap:.1f}%"),
            ("Avg Open→High", f"+{avg_oh:.1f}%"),
            ("Avg Open→Low", f"{avg_ol:.1f}%"),
            ("New High After 11am", f"{pct_high_after_11:.0f}%"),
            ("Closed Below VWAP", f"{pct_below_vwap:.0f}%"),
            ("Closed Below Open", f"{pct_below_open:.0f}%"),
        ]

        for label, value in stats:
            row = tk.Frame(body, bg=BG_CARD)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, fg=FG_DIM, bg=BG_CARD,
                     font=FONT_MONO, width=22, anchor="w").pack(side="left")
            # Color code certain values
            ORANGE = "#B96A16"
            val_color = FG
            if "Below VWAP" in label:
                try:
                    pv = float(value.rstrip("%"))
                    val_color = GREEN if pv <= 59 else (ORANGE if pv <= 84 else RED)
                except ValueError:
                    pass
            elif "Below Open" in label:
                try:
                    pv = float(value.rstrip("%"))
                    val_color = GREEN if pv <= 50 else (ORANGE if pv <= 74 else RED)
                except ValueError:
                    pass
            elif "After 11am" in label:
                try:
                    pv = float(value.rstrip("%"))
                    val_color = GREEN if pv >= 45 else (ORANGE if pv >= 21 else RED)
                except ValueError:
                    pass
            elif "Open→High" in label:
                val_color = GREEN
            elif "Open→Low" in label:
                val_color = RED
            tk.Label(row, text=value, fg=val_color, bg=BG_CARD,
                     font=FONT_MONO_BOLD, anchor="w").pack(side="left")


    def _add_offerings_card(self, offerings: list[dict], stock_price: float = 0.0,
                            url: str = ""):
        """Recent Offerings card with headline + data row per offering."""
        card = self._make_card(self.content_frame, title="Recent Offerings")
        body = tk.Frame(card, bg=BG_CARD, padx=14, pady=10)
        body.pack(fill="x")

        for i, o in enumerate(offerings):
            row_bg = BG_ROW if i % 2 == 0 else BG_ROW_ALT

            row = tk.Frame(body, bg=row_bg,
                           highlightbackground=BORDER_INNER, highlightthickness=1)
            row.pack(fill="x", pady=2)

            inner = tk.Frame(row, bg=row_bg, padx=10, pady=6)
            inner.pack(fill="x")

            headline = (o.get("headline") or "Offering").strip()
            tk.Label(inner, text=headline, fg="white", bg=row_bg,
                     font=FONT_UI, anchor="w").pack(fill="x")

            is_atm = "ATM USED" in headline.upper()

            data_row = tk.Frame(inner, bg=row_bg)
            data_row.pack(fill="x", pady=(2, 0))

            if is_atm:
                offering_amt = o.get("offering_amount")
                if offering_amt:
                    tk.Label(data_row, text=f"${fmt_millions(offering_amt)}", fg="#4CAF50", bg=row_bg,
                             font=FONT_MONO_BOLD).pack(side="left")
                filed = (o.get("filed_at") or "")[:10]
                if filed:
                    tk.Label(data_row, text="  |  ", fg=FG_DIM, bg=row_bg,
                             font=FONT_MONO).pack(side="left")
                    tk.Label(data_row, text=filed, fg=FG_DIM, bg=row_bg,
                             font=FONT_MONO).pack(side="left")
            else:
                offer_price = o.get("share_price") or 0
                in_money = stock_price > 0 and offer_price > 0 and offer_price <= stock_price
                highlight = "#4CAF50" if in_money else "#FF9800"

                shares = o.get("shares_amount")
                warrants = o.get("warrants_amount")
                filed = (o.get("filed_at") or "")[:10]

                parts_colored = []
                if shares:
                    parts_colored.append(f"Amt:{fmt_millions(shares)}")
                if offer_price:
                    parts_colored.append(f"${offer_price:.2f}")
                if warrants:
                    parts_colored.append(f"Wrrnts:{fmt_millions(warrants)}")

                for j, part in enumerate(parts_colored):
                    if j > 0:
                        tk.Label(data_row, text=" | ", fg=FG_DIM, bg=row_bg,
                                 font=FONT_MONO).pack(side="left")
                    tk.Label(data_row, text=part, fg=highlight, bg=row_bg,
                             font=FONT_MONO_BOLD).pack(side="left")

                if filed:
                    if parts_colored:
                        tk.Label(data_row, text="  |  ", fg=FG_DIM, bg=row_bg,
                                 font=FONT_MONO).pack(side="left")
                    tk.Label(data_row, text=filed, fg=FG_DIM, bg=row_bg,
                             font=FONT_MONO).pack(side="left")

        if url:
            self._bind_card_click(card, url)

    def _add_jmt415_card(self, notes: list[dict]):
        """JMT415 Previous Notes card with bordered panels per note."""
        card = self._make_card(self.content_frame, title="JMT415 Previous Notes")
        body = tk.Frame(card, bg=BG_CARD, padx=10, pady=10)
        body.pack(fill="x")

        for i, note in enumerate(notes):
            date = (note.get("filed_at") or "")[:10]
            text = (note.get("summary") or note.get("title") or "Note").strip()
            row_bg = BG_ROW if i % 2 == 0 else BG_ROW_ALT

            row = tk.Frame(body, bg=row_bg,
                           highlightbackground=BORDER_INNER, highlightthickness=1)
            row.pack(fill="x", pady=2)

            inner = tk.Frame(row, bg=row_bg, padx=10, pady=8)
            inner.pack(fill="x")

            tk.Label(inner, text=date, fg=FG_DIM, bg=row_bg,
                     font=FONT_MONO).pack(anchor="w")
            note_label = tk.Label(inner, text=text, fg=FG, bg=row_bg,
                                  font=FONT_UI, anchor="w",
                                  wraplength=350, justify="left")
            note_label.pack(fill="x", pady=(2, 0))

            def _rewrap(event, lbl=note_label):
                lbl.config(wraplength=max(event.width - 40, 100))
            row.bind("<Configure>", _rewrap)

    def _add_ownership_card(self, ownership: dict):
        """Ownership card showing latest reported date with owner table."""
        reported_date = (ownership.get("reported_date") or "")[:10]
        title = f"Ownership  ({reported_date})" if reported_date else "Ownership"
        card = self._make_card(self.content_frame, title=title)
        body = tk.Frame(card, bg=BG_CARD, padx=10, pady=10)
        body.pack(fill="x")

        # Table header
        hdr = tk.Frame(body, bg=BG_CARD)
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="Owner", fg=ACCENT, bg=BG_CARD,
                 font=FONT_MONO_BOLD, anchor="w", width=20).pack(side="left")
        tk.Label(hdr, text="Title", fg=ACCENT, bg=BG_CARD,
                 font=FONT_MONO_BOLD, anchor="w", width=14).pack(side="left")
        tk.Label(hdr, text="Shares", fg=ACCENT, bg=BG_CARD,
                 font=FONT_MONO_BOLD, anchor="e").pack(side="right")

        doc_url = ""
        owners = ownership.get("owners", [])
        for i, owner in enumerate(owners):
            row_bg = BG_ROW if i % 2 == 0 else BG_ROW_ALT
            row = tk.Frame(body, bg=row_bg)
            row.pack(fill="x", pady=1)
            inner = tk.Frame(row, bg=row_bg, padx=6, pady=4)
            inner.pack(fill="x")

            name = owner.get("owner_name", "")
            title_str = owner.get("title", "") or owner.get("owner_type", "")
            shares = owner.get("common_shares_amount", 0)
            shares_str = f"{shares:,.0f}" if shares else "0"

            tk.Label(inner, text=name, fg=FG, bg=row_bg,
                     font=FONT_MONO, anchor="w", width=20).pack(side="left")
            tk.Label(inner, text=title_str, fg=FG_DIM, bg=row_bg,
                     font=FONT_MONO, anchor="w", width=14).pack(side="left")
            tk.Label(inner, text=shares_str, fg="#4CAF50", bg=row_bg,
                     font=FONT_MONO_BOLD, anchor="e").pack(side="right")

            if not doc_url:
                doc_url = owner.get("document_url", "")

        if doc_url:
            self._bind_card_click(card, doc_url)

    def _start_gc_refresh(self):
        """Auto-refresh the Gap & Crap panel every 5 seconds."""
        # Cancel any existing refresh timer
        if self._gc_refresh_id:
            self.root.after_cancel(self._gc_refresh_id)
            self._gc_refresh_id = None

        if not self.current_ticker:
            return

        def _refresh():
            ticker = self.current_ticker
            if not ticker:
                return

            def _fetch():
                try:
                    pm_high = self._pm_high_cache.get(ticker)
                    tv_quote = fetch_tv_quote(ticker)

                    if not pm_high and tv_quote:
                        pm_h = tv_quote.get("premarket_high")
                        if pm_h and pm_h > 0:
                            self._pm_high_cache[ticker] = pm_h
                            pm_high = pm_h

                    if not pm_high and tv_quote:
                        day_high = tv_quote.get("dayHigh")
                        if day_high and day_high > 0:
                            pm_high = day_high

                    # Pre-market: derive prev_close from gainer change%; regular: use TV quote
                    gainer_item = next((g for g in self._gainers_data
                                        if (g.get("ticker") or g.get("symbol")) == ticker), None)
                    if _is_premarket() and gainer_item:
                        current_price = tv_quote.get("price", 0) if tv_quote else 0
                        gainer_pct = gainer_item.get("todaysChangePerc", 0)
                        if current_price and gainer_pct > 0:
                            prev_close = current_price / (1 + gainer_pct / 100)
                        else:
                            prev_close = 0
                    elif tv_quote:
                        current_price = tv_quote.get("price", 0)
                        prev_close = tv_quote.get("previousClose", 0)
                    else:
                        current_price = 0
                        prev_close = 0

                    if current_price and prev_close:
                        ae_float = self._float_cache.get(ticker)
                        gc_data = classify_gap_crap(
                            current_price,
                            prev_close,
                            pm_high,
                            ae_float,
                        )
                        if gc_data:
                            pm_source = "live" if ticker in self._pm_high_cache else "open_proxy"
                            gc_data["pm_source"] = pm_source
                        self.root.after(0, self._update_gc_card, gc_data)

                        # Also refresh PM Short classification
                        gap_pct = (current_price - prev_close) / prev_close * 100
                        mcap_val = tv_quote.get("marketCap") if tv_quote else None
                        pm_vol = gainer_item.get("day", {}).get("v", 0) if gainer_item else (
                            tv_quote.get("volume", 0) if tv_quote else 0)
                        pm_h2pmh = ((current_price - pm_high) / pm_high * 100) if pm_high and pm_high > 0 else None
                        pm_short_data = classify_pm_short(
                            gap_pct, ae_float, mcap_val, pm_vol, current_price,
                            pm_high, pm_h2pmh
                        )
                        self.root.after(0, self._update_pm_short_card, pm_short_data)
                except Exception as e:
                    print(f"[GC] Refresh error: {e}")

            threading.Thread(target=_fetch, daemon=True).start()
            # Schedule next refresh
            self._gc_refresh_id = self.root.after(20000, _refresh)

        # Start first refresh after 5 seconds
        self._gc_refresh_id = self.root.after(20000, _refresh)

    def _update_gc_card(self, gc_data: dict | None):
        """Update just the Gap & Crap card in-place without rebuilding the whole panel."""
        if self._gc_card_frame:
            # Clear the card's contents, keep the frame
            for w in self._gc_card_frame.winfo_children():
                w.destroy()
            # Rebuild header
            hdr = tk.Label(self._gc_card_frame, text="Gap & Crap", fg=ACCENT, bg=BG_CARD,
                           font=FONT_HEADER, anchor="w", padx=14, pady=8)
            hdr.pack(fill="x")
            tk.Frame(self._gc_card_frame, bg=BORDER, height=1).pack(fill="x")
            # Rebuild body
            self._build_gc_body(self._gc_card_frame, gc_data)

    def _update_pm_short_card(self, pm_short_data: dict | None):
        """Update just the PM Short card in-place without rebuilding the whole panel."""
        if self._pm_short_card_frame:
            for w in self._pm_short_card_frame.winfo_children():
                w.destroy()
            hdr = tk.Label(self._pm_short_card_frame, text="PM Short", fg=ACCENT, bg=BG_CARD,
                           font=FONT_HEADER, anchor="w", padx=14, pady=8)
            hdr.pack(fill="x")
            tk.Frame(self._pm_short_card_frame, bg=BORDER, height=1).pack(fill="x")
            self._build_pm_short_body(self._pm_short_card_frame, pm_short_data)

    def _add_gap_crap_panel(self, gc: dict | None):
        """Add Gap & Crap trade setup card."""
        card = self._make_card(self.content_frame, title="Gap & Crap")
        self._gc_card_frame = card
        self._build_gc_body(card, gc)

    def _build_pm_short_body(self, card, pm: dict | None):
        """Build the PM Short setup card content."""
        body = tk.Frame(card, bg=BG_CARD, padx=14, pady=10)
        body.pack(fill="x")

        if pm is None or pm.get("reject_reason"):
            reason = pm.get("reject_reason", "No data") if pm else "No data"
            tk.Label(body, text=reason, fg=FG_DIM, bg=BG_CARD,
                     font=FONT_MONO, wraplength=280, justify="left").pack(anchor="w")
            return

        TIER_COLORS = {"A": "#5fb890", "B": "#6090c8", "C": "#c47070", "D": "#a07ed4"}

        # Header: Setup A — Micro Cap Fade
        hdr = tk.Frame(body, bg=BG_CARD)
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text=f"Setup {pm['tier']}", fg=TIER_COLORS.get(pm['tier'], ACCENT),
                 bg=BG_CARD, font=("Segoe UI Semibold", 14)).pack(side="left")
        tk.Label(hdr, text=f"  {pm['name']}", fg=FG_DIM, bg=BG_CARD,
                 font=FONT_UI).pack(side="left")

        # Live data row (same style as Gap & Crap info rows)
        info = tk.Frame(body, bg=BG_CARD)
        info.pack(fill="x", pady=(0, 4))
        tk.Label(info, text=f"Gap: +{pm.get('actual_gap', 0):.0f}%", fg=ACCENT, bg=BG_CARD,
                 font=FONT_MONO_BOLD).pack(side="left")
        if pm.get("actual_h2pmh") is not None:
            tk.Label(info, text=f"  |  H2PMH: {pm['actual_h2pmh']:+.1f}%", fg="#4CAF50",
                     bg=BG_CARD, font=FONT_MONO).pack(side="left")
        if pm.get("actual_pm_high"):
            tk.Label(info, text=f"  |  PM High: {fmt_price(pm['actual_pm_high'])}",
                     fg="#4CAF50", bg=BG_CARD, font=FONT_MONO).pack(side="left")

        # Float / MCap line
        detail_parts = []
        if pm.get("actual_float") and pm["actual_float"] > 0:
            detail_parts.append(f"Float {fmt_millions(pm['actual_float'])}")
        if pm.get("actual_mcap") and pm["actual_mcap"] > 0:
            detail_parts.append(f"MCap {fmt_millions(pm['actual_mcap'])}")
        if pm.get("actual_pm_vol") and pm["actual_pm_vol"] > 0:
            detail_parts.append(f"PMVol {fmt_volume(pm['actual_pm_vol'])}")
        if pm.get("actual_price") and pm.get("price_max"):
            detail_parts.append(f"Price {fmt_price(pm['actual_price'])}")
        if detail_parts:
            tk.Label(body, text="  |  ".join(detail_parts), fg=FG_DIM, bg=BG_CARD,
                     font=FONT_MONO).pack(anchor="w")

        # Separator
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(0, 6))

        # Entry pattern stats
        tk.Label(body, text="ENTRY PATTERN", fg="#FFD600", bg=BG_CARD,
                 font=FONT_MONO_BOLD).pack(anchor="w", pady=(0, 3))

        entry_stats = [
            ("1st Drop", f"{pm['first_drop']:+.1f}%", RED),
            ("Lower High", f"{pm['lower_high']:+.1f}%", RED),
            ("LH Confirmed", f"{pm['lh_confirmed']:.0f}%", GREEN),
            ("Time to LH", pm['time_to_lh'], FG),
        ]
        for label, value, color in entry_stats:
            row = tk.Frame(body, bg=BG_CARD)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, fg=FG_DIM, bg=BG_CARD,
                     font=FONT_MONO, width=16, anchor="w").pack(side="left")
            tk.Label(row, text=value, fg=color, bg=BG_CARD,
                     font=FONT_MONO_BOLD, anchor="w").pack(side="left")

        # Separator
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(6, 6))

        # Expected outcome
        tk.Label(body, text="EXPECTED OUTCOME", fg="#FFD600", bg=BG_CARD,
                 font=FONT_MONO_BOLD).pack(anchor="w", pady=(0, 3))

        outcome_stats = [
            ("Full Fade", f"{pm['full_fade']:.1f}%", GREEN),
            ("Cost of Wait", f"-{pm['cost_of_waiting']:.1f}%", "#FF9800"),
            ("Remaining", f"{pm['remaining_profit']:.1f}%", GREEN),
            ("Avg Bounce", f"{pm['avg_bounce']:.0f}%", RED),
        ]
        for label, value, color in outcome_stats:
            row = tk.Frame(body, bg=BG_CARD)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, fg=FG_DIM, bg=BG_CARD,
                     font=FONT_MONO, width=16, anchor="w").pack(side="left")
            tk.Label(row, text=value, fg=color, bg=BG_CARD,
                     font=FONT_MONO_BOLD, anchor="w").pack(side="left")

        # Fade distribution
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(6, 6))
        tk.Label(body, text="FADE HIT RATE", fg="#FFD600", bg=BG_CARD,
                 font=FONT_MONO_BOLD).pack(anchor="w", pady=(0, 3))

        fade_keys = [k for k in ("fade_5", "fade_10", "fade_15", "fade_20", "fade_25") if k in pm]
        for key in fade_keys:
            pct_label = key.replace("fade_", "")
            hit_rate = pm[key]
            row = tk.Frame(body, bg=BG_CARD)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f">{pct_label}% fade", fg=FG_DIM, bg=BG_CARD,
                     font=FONT_MONO, width=16, anchor="w").pack(side="left")
            hr_color = GREEN if hit_rate >= 80 else ("#FF9800" if hit_rate >= 60 else RED)
            tk.Label(row, text=f"{hit_rate:.1f}%", fg=hr_color, bg=BG_CARD,
                     font=FONT_MONO_BOLD, anchor="w").pack(side="left")

    def _build_gc_body(self, card, gc: dict | None):
        body = tk.Frame(card, bg=BG_CARD, padx=14, pady=10)
        body.pack(fill="x")

        if gc is None:
            tk.Label(body, text="Gap < 40% — no setup", fg=FG_DIM, bg=BG_CARD,
                     font=FONT_UI).pack(anchor="w")
            return

        gap_pct = gc["gap_pct"]

        # Not qualified — above PM high
        if not gc["qualifies"]:
            tk.Label(body, text=f"Gap +{gap_pct:.0f}% — Setup {gc['tier']} ({gc['tier_name']})",
                     fg="#FF9800", bg=BG_CARD, font=FONT_UI_BOLD).pack(anchor="w")
            h2_text = f"H2PMH: {gc['h2pmh']:+.1f}%" if gc['h2pmh'] is not None else "H2PMH: unknown"
            tk.Label(body, text=f"NO SETUP — price above PM high ({h2_text})",
                     fg="#FF4444", bg=BG_CARD, font=FONT_UI_BOLD).pack(anchor="w", pady=(4, 0))
            return

        # ── QUALIFIED ──
        TIER_COLORS = {"A": "#6090c8", "B": "#c47070", "C": "#5fb890", "D": "#a07ed4"}
        SIZING_COLORS = {"FULL": "#4CAF50", "HALF": "#FFD600", "QUARTER": "#FF9800", "UNKNOWN": FG_DIM}

        # Header: SHORT Setup B — FULL SIZE
        hdr = tk.Frame(body, bg=BG_CARD)
        hdr.pack(fill="x", pady=(0, 6))
        tk.Label(hdr, text="SHORT", fg="#FF4444", bg=BG_CARD,
                 font=("Segoe UI Semibold", 14)).pack(side="left")
        tk.Label(hdr, text=f"  Setup {gc['tier']}", fg=TIER_COLORS.get(gc['tier'], ACCENT),
                 bg=BG_CARD, font=("Segoe UI Semibold", 14)).pack(side="left")
        tk.Label(hdr, text=f"  ({gc['tier_name']})", fg=FG_DIM, bg=BG_CARD,
                 font=FONT_UI).pack(side="left")
        tk.Label(hdr, text=f"  {gc['sizing']}", fg="black",
                 bg=SIZING_COLORS.get(gc['sizing'], FG_DIM),
                 font=("Segoe UI Semibold", 10), padx=8, pady=2).pack(side="right")

        # Info row
        info = tk.Frame(body, bg=BG_CARD)
        info.pack(fill="x", pady=(0, 4))
        tk.Label(info, text=f"Gap: +{gap_pct:.0f}%", fg=ACCENT, bg=BG_CARD,
                 font=FONT_MONO_BOLD).pack(side="left")
        if gc["h2pmh"] is not None:
            tk.Label(info, text=f"  |  H2PMH: {gc['h2pmh']:+.1f}%", fg="#4CAF50",
                     bg=BG_CARD, font=FONT_MONO).pack(side="left")
        if gc["pm_high"]:
            pm_src = gc.get("pm_source", "")
            pm_color = "#4CAF50" if pm_src == "live" else ("#FFD600" if pm_src == "open_proxy" else FG_DIM)
            pm_label = f"PM High: ${gc['pm_high']:.2f}"
            if pm_src == "open_proxy":
                pm_label += " (est.)"
            tk.Label(info, text=f"  |  {pm_label}", fg=pm_color,
                     bg=BG_CARD, font=FONT_MONO).pack(side="left")

        tk.Label(body, text=gc["sizing_detail"], fg=FG_DIM, bg=BG_CARD,
                 font=FONT_MONO).pack(anchor="w")

        # PM high warning
        pm_src = gc.get("pm_source", "unavailable")
        if pm_src == "open_proxy":
            tk.Label(body, text="⚠ PM High estimated from day high — run app pre-market for exact value",
                     fg="#FFD600", bg=BG_CARD, font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 6))
        elif pm_src == "unavailable":
            tk.Label(body, text="⚠ PM High unavailable — run app before 9:30 to cache",
                     fg="#FF9800", bg=BG_CARD, font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 6))
        else:
            tk.Frame(body, bg=BG_CARD, height=6).pack()  # spacer

        # Price levels
        levels_frame = tk.Frame(body, bg=BG_ROW,
                                highlightbackground=BORDER_INNER, highlightthickness=1)
        levels_frame.pack(fill="x")

        for label, price_str, detail, color in [
            ("STOP", f"${gc['stop']:.2f}", f"+{GC_STOP_PCT}%", "#FF4444"),
            ("ENTRY", f"${gc['entry']:.2f}", "Short at open", ACCENT),
            ("BE Trail", f"${gc['be_trigger']:.2f}", f"-{GC_BE_PCT}% trigger", "#FFD600"),
            ("T1 (1/3)", f"${gc['t1']:.2f}", f"-{GC_T1_PCT}%", "#4CAF50"),
            ("T2 (1/3)", f"${gc['t2']:.2f}", f"-{GC_T2_PCT}%", "#4CAF50"),
            ("T3 (1/3)", f"${gc['t3']:.2f}", f"-{GC_T3_PCT}%", "#4CAF50"),
        ]:
            row = tk.Frame(levels_frame, bg=BG_ROW, padx=10, pady=3)
            row.pack(fill="x")
            tk.Label(row, text=label, fg=color, bg=BG_ROW,
                     font=FONT_MONO_BOLD, width=10, anchor="w").pack(side="left")
            tk.Label(row, text=price_str, fg="white", bg=BG_ROW,
                     font=("Consolas", 11, "bold"), width=10, anchor="e").pack(side="left")
            tk.Label(row, text=detail, fg=FG_DIM, bg=BG_ROW,
                     font=FONT_MONO).pack(side="left", padx=(10, 0))

        tk.Label(body, text=f"Prev close: ${gc['prev_close']:.2f}", fg=FG_DIM,
                 bg=BG_CARD, font=FONT_MONO).pack(anchor="w", pady=(6, 0))

    def _add_in_play_section(self, warrants: list[dict], convertibles: list[dict],
                             stock_price: float = 0.0, dilution_url: str = ""):
        card = self._make_card(self.content_frame, title="In Play Dilution")
        body = tk.Frame(card, bg=BG_CARD, padx=14, pady=10)
        body.pack(fill="x")

        if warrants:
            tk.Label(
                body, text="WARRANTS", fg="#FFD600", bg=BG_CARD,
                font=FONT_UI_BOLD, anchor="w",
            ).pack(fill="x", pady=(4, 4))
            for w in warrants:
                ex_price = w.get("warrants_exercise_price", 0) or 0
                in_money = stock_price > 0 and ex_price <= stock_price
                self._add_dilution_row(
                    body, w.get("details", ""),
                    f"Remaining: {fmt_millions(w.get('warrants_remaining'))}",
                    f"Strike: ${ex_price:.2f}",
                    (w.get("filed_at") or "")[:10],
                    in_money,
                )

        if convertibles:
            tk.Label(
                body, text="CONVERTIBLES", fg="#FFD600", bg=BG_CARD,
                font=FONT_UI_BOLD, anchor="w",
            ).pack(fill="x", pady=(8, 4))
            for c in convertibles:
                conv_price = c.get("conversion_price", 0) or 0
                in_money = stock_price > 0 and conv_price <= stock_price
                self._add_dilution_row(
                    body, c.get("details", ""),
                    f"Shares: {fmt_millions(c.get('underlying_shares_remaining'))}",
                    f"Conv: ${conv_price:.2f}",
                    (c.get("filed_at") or "")[:10],
                    in_money,
                )

        if dilution_url:
            self._bind_card_click(card, dilution_url)

    def _add_dilution_row(self, parent, details, remaining, price, filed,
                          price_above=False):
        # Green if strike/conv price <= stock price (in the money), orange otherwise
        highlight = "#4CAF50" if price_above else "#FF9800"

        row = tk.Frame(parent, bg=BG_ROW,
                       highlightbackground=BORDER_INNER, highlightthickness=1)
        row.pack(fill="x", pady=2)

        inner = tk.Frame(row, bg=BG_ROW, padx=10, pady=6)
        inner.pack(fill="x")

        # Line 1: details (truncated if long)
        detail_text = details if len(details) <= 60 else details[:57] + "..."
        tk.Label(inner, text=detail_text, fg="white", bg=BG_ROW,
                 font=FONT_UI, anchor="w").pack(fill="x")

        # Line 2: remaining | price | filed
        data_row = tk.Frame(inner, bg=BG_ROW)
        data_row.pack(fill="x", pady=(2, 0))
        tk.Label(data_row, text=remaining, fg=highlight, bg=BG_ROW,
                 font=FONT_MONO_BOLD).pack(side="left")
        tk.Label(data_row, text="  |  ", fg=FG_DIM, bg=BG_ROW,
                 font=FONT_MONO).pack(side="left")
        tk.Label(data_row, text=price, fg=highlight, bg=BG_ROW,
                 font=FONT_MONO_BOLD).pack(side="left")
        tk.Label(data_row, text=f"  |  Filed: {filed}", fg=FG_DIM, bg=BG_ROW,
                 font=FONT_MONO).pack(side="left")

    # ── Gainers panel ───────────────────────────────────────────────────────
    def _cache_pm_highs_from_gainers(self):
        """Cache PM highs for all gainers. Called on startup and after every gainers refresh.
        Uses TradingView premarket_high or dayHigh as fallback."""
        def _cache():
            cached = 0
            for g in self._gainers_data:
                ticker = g.get("symbol") or g.get("ticker", "")
                if not ticker or ticker in self._pm_high_cache:
                    continue
                q = fetch_tv_quote(ticker)
                if q:
                    pm_h = q.get("premarket_high")
                    if pm_h and pm_h > 0:
                        self._pm_high_cache[ticker] = pm_h
                        cached += 1
                        continue
                    dh = q.get("dayHigh")
                    if dh and dh > 0:
                        self._pm_high_cache[ticker] = dh
                        cached += 1
                time.sleep(0.2)
            if cached > 0:
                print(f"[GC] Cached PM high for {cached} new tickers (total: {len(self._pm_high_cache)})")
        # Delay 5s on first call to let gainers load
        self.root.after(5000, lambda: threading.Thread(target=_cache, daemon=True).start())

    def _cache_pm_highs_bg(self):
        """Background thread: cache PM highs for all current gainers."""
        cached = 0
        for g in self._gainers_data:
            ticker = g.get("symbol") or g.get("ticker", "")
            if not ticker or ticker in self._pm_high_cache:
                continue
            q = fetch_tv_quote(ticker)
            if q:
                pm_h = q.get("premarket_high")
                if pm_h and pm_h > 0:
                    self._pm_high_cache[ticker] = pm_h
                    cached += 1
                    time.sleep(0.2)
                    continue
                dh = q.get("dayHigh")
                if dh and dh > 0:
                    self._pm_high_cache[ticker] = dh
                    cached += 1
            time.sleep(0.2)
        if cached > 0:
            print(f"[GC] Auto-cached PM high for {cached} tickers (total: {len(self._pm_high_cache)})")

    def _schedule_gainers_refresh(self):
        """Kick off the first gainers fetch."""
        self._trigger_gainers_refresh()

    def _trigger_gainers_refresh(self):
        """Fetch gainers in background thread. TradingView provides MCap filtering."""
        self._gainers_status.config(text="loading...")

        def _fetch():
            try:
                gainers = fetch_top_gainers_raw()  # TradingView (or FMP fallback)
                self.root.after(0, self._update_gainers_ui, gainers)
            except Exception as ex:
                print(f"Gainers refresh error: {ex}")
                self.root.after(0, self._update_gainers_ui, [])

        threading.Thread(target=_fetch, daemon=True).start()

    def _update_gainers_ui(self, gainers: list[dict]):
        """Rebuild the gainers list with fresh data."""
        self._gainers_data = gainers
        self._gainers_status.config(text=str(len(gainers)))
        # Cache PM highs for any new tickers
        threading.Thread(target=self._cache_pm_highs_bg, daemon=True).start()

        # Clear existing rows
        for w in self._gainers_frame.winfo_children():
            w.destroy()

        if not gainers:
            tk.Label(self._gainers_frame, text="No gainers found",
                     fg=FG_DIM, bg=BG, font=FONT_UI).pack(pady=20)
        else:
            for item in gainers:
                self._build_gainer_row(item)

        # Schedule next refresh
        self.root.after(GAINERS_REFRESH_SECS * 1000, self._trigger_gainers_refresh)


    def _build_gainer_row(self, item: dict):
        """Build a single clickable gainer row."""
        ticker = item.get("ticker", "")
        change_pct = item.get("todaysChangePerc", 0)
        price = item.get("day", {}).get("c", 0) or item.get("lastTrade", {}).get("p", 0) or 0
        volume = item.get("day", {}).get("v", 0) or item.get("min", {}).get("av", 0) or 0

        is_selected = (ticker == self._selected_gainer)
        row_bg = BG_SELECTED if is_selected else BG_CARD
        border_color = BORDER_ACCENT if is_selected else BORDER

        row = tk.Frame(self._gainers_frame, bg=row_bg,
                       highlightbackground=border_color, highlightthickness=1,
                       cursor="hand2")
        row.pack(fill="x", padx=4, pady=2)

        inner = tk.Frame(row, bg=row_bg, padx=4, pady=3)
        inner.pack(fill="x")

        # Top line: ticker + risk badge + change %
        top = tk.Frame(inner, bg=row_bg)
        top.pack(fill="x")

        tk.Label(top, text=ticker, fg=ACCENT, bg=row_bg,
                 font=FONT_GAINER_TICKER, cursor="hand2").pack(side="left")

        risk_level = item.get("_risk", "")
        if risk_level:
            tk.Label(top, text=f" {risk_level} ", fg="white",
                     bg=risk_bg(risk_level), font=("Consolas", 7, "bold"),
                     padx=4, pady=1, cursor="hand2").pack(side="left", padx=(6, 0))
        if item.get("_news_today"):
            tk.Label(top, text=" News ", fg="white", bg="#1F8FB3",
                     font=("Consolas", 7, "bold"), padx=4, pady=1,
                     cursor="hand2").pack(side="left", padx=(4, 0))
        pct_text = f"+{change_pct:.1f}%" if change_pct >= 0 else f"{change_pct:.1f}%"
        pct_color = GREEN if change_pct >= 0 else RED
        tk.Label(top, text=pct_text, fg=pct_color, bg=row_bg,
                 font=FONT_GAINER_PCT, cursor="hand2").pack(side="right")

        # Middle line: price + volume
        mid = tk.Frame(inner, bg=row_bg)
        mid.pack(fill="x")

        tk.Label(mid, text=fmt_price(price), fg=FG, bg=row_bg,
                 font=FONT_GAINER_DETAIL, cursor="hand2").pack(side="left")
        tk.Label(mid, text=f"Vol {fmt_volume(volume)}", fg=FG_DIM, bg=row_bg,
                 font=FONT_GAINER_DETAIL, cursor="hand2").pack(side="right")

        # Bottom line: float / mcap / sector / country (condensed)
        flt = item.get("_float")
        mcap = item.get("_mcap")
        sector = item.get("_sector", "")
        country = item.get("_country", "")
        # Shorten long sector names
        sector_short = {
            "Healthcare": "Health", "Technology": "Tech",
            "Industrials": "Indust", "Consumer Cyclical": "Cons Cyc",
            "Consumer Defensive": "Cons Def", "Communication Services": "Comms",
            "Financial Services": "Financ", "Basic Materials": "Materials",
            "Real Estate": "RE",
        }.get(sector, sector)
        info_parts = []
        if flt:
            info_parts.append(fmt_millions(flt))
        if mcap:
            info_parts.append(fmt_millions(mcap))
        if sector_short:
            info_parts.append(sector_short)
        if country:
            info_parts.append(country)
        if info_parts:
            bot = tk.Frame(inner, bg=row_bg)
            bot.pack(fill="x")
            tk.Label(bot, text=" | ".join(info_parts), fg=FG_DIM, bg=row_bg,
                     font=FONT_GAINER_DETAIL, cursor="hand2").pack(side="left")
        else:
            bot = None

        # Bind click on all child widgets
        def on_click(e, t=ticker):
            self._on_gainer_click(t)

        click_targets = [row, inner, top, mid]
        if bot:
            click_targets.append(bot)
        for widget in click_targets:
            widget.bind("<Button-1>", on_click)
        for widget in (list(top.winfo_children()) + list(mid.winfo_children())
                       + (list(bot.winfo_children()) if bot else [])):
            widget.bind("<Button-1>", on_click)

    def _on_gainer_click(self, ticker: str):
        """Handle click on a gainer — select it and load Ask Edgar data."""
        self._selected_gainer = ticker
        # Rebuild gainers list to update selection highlight
        self._rebuild_gainers_list()
        # Load Ask Edgar data
        self._on_ticker_change(ticker)

    def _rebuild_gainers_list(self):
        """Rebuild gainer rows from cached data (updates selection state)."""
        for w in self._gainers_frame.winfo_children():
            w.destroy()
        for item in self._gainers_data:
            self._build_gainer_row(item)

    def _on_search(self, event=None):
        """Handle search box submit."""
        ticker = self.search_entry.get().strip().upper()
        if ticker:
            self.search_entry.delete(0, "end")
            self._selected_gainer = None
            self._rebuild_gainers_list()
            self._on_ticker_change(ticker)

    # ── Dragging ──
    def _start_drag(self, event):
        self._drag_data["x"] = event.x
        self._drag_data["y"] = event.y

    def _on_drag(self, event):
        dx = event.x - self._drag_data["x"]
        dy = event.y - self._drag_data["y"]
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")

    # ── Monitor thread ──
    def _start_monitor(self):
        # DAS/ToS window monitoring disabled for Gap & Crap app.
        # Ticker selection only via: gainers list click or search box.
        def poll():
            while True:
                time.sleep(POLL_INTERVAL)

        threading.Thread(target=poll, daemon=True).start()

    def _on_ticker_change(self, ticker: str):
        self.current_ticker = ticker
        self._show_loading(ticker)

        def fetch():
            dilution = fetch_dilution_data(ticker)
            floatdata = fetch_float_data(ticker)
            news, grok_line, grok_date, grok_url, jmt415_notes = fetch_news_and_grok(ticker)
            warrants, converts, stock_price = fetch_in_play_dilution(ticker)
            gap_stats = fetch_gap_stats(ticker)
            recent_offerings = fetch_offerings(ticker)
            ownership = fetch_ownership(ticker)
            short_interest = fetch_short_interest(ticker)  # uses cached screener call
            # Fetch chart analysis for history badge
            history_rating = ""
            history_url = ""
            cdata = _askedgar_get(CHART_ANALYSIS_URL, {"ticker": ticker, "limit": 1}, f"chart/{ticker}")
            if cdata and cdata.get("status") == "success" and cdata.get("results"):
                history_rating = cdata["results"][0].get("rating", "")
                history_url = cdata["results"][0].get("post_url", "")
            self.root.after(0, self._update_history_badge, history_rating, history_url)
            # Fetch Gap & Crap data
            # 1. Get quote from TradingView (price, PM high, volume, mcap)
            tv_quote = fetch_tv_quote(ticker)

            pm_high = None
            if tv_quote:
                pm_h = tv_quote.get("premarket_high")
                if pm_h and pm_h > 0:
                    self._pm_high_cache[ticker] = pm_h
                    pm_high = pm_h
                    print(f"[GC] Live PM high for {ticker}: ${pm_high:.2f}")

            # 2. Fall back to cached PM high
            if not pm_high:
                pm_high = self._pm_high_cache.get(ticker)
                if pm_high:
                    print(f"[GC] Using cached PM high for {ticker}: ${pm_high:.2f}")

            # 3. Fall back to dayHigh as PM high proxy (regular hours only)
            if not pm_high and tv_quote:
                day_high = tv_quote.get("dayHigh")
                if day_high and day_high > 0:
                    pm_high = day_high
                    print(f"[GC] Using TV dayHigh as PM high proxy for {ticker}: ${pm_high:.2f}")

            # 4. Determine current price + previous close for gap calculation
            gc_data = None
            pm_source = "live" if ticker in self._pm_high_cache else ("open_proxy" if pm_high else "unavailable")

            # Look up gainer data from left panel
            gainer_item = next((g for g in self._gainers_data
                                if (g.get("ticker") or g.get("symbol")) == ticker), None)

            if _is_premarket() and gainer_item:
                # Pre-market: price and change% from TradingView scanner
                gainer_price = gainer_item.get("day", {}).get("c", 0)
                gainer_pct = gainer_item.get("todaysChangePerc", 0)
                if gainer_price and gainer_pct > 0:
                    current_price = gainer_price
                    prev_close = current_price / (1 + gainer_pct / 100)
                    print(f"[GC] Pre-market: using gainer data price=${current_price:.4f}, "
                          f"derived prev_close=${prev_close:.4f} (gap {gainer_pct:.1f}%)")
                else:
                    current_price = 0
                    prev_close = 0
            elif tv_quote:
                current_price = tv_quote.get("price", 0)
                prev_close = tv_quote.get("previousClose", 0)
            else:
                current_price = 0
                prev_close = 0

            ae_float = floatdata.get("float") if floatdata else None
            if ae_float and ae_float > 0:
                self._float_cache[ticker] = ae_float

            if current_price and prev_close:
                gc_data = classify_gap_crap(
                    current_price,
                    prev_close,
                    pm_high,
                    ae_float,
                )
                if gc_data:
                    gc_data["pm_source"] = pm_source

            # PM Short classification — uses gap%, float, mcap, PM volume, price
            gap_pct = ((current_price - prev_close) / prev_close * 100) if current_price and prev_close else 0
            mcap_val = floatdata.get("market_cap_final") if floatdata else None
            pm_vol = gainer_item.get("day", {}).get("v", 0) if gainer_item else (
                tv_quote.get("volume", 0) if tv_quote else 0)
            # H2PMH for PM Short card
            pm_h2pmh = None
            if pm_high and pm_high > 0 and current_price:
                pm_h2pmh = (current_price - pm_high) / pm_high * 100
            pm_short_data = classify_pm_short(
                gap_pct, ae_float, mcap_val, pm_vol, current_price,
                pm_high, pm_h2pmh
            )

            if dilution:
                self.root.after(0, self._show_data, ticker, dilution, floatdata,
                                news, grok_line, grok_date, grok_url, warrants, converts, stock_price,
                                jmt415_notes, gap_stats, recent_offerings, ownership, gc_data,
                                pm_short_data)
            else:
                # Show setup cards even without dilution data
                self.root.after(0, self._show_no_data_gc, ticker, gc_data, pm_short_data)

        threading.Thread(target=fetch, daemon=True).start()

    def _on_balance_update(self, balance: float) -> None:
        """Called from the AskEdgar fetch worker thread — schedule the actual
        title update on the tkinter main thread so we don't crash the GUI."""
        try:
            self.root.after(0, self._render_title_with_balance, balance)
        except Exception:
            # Root may be destroyed during shutdown — swallow silently.
            pass

    def _render_title_with_balance(self, balance: float) -> None:
        """Update the window title with the current AskEdgar balance.
        Prepends a warning glyph + LOW marker when below the threshold."""
        amount = f"${balance:.2f}"
        if balance < _LOW_BALANCE_THRESHOLD_DOLLARS:
            self.root.title(f"⚠ {self._base_title} — {amount} LOW")
        else:
            self.root.title(f"{self._base_title} — {amount}")

    def _on_close(self):
        """Save window geometry before closing."""
        try:
            geo = self.root.geometry()
            with open(self._geo_file, "w") as f:
                f.write(geo)
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = DilutionOverlay()
    app.run()

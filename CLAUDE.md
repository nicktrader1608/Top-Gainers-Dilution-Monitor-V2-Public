# Small Cap Monitor — Project CLAUDE.md

## Project Overview

This folder contains **two apps**:

| File | App Name | Purpose |
|---|---|---|
| `das_monitor.py` | Ask Edgar V2 (original) | V2 dilution overlay — **backup, don't modify** |
| `das_monitor_gc.py` | **Small Cap Monitor** | **ACTIVE APP** — combines Gap & Crap + PM Short + Ask Edgar dilution |

**Small Cap Monitor** (`das_monitor_gc.py`) is the daily-use trading app. It's a single-file Python tkinter desktop overlay with:

- **Left panel**: Top gainers (stockanalysis.com pre-market / FMP regular session, filtered MCap < $500M, gain >= 30%)
- **Right panel, Row 1**: PM Short setup (left) + Gap & Crap setup (right) — side by side with exact trade levels
- **Right panel, Row 2**: News & Catalyst
- **Right panel, Row 3**: Risk badges + Offering Ability + Recent Offerings (3 equal columns)
- **Below**: In Play Dilution, Gap Stats, JMT415 Notes, Mgmt Commentary, Ownership
- **Search box**: Manual ticker entry
- **Auto-refresh**: GC and PM Short panels refresh every 20 seconds
- **Window geometry**: Saves size/position on close, restores on open

DAS/ToS auto-detect is disabled — ticker selection via gainers list or search box only.

## Related Projects

| Project | Location | Relationship |
|---|---|---|
| Gap & Crap research | `../Gap and Crap/` | Backtesting, dashboards — GC setup rules come from here |
| PM Short research | `../PM Short and Long Strategy/` | Backtesting — PM Short setup rules come from here |
| V1 Ask Edgar app | ~~deleted~~ | Was superseded by V2, folder removed |

## Setup Requirements

### API Keys (canonical `.env` is `C:\Users\User\Documents\Trading\Claude\.env`)

Two API keys are needed:

| Variable | Source | Purpose |
|---|---|---|
| `ASKEDGAR_API_KEY` | [askedgar.io/api-trial](https://www.askedgar.io/api-trial) | Dilution data, news, filings, gap stats, offerings — one key for all endpoints |
| `FMP_API_KEY` | [financialmodelingprep.com](https://financialmodelingprep.com) | Top gainers and real-time quote data (premium tier) |

**Key resolution order** (set 2026-05-06 to fix the dual-`.env` bug class):
1. `..\.env` — canonical `Claude/.env`, single source of truth on Nick's machine
2. `.env` next to `das_monitor_gc.py` — dev / build case
3. `.env` next to the exe — PyInstaller distribution to other users (the legacy `dist/.env` workflow)

On Nick's machine the canonical file always wins. The previous local `.env` was renamed `.env.deprecated_20260506` so any future regression fails loud. **Never** create a project-local `.env` for Nick's setup — paste keys into the canonical file only.

The `.env` file is git-ignored at every location.

### FMP API endpoints used

| Endpoint | Purpose | Hours |
|---|---|---|
| `/stable/biggest-gainers` | Top gainers list (symbol, price, changesPercentage) | **Regular session only** |
| `/stable/quote?symbol=X` | Real-time quote with volume and market cap (for filtering) | **Regular session only** |
| `/stable/aftermarket-trade?symbol=X` | Extended hours last trade (price, tradeSize, timestamp) | **Pre-market 4-9:30 AM + After-hours 4-8 PM ET** |
| `/stable/aftermarket-quote?symbol=X` | Extended hours bid/ask + volume | **Pre-market 4-9:30 AM + After-hours 4-8 PM ET** |

**Note:** The old `/api/v3/` endpoints are legacy and blocked for accounts created after Aug 2025. V2 uses the `/stable/` endpoints.

**FMP pre-market facts (confirmed live 2026-04-06 at 4:43 AM ET):**
- `aftermarket-trade` and `aftermarket-quote` **work for pre-market** — "aftermarket" is FMP's umbrella term for all extended hours (pre-market + after-hours)
- These return real-time data with timestamps, prices, and volume for individual tickers
- **Cannot discover gappers** — these endpoints require you to already know the ticker symbol
- `batch-aftermarket-trade` returns 400 error — **do not use**
- `/stable/quote` only updates during regular hours — **do not use for pre-market prices**
- `/stable/historical-chart/5min` returns regular hours only — **do not use for pre-market**
- FMP has **no pre-market gainers/movers endpoint** — use stockanalysis.com scraping to discover gappers, then FMP aftermarket endpoints for real-time prices

### Python Dependencies

Listed in `requirements.txt`. Install with `pip install -r requirements.txt`. Key packages:

- `pywin32` — Windows API for window title detection
- `requests` — HTTP client for API calls
- `python-dotenv` — Loads `.env` file

### Platform

Windows only — uses `win32gui` for enumerating desktop windows.

## Architecture

Everything is in `das_monitor.py` (~1714 lines). No separate modules, no frameworks.

### Key sections:

1. **Config & constants** (top) — API URLs, keys, colors, fonts
2. **RateLimiter class** — Thread-safe rate limiter for Ask Edgar API (45 req/min, limit is 50)
3. **API fetch functions** — `_askedgar_get()` (shared retry wrapper), `fetch_dilution_data()`, `fetch_float_data()`, `fetch_news_and_grok()`, `fetch_in_play_dilution()`, `fetch_gap_stats()`, `fetch_offerings()`, `fetch_ownership()`, `fetch_top_gainers_raw()`, `_fetch_premarket_gainers()`, `_fetch_fmp_gainers()`, `_mcap_filter()`
4. **Window detection** — `find_montage_windows()` for DAS, `find_tos_chart_windows()` for thinkorswim
5. **`DilutionMonitor` class** — The main tkinter app with all UI rendering methods
6. **Card rendering methods** — `_add_offering_ability_card()`, `_add_gap_stats_card()`, `_add_offerings_card()`, `_add_in_play_section()`, etc.

### Data flow:

1. `_poll_windows()` runs every 1 second, checks for ticker changes
2. On change, `_on_ticker_change()` spawns a background thread
3. Thread makes parallel API calls via `ThreadPoolExecutor` (3 workers, rate-limited)
4. Results are passed back to the UI thread via `root.after(0, callback)`
5. UI clears and rebuilds the right panel with fresh data

### Top gainers data flow:

1. `_is_premarket()` checks if before 9:30 AM ET
2. Pre-market: scrapes stockanalysis.com for gappers >= 30% gain
3. Regular hours: FMP `/stable/biggest-gainers` for session gainers >= 30%
4. Each ticker filtered via `_mcap_filter()` in parallel (3 workers) — FMP quote only, drops if mcap >= $500M
5. **No Ask Edgar calls until user clicks a ticker** — keeps the left panel fast and preserves API quota
6. On click, right panel fetches all Ask Edgar data (8 calls: dilution, float, **news-basic**, screener, dilution-data, chart, gap-stats, offerings, ownership). News uses `/v1/news-basic` (not `/v1/news`) — same headlines/summaries/grok/jmt415 rows, just without article bodies. Verified 4-6x cheaper than full news on news-heavy tickers (EVC: $0.02 vs $0.09; EZGO: $0.01 vs $0.06).

### Ask Edgar API retry logic:

All Ask Edgar calls go through `_askedgar_get()` which:
- Rate-limits at 45 req/min via `_askedgar_limiter`
- Auto-retries up to 3 times on 429 (rate limit), waiting the `retry_after` seconds from the response
- **Live AskEdgar balance in window title** — every fresh fetch parses `usage.credits_remaining_dollars` and updates the title bar. Healthy: `Small Cap Monitor — $16.68`. Below threshold (`_LOW_BALANCE_THRESHOLD_DOLLARS = $5`): `⚠ Small Cap Monitor — $4.23 LOW`. Title also shows in the taskbar so the warning is visible even when SCM isn't focused.
- **Disk-persisted response cache** (`_askedgar_cache`, file `askedgar_session_cache.json` next to script/exe) with 30-minute TTL. Survives SCM restarts so re-clicking a recently-viewed ticker after a relaunch is free instead of paying the per-click fetch fee again (~$0.02-0.40 depending on news payload). Cache key is `(url, ticker)`. Timestamps are `time.time()` epoch seconds (NOT `monotonic` — monotonic is process-relative and breaks across restarts). Stale entries are pruned on save and on load. Atomic writes via `.tmp` + `os.replace`. Only fresh fetches trigger a save — cache hits do no I/O.

### Right panel card order:

1. Feed (news + grok)
2. Risk badges (grid)
3. Offering Ability
4. In Play Dilution
5. Recent Offerings
6. Gap Stats
7. JMT415 Notes
8. Mgmt Commentary
9. Ownership

### Ask Edgar API endpoints used:

| Endpoint | Function |
|---|---|
| `/v1/dilution-rating` | `fetch_dilution_data()` |
| `/v1/float-outstanding` | `fetch_float_data()` |
| `/v1/news` | `fetch_news_and_grok()` |
| `/v1/screener` | `fetch_last_price()` |
| `/v1/dilution-data` | `fetch_in_play_dilution()` |
| `/v1/ai-chart-analysis` | Chart history badge |
| `/v1/gap-stats` | `fetch_gap_stats()` |
| `/v1/offerings` | `fetch_offerings()` |
| `/v1/ownership` | `fetch_ownership()` |

All Ask Edgar endpoints use the same API key via `API-KEY` header. Base URL: `https://eapi.askedgar.io/v1/` (the `/enterprise/v1/` prefix is deprecated and returns empty responses). All calls go through `_askedgar_get()` for rate limiting + 429 retry.

## Key files

| File | Purpose |
|---|---|
| `das_monitor.py` | Entire app — single Python file, tkinter GUI |
| `dist/AskEdgarV2.exe` | Compiled exe (PyInstaller) — daily launcher |
| `dist/.env` | API keys for the exe (copy from root) |
| `app_icon.ico` | Custom V2 app icon (shield + rising bars + "V2") |
| `app_icon.png` | Icon preview PNG |
| `create_icon.py` | Generates the icon from code |
| `create_shortcut.ps1` | Creates desktop shortcut pointing to exe |
| `requirements.txt` | Python dependencies |
| `run.bat` | Launch script (runs .py directly) |
| `setup.bat` | First-time setup (installs deps, creates .env) |

## Running the App

**Recommended:** Double-click the desktop shortcut "Ask Edgar V2 - Dilution Monitor" (points to `dist/AskEdgarV2.exe`)

**Dev mode:**
```bash
cd "Top-Gainers-Dilution-Monitor-V2-Public"
python das_monitor.py
```

### Rebuilding the exe

After code changes:
```bash
python -m PyInstaller --onefile --windowed --icon=app_icon.ico --name="AskEdgarV2" --add-data="app_icon.ico;." --add-data="app_icon.png;." das_monitor.py
```
Then copy `.env` to `dist/`.

### Recreating the desktop shortcut
```powershell
powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
```

## Common Issues

- **"ASKEDGAR_API_KEY not set"** — The `.env` file is missing or the key is blank. Create `.env` from `.env.example` and paste the API key.
- **"FMP_API_KEY not set"** — Same as above, need FMP premium key.
- **`ModuleNotFoundError: win32gui`** — Run `pip install pywin32`. This only works on Windows.
- **Ask Edgar rate limit errors** — 50 req/min limit. The rate limiter handles this automatically, but if running V1 and V2 simultaneously they share the same API key and limit.
- **"No data" for a ticker** — The ticker may not be in Ask Edgar's database. Normal for non-dilution stocks.
- **FMP legacy endpoint error** — Must use `/stable/` endpoints, not `/api/v3/`.

## Known Issues

### Pre-market data is scraped, not from an official API
**Status:** Partially resolved — stockanalysis.com scraping works but is fragile
**Root cause:** FMP `/stable/biggest-gainers` works pre-market but returns a different (smaller) set of tickers than stockanalysis.com — many small-cap gappers are missing from FMP. Solution: scrape stockanalysis.com pre-market gainers page before 9:30 AM ET, fall back to FMP during regular hours. The scraping depends on HTML structure remaining stable.
**Pre-market price note:** FMP `/stable/quote` `previousClose` is unreliable pre-market (includes after-hours activity). The GC classification derives `prev_close` from the gainer's `todaysChangePerc` instead.
**Remaining options if scraping breaks:**
1. **Webull** — has a free pre-market movers API
2. **Tradier** — has pre-market quotes with a free tier
3. **AskEdgar screener** (`/v1/screener`) — may support sorting by gap % (untested)
4. **FMP biggest-gainers** — works pre-market but with limited ticker coverage

## Customization

This is designed to be modified by AI coding assistants. Common requests:

- Changing window size, colors, fonts
- Adding support for other trading platforms
- Adding/removing data cards
- Changing the polling interval
- Modifying how data is displayed
- Adjusting market cap filter threshold (currently $500M)

## Changes log

### 2026-04-25 — Pre-market gainer filter fix + exe rebuild
- **Bug:** Left-panel scanner showed garbage tickers (+40,000% NWMH, +20,233% AMNC etc. with 100-share volume) during pre-market session.
- **Root cause:** Pre-market branch of `fetch_top_gainers_raw()` in [das_monitor_gc.py:265-273](das_monitor_gc.py#L265-L273) had no volume filter — only `premarket_change > 30%`. Regular-hours branch (line 274-282) was protected by `volume > 500,000` but the PM branch had nothing equivalent.
- **Why this regressed:** V2 (`das_monitor.py`) used stockanalysis.com which pre-filtered the junk server-side. When the source switched to TradingView screener (raw, unfiltered) on 2026-04-06, the regular-hours volume filter was added but the PM filter was missed.
- **Fix:** Added `col("premarket_volume") > 50_000` to the pre-market `Query().where()` clause. 50K PM-volume floor is the rough equivalent of the 500K regular-hours floor (PM has ~10× lower aggregate volume than regular hours).
- **NOT added:** price floor — Setup A trades down to micro-caps and Setup D up to $5, so sub-dollar names are legitimately in the universe. Volume filter alone kills the 1-share-print noise.
- **Verified live** before rebuild: new query returns 6 real gappers (SCNI +73.5%, ELPW +46.3%, CTNT +45%, CAST +35.7%, LIDR +32.5%, INTW +46.5%) instead of the 100-share-print junk. Matches what the screener should look like during PM.
- **Exe rebuilt** via `python -m PyInstaller --noconfirm SmallCapMonitor.spec` using Windows Store Python 3.13 + PyInstaller 6.19.0. New `dist/SmallCapMonitor.exe` is 114.7 MB, written 2026-04-25 07:09. `.env` copied into `dist/`. Old exe was from 2026-04-09 14:39.
- **Follow-up worth doing:** externalise the PM and regular-hours volume thresholds (and `min_pct = 30`) to `setup_config.json` under a new `"gainers"` section so future tuning doesn't need a code edit + exe rebuild. ~10 minutes.
- **Next:** open the app, confirm the gainers list shows only legit names with meaningful PM volume.

### 2026-04-07 (session 1) — PM Short Strategy + Pre-market Fixes + Rename
- **Renamed app** to "Small Cap Monitor" (was "Gap & Crap - Trade Monitor") — title bar, header, AppUserModelID all updated
- **Added PM Short strategy panel** to `das_monitor_gc.py`:
  - `classify_pm_short()` — classifies tickers into Setup A/B/C/D from backtested PM Short strategy (7,045 trades, IS/OOS validated)
  - Setup criteria: gap%, float, mcap, PM volume, price — all from existing data, no extra API calls
  - Card shows: setup tier + name, live ticker values (gap, H2PMH, PM high, float, mcap, PM vol), entry pattern stats (1st drop, lower high, LH confirmed%, time to LH), expected outcome (full fade, cost of waiting, remaining profit, avg bounce), fade hit rate distribution
  - When no setup matches, shows specific reason why (e.g. "MCap 26M > 25M — no setup")
  - PM Short card auto-refreshes every 20s alongside Gap & Crap card
- **New layout**: Top row = PM Short (left, 320px) + Gap & Crap (right, expanding). News & Catalyst moved below.
- **Added session-level Ask Edgar cache** to both `das_monitor.py` and `das_monitor_gc.py` — `_askedgar_cache` dict with 30-minute TTL
- **Fixed Gap & Crap setup not showing pre-market**:
  - **Root cause**: FMP `/stable/quote` `previousClose` includes after-hours activity, giving ~3% gap instead of the real 60-90% gap
  - **Fix**: Pre-market, derive `prev_close` from gainer's `todaysChangePerc`: `prev_close = price / (1 + change_pct / 100)`
- **Renamed `fetch_fmp_pm_high()` → `fetch_fmp_pm_data()`** — returns `(pm_high, last_price)` tuple
- **Confirmed**: FMP biggest-gainers works pre-market but different ticker universe than stockanalysis.com. Stockanalysis stays as pre-market source.
- **Gainers refresh** changed from 60s to 30s
- **Font consistency**: Offering Ability + Recent Offerings columns switched to FONT_MONO/FONT_MONO_BOLD, padding tightened
- **Taskbar icon fix**: added `wm_iconphoto` with PNG as primary method, kept Win32 SendMessageW as fallback
- **Verified live** with RDGT (+94%), HCAI (+188%), SKYQ (no setup — MCap over limit)
- **Next:** Rebuild exe, test after 9:30 AM transition, verify taskbar icon

### 2026-04-06 (session 3) — Gap & Crap Trade Monitor
- **Built `das_monitor_gc.py`** — copy of V2 with Gap & Crap trade panel added (1,900+ lines)
- **Gap & Crap panel**: setup tier (A/B/C/D), position sizing (FULL/HALF/QUARTER), exact dollar levels (stop, entry, BE trail, T1/T2/T3)
- **Setup rules**: v8 Universal — Gap>=40% + Open below PM High (H2PMH<0). Stop +75%, BE -20%, T1-24/T2-30/T3-36.
- **PM high logic**: live FMP aftermarket data pre-market → cached → dayHigh fallback during session. Yellow warning when estimated.
- **Float caching**: Ask Edgar float stored on first load, reused by auto-refresh
- **Auto-refresh**: Gap & Crap prices update every 20 seconds via FMP quote (in-place, no full rebuild)
- **Side-by-side layout**: Row 1 = Gap & Crap (left, fixed 320px) + News & Catalyst (right). Row 2 = Risk Badges + Offering Ability + Recent Offerings (3 equal grid columns with vertical separators)
- **DAS/ToS auto-detect disabled** — ticker selection only via gainers list click or search box
- **Left panel tightened**: 195px width, smaller fonts/padding for compact gainer rows
- Title: "Gap & Crap - Trade Monitor", header: "Gap & Crap Trade Monitor"
- Custom GC icon created (gc_icon.ico) — taskbar icon not yet working (Windows cache issue, saved to memory)
- **Verified live** with PFSA (+155%), SMX (+74%), AIXI (+130%) — all calculations correct
- **Window geometry persistence**: saves size/position on close to `.gc_geometry`, restores on next launch
- **Taskbar icon fixed** — built as `SmallCapMonitor.exe` via PyInstaller, GC shield icon shows correctly
- **Setup config externalized** — all setup parameters in `setup_config.json`, no code changes needed when backtesting produces new values
- **PM high auto-caching** — caches PM highs for all gainers on startup and after every refresh
- **Exe rebuilt** — `dist/SmallCapMonitor.exe` with proper icon, .env path fix, config path fix
- **Config externalized** — `setup_config.json` for all GC + PM Short parameters
- **PM high auto-caching** — runs on startup + after every gainers refresh for all tickers
- **.env path fixed for exe** — looks next to exe first, then script dir
- **Added IO + SI to info line** — IO from Ask Edgar float endpoint (`institutions_percent`), SI from screener (`short_float`). Note: IO may differ from Ask Edgar website display — checking with Ask Edgar team.
- **Switched gainers source to TradingView** — replaced stockanalysis.com scraping + FMP with `tradingview-screener` package. Works pre-market (premarket_change) and regular hours (change). MCap filtered inline (<$500M). FMP as fallback only. Requires `TRADINGVIEW_SESSION_ID` cookie in `.env`.
- **Added IO (Institutional Ownership)** to info area — from Ask Edgar float endpoint (`institutions_percent`). Color-coded: green (<5%), orange (5-15%), red (>15%). Note: may differ from Ask Edgar website — checking with their team.
- **Added SI (Short Interest)** to info line — from Ask Edgar screener endpoint (`short_float`). Verified matches website.
- **Added R/S (Reverse Split) badge** — from Ask Edgar `/v1/reverse-splits`. Shows upcoming (red), recent 30 days (orange), or last 6 months (yellow). Full date shown.
- **Float/MC display** changed to 1 decimal (e.g. 1.3M instead of 1.34M)
- **Deleted folders**: `Ask Edgar app/` (V1 archived), `Top-Gainers-V2-Official/` (reviewed clone)
- **Next:** Test pre-market PM high caching (run before 9:30), verify 20s auto-refresh updates prices correctly for fast movers

### 2026-04-06 (session 2)
- **Removed upfront Ask Edgar enrichment from gainers panel** — left panel now only uses FMP for mcap filtering, no Ask Edgar calls until user clicks a ticker
- **Added `_askedgar_get()` retry wrapper** — all Ask Edgar calls now auto-retry up to 3× on 429, waiting `retry_after` seconds
- Replaced all individual `requests.get()` + rate limiter calls in fetch functions with `_askedgar_get()`
- Removed `_enrich_gainer()`, replaced with `_mcap_filter()` (FMP-only)
- Confirmed FMP `aftermarket-trade` and `aftermarket-quote` work for pre-market data (live tested 4:43 AM ET)
- Documented FMP pre-market endpoint behavior in CLAUDE.md
- **Verified:** Pre-market gainers load from stockanalysis.com, right panel loads Ask Edgar data on click with retry
- **Next:** Consider committing all changes; rebuild exe

### 2026-04-06 (session 1 — uncommitted from 04-02)
- **Pre-market gappers solved**: Added stockanalysis.com scraping for pre-market gainers (before 9:30 AM ET), falls back to FMP during regular hours
- Added `_is_premarket()` time check, `_fetch_premarket_gainers()` scraper, `_fetch_fmp_gainers()`
- Added 30% minimum gain filter for both pre-market and regular session gainers
- Rate limiter (`_askedgar_limiter.wait()`) added to all Ask Edgar API functions
- Added Windows taskbar icon support via `ctypes` (`SetCurrentProcessExplicitAppUserModelID`, `WM_SETICON`, `SetClassLongPtrW`)
- Added Ownership card with insider holdings table (`/v1/ownership` endpoint)
- Fixed premarket price display, updated history badge labels
- Removed hardcoded API key, fixed API key ordering for public release
- Updated README, `.env.example` to reference FMP instead of Massive/Polygon

### 2026-04-02 (session 2)
- Investigated FMP pre-market data gap: confirmed FMP has NO pre-market gappers endpoint
- Documented all working FMP stable endpoints (aftermarket-trade/quote, batch variants)
- Confirmed Massive/Polygon snapshot requires paid plan (NOT_AUTHORIZED)
- Added Known Issues section with potential alternative data sources

### 2026-04-02 (session 1)
- Swapped Massive/Polygon API for FMP (Financial Modeling Prep) — `/stable/biggest-gainers` + `/stable/quote`
- Updated Ask Edgar API URLs from `/enterprise/v1/` to `/v1/` (enterprise prefix deprecated)
- Added market cap filter: only show gainers with mcap < $500M
- Added thread-safe rate limiter (45 req/min) on all Ask Edgar API calls to prevent hitting 50/min limit
- Reduced ThreadPoolExecutor workers from 10 to 3
- Created custom V2 app icon (shield + rising bar chart + "V2")
- Built `AskEdgarV2.exe` via PyInstaller with icon
- Created desktop shortcut (`create_shortcut.ps1`)
- Created project CLAUDE.md

---

*Update this file at end of every session with: what changed, what was verified, what is next.*

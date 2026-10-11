"""Minimal local stock-pattern screener.

Run:  python server.py
Open: http://localhost:8000
"""

from __future__ import annotations

import concurrent.futures
import csv
from datetime import datetime, timedelta
import io
import json
import math
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
# 8000 is commonly occupied by old local development servers. A dedicated port
# makes it obvious that this prototype, rather than a stale process, is serving
# the page.
PORT = 8765
BUILD_ID = "2026-10-11-incomplete-history-results"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=5m&includePrePost=false" # change data point interval here
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockPatternPrototype/1.0)", "Accept": "application/json"}
TEXT_HEADERS = {"User-Agent": "StockPatternPrototype contact@example.com", "Accept": "text/plain"}
NEW_YORK = ZoneInfo("America/New_York")
SCREENING_TIMEZONE = ZoneInfo("Asia/Taipei")
AVERAGE_VOLUME_SESSIONS = 65
MINIMUM_AVERAGE_VOLUME = 100_000
AVERAGE_VOLUME_CACHE: dict = {}
AVERAGE_VOLUME_LOCK = threading.Lock()
REPORT_LOCK = threading.Lock()


class DataSourceError(RuntimeError):
    """An actionable error returned to the browser when free data is unreachable."""


class VolumeHistoryError(ValueError):
    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


def volume_skip_detail(symbol: str, error: Exception) -> dict:
    causes = []
    current = error
    while current is not None:
        causes.append(f"{type(current).__name__}: {current}")
        current = current.__cause__
    return {"symbol": symbol, "error": " | caused by: ".join(causes),
            **getattr(error, "details", {})}


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.URLError as error:
        if isinstance(error.reason, PermissionError) or "WinError 10013" in str(error):
            raise DataSourceError(
                "Windows or security software is blocking Python's outbound HTTPS connection. "
                "Use “Run connection diagnostic” to see which endpoint and Python executable are affected."
            ) from error
        raise DataSourceError("The free data source could not be reached. Try again later.") from error


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers=TEXT_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8-sig")
    except urllib.error.URLError as error:
        if isinstance(error.reason, PermissionError) or "WinError 10013" in str(error):
            raise DataSourceError(
                "Windows or security software is blocking Python's outbound HTTPS connection. "
                "Use “Run connection diagnostic” to see which endpoint and Python executable are affected."
            ) from error
        raise DataSourceError("The free data source could not be reached. Try again later.") from error


def candle_points(symbol: str) -> list[dict]:
    """Get valid five-minute close points for up to the most recent five trading days."""
    encoded = urllib.parse.quote(symbol, safe=".-^")
    result = fetch_json(YAHOO_URL.format(symbol=encoded))["chart"]["result"]
    if not result:
        return []
    chart = result[0]
    closes = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    volumes = chart.get("indicators", {}).get("quote", [{}])[0].get("volume", [])
    timestamps = chart.get("timestamp", [])
    return [
        # Preserve the provider's raw close value. Matching must be exact, not
        # accidentally created by rounding two distinct prices to the same cent.
        {
            "time": stamp,
            "price": float(close),
            "volume": int(volume) if isinstance(volume, (float, int)) and math.isfinite(volume) else None,
        }
        for stamp, close, volume in zip(timestamps, closes, volumes)
        if close is not None and isinstance(close, (float, int)) and math.isfinite(close)
    ]


# Previous pattern logic: preserve this for an easy reversion.
#
# def find_rising_plateaus(points: list[dict]) -> list[dict]:
#     """Find exact-equal close runs followed later by an exact-higher-price run."""
#     plateaus: list[dict] = []
#     index = 0
#     while index < len(points):
#         end = index
#         while end + 1 < len(points) and points[end + 1]["price"] == points[index]["price"]:
#             end += 1
#         if end > index:
#             plateaus.append({
#                 "price": points[index]["price"], "start": points[index]["time"],
#                 "end": points[end]["time"], "points": end - index + 1,
#             })
#         index = end + 1
#
#     matches: list[dict] = []
#     for earlier_index, earlier in enumerate(plateaus):
#         for later in plateaus[earlier_index + 1:]:
#             if later["price"] > earlier["price"]:
#                 matches.append({"first": earlier, "second": later})
#                 break
#     return matches


def find_low_peak_plateau(points: list[dict]) -> list[dict]:
    """Find the first peak before the five-day low and a later higher plateau.

    The first occurrence is used when the five-day low appears more than once.
    A peak is a point whose immediate neighbors are both strictly lower. A plateau
    is two or more consecutive, exactly equal close prices.
    """
    if len(points) < 3:
        return []

    low_index = min(range(len(points)), key=lambda index: points[index]["price"])
    low = points[low_index]

    # Walk backward so the first qualifying point is the peak nearest to L.
    peak_index = next(
        ( # peak logic
            index
            for index in range(low_index - 1, 0, -1)
            if points[index - 1]["price"] < points[index]["price"]
            and points[index + 1]["price"] < points[index]["price"]
        ),
        None,
    )
    if peak_index is None:
        return []
    peak = points[peak_index]

    index = peak_index + 1
    while index < len(points):
        end = index
        while end + 1 < len(points) and points[end + 1]["price"] == points[index]["price"]:
            end += 1
        if end > index+1 and points[index]["price"] > peak["price"]: # adjust end>index to determine how many consecutive points there needs to be
            plateau = {
                "price": points[index]["price"],
                "start": points[index]["time"],
                "end": points[end]["time"],
                "points": end - index + 1,
            }
            return [{"low": low, "peak": peak, "plateau": plateau}]
        index = end + 1
    return []


def price_bounds(query: dict[str, list[str]]) -> tuple[float | None, float | None]:
    """Parse optional, inclusive latest-price bounds from a request query."""
    def number(name: str) -> float | None:
        raw = query.get(name, [""])[0].strip()
        if not raw:
            return None
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a non-negative number.")
        return value

    lower, upper = number("minPrice"), number("maxPrice")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("Minimum price cannot be greater than maximum price.")
    return lower, upper


def prior_day_volume_bounds(query: dict[str, list[str]]) -> tuple[int | None, int | None]:
    """Parse optional inclusive prior-trading-day volume bounds, in shares."""
    def whole_number(name: str) -> int | None:
        raw = query.get(name, [""])[0].strip()
        if not raw:
            return None
        value = float(raw)
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            raise ValueError(f"{name} must be a non-negative whole number.")
        return int(value)

    lower = whole_number("minPriorDayVolume")
    upper = whole_number("maxPriorDayVolume")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("Minimum prior-day volume cannot be greater than maximum prior-day volume.")
    return lower, upper


def average_daily_volume(symbol: str, now: datetime) -> dict:
    """Average the latest 65 completed daily bars; cache successes."""
    now = now.astimezone(NEW_YORK)
    with AVERAGE_VOLUME_LOCK:
        cached = AVERAGE_VOLUME_CACHE.get(symbol)
        if cached and cached[0] == now.date() and now.timestamp() < cached[1]:
            return cached[2].copy()
    query = urllib.parse.urlencode({
        "range": "6mo",
        "interval": "1d", "includePrePost": "false",
    })
    encoded = urllib.parse.quote(symbol, safe=".-^")
    payload = fetch_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?{query}")
    chart_response = payload["chart"]
    if chart_response.get("error"):
        raise VolumeHistoryError(f"Yahoo chart error: {json.dumps(chart_response['error'])}")
    if not chart_response.get("result"):
        raise VolumeHistoryError("Yahoo returned no chart result; session counts are unknown.")
    chart = chart_response["result"][0]
    meta = chart["meta"]
    regular_end = meta.get("currentTradingPeriod", {}).get("regular", {}).get("end")
    # Allow delayed daily volume to settle after the actual session close,
    # including early closes supplied by Yahoo's trading-period metadata.
    settled_at = regular_end + 15 * 60 if regular_end is not None else None
    expires = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if settled_at is not None and now.timestamp() < settled_at:
        expires = min(expires, settled_at)
    timestamps = chart.get("timestamp", [])
    quotes = chart.get("indicators", {}).get("quote") or [{}]
    volumes = quotes[0].get("volume") or []
    daily = {}
    for index, stamp in enumerate(timestamps):
        day = datetime.fromtimestamp(stamp, NEW_YORK).date()
        if day > now.date() or day.weekday() >= 5:
            continue
        if day == now.date():
            if settled_at is None or datetime.fromtimestamp(regular_end, NEW_YORK).date() != day or now.timestamp() < settled_at:
                continue
        volume = volumes[index] if index < len(volumes) else None
        daily[day] = volume
    recent_days = sorted(daily)[-AVERAGE_VOLUME_SESSIONS:]
    recent = [daily[day] for day in recent_days]
    invalid_days = [day.isoformat() for day in recent_days
                    if not isinstance(daily[day], (int, float)) or not math.isfinite(daily[day]) or daily[day] < 0]
    shortfall = max(0, AVERAGE_VOLUME_SESSIONS - len(recent))
    if shortfall or invalid_days:
        valid_volumes = [volume for volume in recent
                         if isinstance(volume, (int, float)) and math.isfinite(volume) and volume >= 0]
        available_average = sum(valid_volumes) / len(valid_volumes) if valid_volumes else None
        reasons = []
        if shortfall:
            reasons.append(f"Only {len(recent)} of 65 required completed sessions returned")
        if invalid_days:
            reasons.append(f"Missing or invalid volume in {len(invalid_days)} selected session(s)")
        raise VolumeHistoryError("; ".join(reasons), returnedSessions=len(daily),
                                 selectedSessions=len(recent), sessionShortfall=shortfall,
                                 invalidVolumeSessions=len(invalid_days), invalidVolumeDates=invalid_days,
                                 missingSessions=shortfall + len(invalid_days),
                                 availableAverageVolume=available_average,
                                 availableAverageSessions=len(valid_volumes))
    result = {"averageVolume65Day": sum(recent) / AVERAGE_VOLUME_SESSIONS, "averageVolumeSessions": AVERAGE_VOLUME_SESSIONS}
    with AVERAGE_VOLUME_LOCK:
        AVERAGE_VOLUME_CACHE[symbol] = (now.date(), expires, result)
    return result.copy()


def filter_average_volume(results: list[dict], diagnostics: list[dict] | None = None) -> tuple[list[dict], int]:
    now = datetime.now(NEW_YORK)

    def evaluate(row):
        try:
            summary = average_daily_volume(row["symbol"], now)
        except Exception as error:
            detail = volume_skip_detail(row["symbol"], error)
            if detail.get("sessionShortfall", 0) > 0:
                return {**row, "volumeHistoryIncomplete": True,
                        "availableAverageVolume": detail.get("availableAverageVolume"),
                        "availableAverageSessions": detail.get("availableAverageSessions", 0),
                        "returnedVolumeSessions": detail["selectedSessions"]}, detail
            return None, detail
        if summary["averageVolume65Day"] < MINIMUM_AVERAGE_VOLUME:
            return None, None
        return {**row, **summary}, None

    passed, unavailable = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        for row, skipped in pool.map(evaluate, results):
            if skipped is not None:
                unavailable += 1
                if diagnostics is not None:
                    diagnostics.append(skipped)
            if row is not None:
                passed.append(row)
    return passed, unavailable


def day_change(points: list[dict]) -> dict:
    """Compare the latest available price with the prior trading session's last close."""
    latest = points[-1]
    latest_day = datetime.fromtimestamp(latest["time"], NEW_YORK).date()
    earlier_points = [
        point for point in points
        if datetime.fromtimestamp(point["time"], NEW_YORK).date() < latest_day
    ]
    if not earlier_points:
        return {"direction": "unavailable", "previousClose": None, "change": None, "priorDayVolume": None}
    prior_day = datetime.fromtimestamp(earlier_points[-1]["time"], NEW_YORK).date()
    prior_points = [
        point for point in earlier_points
        if datetime.fromtimestamp(point["time"], NEW_YORK).date() == prior_day
    ]
    previous_close = prior_points[-1]["price"]
    change = latest["price"] - previous_close
    direction = "up" if change > 0 else "down" if change < 0 else "unchanged"
    valid_volumes = [point["volume"] for point in prior_points if point["volume"] is not None]
    prior_day_volume = sum(valid_volumes) if valid_volumes else None
    return {
        "direction": direction,
        "previousClose": previous_close,
        "change": change,
        "priorDayVolume": prior_day_volume,
    }


def scan_symbol(
    symbol: str,
    minimum_price: float | None = None,
    maximum_price: float | None = None,
    minimum_prior_day_volume: int | None = None,
    maximum_prior_day_volume: int | None = None,
) -> dict | None:
    try:
        points = candle_points(symbol)
        matches = find_low_peak_plateau(points)
        if not points or not matches:
            return None
        latest = points[-1]["price"]
        if (minimum_price is not None and latest < minimum_price) or (maximum_price is not None and latest > maximum_price):
            return None
        change = day_change(points)
        prior_day_volume = change["priorDayVolume"]
        if minimum_prior_day_volume is not None and (
            prior_day_volume is None or prior_day_volume < minimum_prior_day_volume
        ):
            return None
        if maximum_prior_day_volume is not None and (
            prior_day_volume is None or prior_day_volume > maximum_prior_day_volume
        ):
            return None
        return {
            "symbol": symbol,
            "matches": matches,
            "latest": latest,
            "points": len(points),
            "dayChange": change,
        }
    except Exception:
        # Individual ticker failure should not stop an entire market scan.
        return None


def us_symbols() -> list[str]:
    """Return U.S. non-ETF, non-test listings from Nasdaq's symbol directories."""
    symbols: list[str] = []
    source_rows = [
        (fetch_text(NASDAQ_LISTED_URL), "Symbol"),
        (fetch_text(OTHER_LISTED_URL), "ACT Symbol"),
    ]
    for content, symbol_column in source_rows:
        for row in csv.DictReader(io.StringIO(content), delimiter="|"):
            symbol = (row.get(symbol_column) or "").strip()
            # Exclude funds and test entries. The remaining exchange-listed securities
            # include NYSE, Nasdaq, NYSE American, Arca, Cboe, and similar U.S. venues.
            if (
                symbol
                and row.get("ETF") == "N"
                and row.get("Test Issue") == "N"
                and all(char.isalnum() or char in ".-" for char in symbol)
                and len(symbol) <= 8
            ):
                symbols.append(symbol)
    return sorted(set(symbols))


def save_screen_results(results: list[dict], skipped: list[dict] | None = None) -> None:
    """Replace the last screening report with a complete, timestamped symbol list."""
    completed_at = datetime.now(SCREENING_TIMEZONE).isoformat(sep=" ", timespec="seconds")
    symbols = []
    seen = set()
    for result in results:
        if result["symbol"] in seen:
            continue
        seen.add(result["symbol"])
        line = result["symbol"]
        if result.get("volumeHistoryIncomplete"):
            average = result.get("availableAverageVolume")
            average_text = f"{average:,.2f}" if average is not None else "Unavailable"
            line += (f" [INCOMPLETE HISTORY: {result['returnedVolumeSessions']}/65 sessions returned; "
                     f"partial average: {average_text} shares/session over {result['availableAverageSessions']} valid sessions; "
                     "other criteria passed, 65-day volume rule NOT verified]")
        symbols.append(line)
    content = f"Screening completed: {completed_at} (Asia/Taipei)\n\n"
    content += "\n".join(symbols) + "\n" if symbols else "No symbols passed the screening.\n"
    log = f"Screening completed: {completed_at} (Asia/Taipei)\n"
    log += "Stocks skipped because 65-day volume history was unavailable or incomplete.\n"
    log += "Missing sessions = shortfall below 65 returned sessions + selected sessions with invalid/missing volume.\n"
    log += "Unknown means the response could not be evaluated. Entire dates omitted by Yahoo cannot be detected without an exchange calendar.\n\n"
    for entry in skipped or []:
        log += f"Symbol: {entry['symbol']}\nError: {' '.join(entry['error'].split())}\n"
        for label, key in (("Completed sessions returned", "returnedSessions"),
                           ("Selected sessions (maximum 65)", "selectedSessions"),
                           ("Session shortfall", "sessionShortfall"),
                           ("Sessions with missing/invalid volume", "invalidVolumeSessions"),
                           ("Missing usable sessions (of 65)", "missingSessions")):
            log += f"{label}: {entry.get(key, 'Unknown')}\n"
        log += f"Missing/invalid volume dates (New York): {', '.join(entry['invalidVolumeDates']) or 'None'}\n" if "invalidVolumeDates" in entry else "Missing/invalid volume dates (New York): Unknown\n"
        available_average = entry.get("availableAverageVolume")
        if available_average is not None:
            log += f"Average daily volume from available data: {available_average:,.2f} shares/session\n"
            log += f"Valid sessions used for this average: {entry['availableAverageSessions']}\n"
            log += "Partial-history average only; stock remains excluded from the 65-session screening rule.\n"
        else:
            log += "Average daily volume from available data: Unavailable\n"
            log += f"Valid sessions used for this average: {entry.get('availableAverageSessions', 'Unknown')}\n"
        log += "\n"
    if not skipped:
        log += "No stocks skipped for unavailable or incomplete 65-day volume history.\n"
    with REPORT_LOCK:
        write_report("results.txt", content)
        write_report("skipped_stocks.txt", log)


def write_report(filename: str, content: str) -> None:
    # Use a unique temporary file so overlapping scans cannot mix their output,
    # and a failed write does not erase the previous completed report.
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ROOT, delete=False) as report:
            temporary_path = Path(report.name)
            report.write(content)
        temporary_path.replace(ROOT / filename)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def make_handler():
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/health":
                self.send_json({"build": BUILD_ID, "serverFile": str(Path(__file__).resolve())})
                return
            if parsed.path == "/api/diagnose":
                probes = {
                    "Nasdaq-listed symbol directory": NASDAQ_LISTED_URL,
                    "Other U.S.-listed symbol directory": OTHER_LISTED_URL,
                    "Yahoo Finance sample": YAHOO_URL.format(symbol="AAPL"),
                }
                results = []
                for name, url in probes.items():
                    try:
                        headers = TEXT_HEADERS if url in (NASDAQ_LISTED_URL, OTHER_LISTED_URL) else HEADERS
                        request = urllib.request.Request(url, headers=headers)
                        with urllib.request.urlopen(request, timeout=12) as response:
                            results.append({"source": name, "ok": True, "status": response.status})
                    except Exception as error:
                        results.append({"source": name, "ok": False, "error": str(error) or repr(error)})
                self.send_json({"python": sys.executable, "results": results})
                return
            if parsed.path == "/api/symbols":
                try:
                    self.send_json({"symbols": us_symbols()})
                except DataSourceError as error:
                    self.send_json({"error": str(error)}, 502)
                except Exception:
                    self.send_json({"error": "Could not retrieve Nasdaq's symbol universe. Check your internet connection and try again."}, 502)
                return
            if parsed.path == "/api/check":
                query = urllib.parse.parse_qs(parsed.query)
                symbol = query.get("symbol", [""])[0].upper()
                if not symbol:
                    self.send_json({"error": "A symbol is required."}, 400)
                    return
                try:
                    minimum_price, maximum_price = price_bounds(query)
                    minimum_volume, maximum_volume = prior_day_volume_bounds(query)
                    started = time.perf_counter()
                    result = scan_symbol(symbol, minimum_price, maximum_price, minimum_volume, maximum_volume)
                    skipped = []
                    results, unavailable = filter_average_volume([result] if result else [], skipped)
                    save_screen_results(results, skipped)
                    self.send_json({"result": results[0] if results else None, "averageVolumeUnavailable": unavailable, "checked": symbol, "elapsedSeconds": time.perf_counter() - started})
                except ValueError as error:
                    self.send_json({"error": str(error)}, 400)
                except OSError:
                    self.send_json({"error": "Screening completed, but results.txt or skipped_stocks.txt could not be saved in the project folder. Check folder permissions and whether the file is locked."}, 500)
                return
            if parsed.path == "/api/scan":
                query = urllib.parse.parse_qs(parsed.query)
                raw_symbols = query.get("symbols", [""])[0]
                try:
                    started = time.perf_counter()
                    minimum_price, maximum_price = price_bounds(query)
                    minimum_volume, maximum_volume = prior_day_volume_bounds(query)
                    # A full U.S. listing currently fits below this ceiling. Keeping a
                    # ceiling prevents an accidental unbounded request while allowing
                    # the dedicated full-market UI action to scan every loaded symbol.
                    limit = min(max(int(query.get("limit", ["100"])[0]), 1), 15000)
                    symbols = [part.strip().upper() for part in raw_symbols.split(",") if part.strip()]
                    if not symbols:
                        symbols = us_symbols()
                    symbols = symbols[:limit]
                    results = []
                    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                        for result in pool.map(
                            lambda item: scan_symbol(item, minimum_price, maximum_price, minimum_volume, maximum_volume),
                            symbols,
                        ):
                            if result:
                                results.append(result)
                    skipped = []
                    results, unavailable = filter_average_volume(results, skipped)
                    save_screen_results(results, skipped)
                    self.send_json({
                        "results": results,
                        "scanned": len(symbols),
                        "limited": limit,
                        "averageVolumeUnavailable": unavailable,
                        "elapsedSeconds": time.perf_counter() - started,
                    })
                except DataSourceError as error:
                    self.send_json({"error": str(error)}, 502)
                except ValueError as error:
                    self.send_json({"error": str(error)}, 400)
                except OSError:
                    self.send_json({"error": "Screening completed, but results.txt or skipped_stocks.txt could not be saved in the project folder. Check folder permissions and whether the file is locked."}, 500)
                except Exception:
                    self.send_json({"error": "Could not retrieve market data. The free source may be temporarily rate-limited; try a smaller batch later."}, 502)
                return
            return super().do_GET()

        def send_json(self, payload: dict, status: int = 200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            print("[server]", format % args)

    return Handler


if __name__ == "__main__":
    print(f"Serving {BUILD_ID} from {Path(__file__).resolve()} at http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), make_handler()).serve_forever()

# Rising Plateau Screener

A rough local prototype for screening U.S.-listed stocks for this sequence in the last five trading days:

1. Find the first occurrence of the five-day lowest close (L).
2. Find the strict local peak closest to L before it (P).
3. Match when a later plateau of two or more exactly equal closes is above P.

## Run it

```powershell
python server.py
```

Open `http://localhost:8765`.

Each completed screening (one stock, a batch, or all U.S. symbols) overwrites
`results.txt` in the project folder. The report starts with the completion date
and time in Asia/Taipei (UTC+08:00), followed by one passing symbol per line.
If no symbols pass, the report records that instead of keeping old matches.

Each completed screen also replaces `skipped_stocks.txt` with a timestamped report
of stocks excluded because their 65-day volume history could not be evaluated.
It records the symbol, specific error (including underlying network errors),
completed-session count, shortfall below 65 sessions, missing/invalid volume count
and dates, and total missing usable sessions. Counts are marked Unknown when a
request fails or its response cannot be evaluated. Completely omitted trading
dates are not detected without an exchange calendar. Stocks simply below the
100,000-share threshold are not data errors and do not appear in this report.
When there are no such skips, the report says so and clears old entries.

After it opens, the page must show `Server: 2026-10-11-incomplete-history-results`
under the introduction. If it does not, you are viewing a server from another
folder or an older process. Press `Ctrl+C` in every terminal that is running a
server, then run the command above from this project folder and refresh with
`Ctrl+F5`.

If Windows reports `WinError 10013`, Python is being blocked from opening an
outbound HTTPS connection. Click **Run connection diagnostic** in the app. If
both sources are blocked, allow the displayed Python executable through your
security product's outbound-network rules (or ask your IT administrator). This
is a device/network policy, not an API key or administrator-privilege problem.

## Data sources

- **Nasdaq Trader symbol directories** supply the U.S. listed-symbol universe.
- **Yahoo Finance chart endpoint** supplies five days of five-minute close data.

Neither requires an API key, but they are public/unofficial endpoints: intraday data can be delayed, incomplete, blocked, or rate-limited. This is a prototype, not trading infrastructure. For reliable all-market scans, replace the chart adapter with a licensed provider and add persistent caching, retry/backoff, and scheduled jobs.

## Practical scanning

The UI limits a scan to 1,000 symbols and makes five concurrent quote requests. Begin with small batches (for example, 50–100) to be kind to the free endpoint. Loading the symbol universe does not itself download price data.

## 65-day average daily volume

Every single-stock, batch, and full-market screen requires average daily volume
of at least 100,000 shares over the latest 65 completed trading sessions. Exactly
100,000 passes. This mandatory rule replaces the optional three-month minimum.
The results table shows the average and session count.

Only stocks passing the existing rules request additional Yahoo daily history,
with at most five requests running concurrently. Six months of history are fetched,
then the latest 65 completed sessions are selected and their volumes divided by 65.
Weekends and holidays do not count; genuine zero-volume sessions do count.
Today's bar is included only 15 minutes after Yahoo's reported regular-session
close, including early closes. New York dates determine the session boundaries.

Stocks with fewer than 65 sessions, missing/invalid volume in those sessions, or
failed requests are excluded and counted in the scan status. Missing volume outside
the selected 65 sessions does not affect the average. Entire sessions omitted by
the provider cannot be detected without an independent exchange calendar. This uses
Yahoo daily data and may differ from MarketWatch's displayed 65-day average.

Successful averages are cached in memory until the next New York date or the current
session's close plus 15 minutes, whichever comes first. Restarting the server clears
the cache. The results.txt report contains only stocks passing all filters.

## Experimental MarketWatch comparison

Run `python marketwatch_probe.py AAPL MSFT --output marketwatch_test_results.json`
to compare MarketWatch's displayed 65 Day Avg with the existing Yahoo calculation.
This standalone diagnostic does not change the app's Yahoo filter or results.txt.
It reports each source's value, 100,000-share threshold decision, request duration,
and differences when both sources respond. MarketWatch's displayed K/M/B values
are rounded, so comparisons near the threshold may differ from unrounded data.
Blocked requests and missing fields are reported as unavailable, not as zero.
Live access is not guaranteed: the initial direct MarketWatch request returned
HTTP 401 (HTTP Forbidden). The script does not bypass access restrictions.

Run offline checks with `python -m unittest -v test_average_volume test_marketwatch_probe`.

For incomplete volume histories, skipped_stocks.txt also shows the average over
valid returned sessions within the latest 65 selected sessions and the number used.
Zero-volume sessions count; missing/invalid volumes are excluded from this diagnostic
average. No valid values produces Unavailable. This partial average does not qualify
the stock for screening; the 65-session requirement remains in effect.

Stocks passing the other rules with fewer than 65 returned completed sessions now
appear alongside confirmed matches, marked INCOMPLETE HISTORY in the table and
results.txt. Their partial average and valid-session count are shown for review,
even if the partial average is below 100,000; the 65-day volume rule is not verified.
They continue to appear in skipped_stocks.txt. Request failures and invalid volume
within a full set of 65 sessions remain excluded from the displayed results.

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

After it opens, the page must show `Server: 2026-09-01-closest-peak-volume-range-9`
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

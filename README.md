# ⛽ Gas Price Scraper

A Streamlit app that pulls **every gas station GasBuddy lists for a US area** —
with all fuel grades (price + who reported it + when) — and exports it to CSV.

Built around GasBuddy's own GraphQL API, driven from a real headless browser so
the request carries the site's session + CSRF token. That returns the **full
metro list (often 100+ stations)** instead of the ~10 the public pages show.

## Features

- Paste **one or more US ZIP codes** (or `City, State`) and scrape them in a batch
- **All fuel grades** per station: regular, midgrade, premium, diesel, e85, unl88
- Per-grade **price, reporter, and timestamp** (shown as relative time)
- Cheapest / average / spread metrics, cheapest station highlighted
- **Download CSV**, live elapsed timer, **Stop** and **Clear All** buttons

## Quick start

```bash
# 1. install dependencies
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium

# 2a. run the web app
python3 -m streamlit run app.py

# 2b. or use the CLI directly
python3 Gasbuddy.py --location 79401 --all-grades --out prices.csv
python3 Gasbuddy.py --location "Lubbock, TX" --fuel diesel
```

The app opens at <http://localhost:8501>.

## How it works

1. `Gasbuddy.py` — the scraper. Loads GasBuddy in headless Chromium, lifts the
   `gbcsrf` CSRF token, then walks the GraphQL `SearchPrices` cursor through the
   whole metro (throttled ~2.5 s/page to avoid the 429 rate-limit). Falls back to
   HTML parsing if GraphQL is unavailable.
2. `app.py` — the Streamlit UI. Runs `Gasbuddy.py` as a subprocess per location
   (cleanly killable for the Stop button) and renders the combined results.

## Important notes

- **Residential IP recommended.** GasBuddy's bot protection blocks datacenter
  IPs hard. This works great from a home connection; hosting it on shared cloud
  infrastructure (e.g. Streamlit Community Cloud) will likely be blocked
  (403/429) and return no data.
- **USA ZIP codes only** (5-digit). International postal codes aren't supported.
- A bare city name can be ambiguous (`houston` → Houston, MO); prefer a ZIP or
  `City, State`.
- Use responsibly and review GasBuddy's Terms of Service before any public or
  high-volume use. This project is for personal/educational use.

## Files

| File | Purpose |
|------|---------|
| `Gasbuddy.py` | Scraper engine + CLI |
| `app.py` | Streamlit web UI |
| `requirements.txt` | Python dependencies |

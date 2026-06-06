"""
GasBuddy fuel price scraper
============================

Two strategies, tried in order (browser engine):

  1. GraphQL API  -> the PRIMARY path. Run from inside a Playwright page so the
                     request carries the site's session + CSRF token (gbcsrf +
                     apollo-require-preflight; a plain `requests` call gets a
                     "400 Bad Request" without them). Walks the cursor through
                     the WHOLE metro -- often 100+ stations -- with clean JSON:
                     name, address, precise rating, review count, all fuels.
  2. HTML parsing -> fallback if GraphQL fails. The /home (~7) and city
                     (~10) pages render only a slice; this parses whichever
                     loads. Anchored ONLY on stable attributes (the
                     href="/station/..." id, the ld+json block, and class-name
                     prefixes with the volatile "___hash" suffix stripped).

Bot detection
-------------
GasBuddy blocks plain `requests`. Two engines:
  * Preferred: Playwright (a real headless browser).  -> --engine browser
               Reaches GraphQL + the full station list.
  * Lighter:   cloudscraper / requests.               -> --engine requests
               No JS, so no GraphQL; HTML only, ~7 stations, blocked often.

Note: GasBuddy rate-limits (HTTP 429) bursts of GraphQL calls, so pagination
is throttled (~2.5s/page) with backoff. A full metro takes ~1 minute.

Install
-------
    pip install requests beautifulsoup4 cloudscraper pandas
    # for the browser engine (recommended):
    pip install playwright
    playwright install chromium

Usage
-----
    python Gasbuddy.py --location 79401 --fuel regular        # ZIP (best)
    python Gasbuddy.py --location "Lubbock, TX" --fuel diesel
    python Gasbuddy.py --location 90001 --fuel premium --out la.csv

Tip: use a ZIP or "City, State". A bare city name can be ambiguous
("houston" -> Houston, MO). Output: a CSV (and prints a preview).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Iterable, Optional

# ----------------------------------------------------------------------------
# Config: the only "magic numbers" GasBuddy uses, taken from the page's own
# <select id="searchFuelType"> options. These are stable product IDs.
# ----------------------------------------------------------------------------
FUEL_IDS = {
    "regular": 1,
    "midgrade": 2,
    "premium": 3,
    "diesel": 4,
    "e85": 5,
    "unl88": 12,
}

BASE = "https://www.gasbuddy.com"
SEARCH_URL = BASE + "/home"
GRAPHQL_URL = BASE + "/graphql"
PAGE_SIZE = 20  # the "More ... Gas Prices" button steps cursor by 20

# The /home search view renders only ~7 stations and has no working "show more"
# (its cursor is opaque and ignored as a URL param). The dedicated city page
# /gasprices/<state>/<city> renders ~20 — the full public list for an area.
# To reach it we need the state's URL slug, so map US/Canada abbreviations.
STATE_SLUGS = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "DC": "washington-dc", "FL": "florida", "GA": "georgia", "HI": "hawaii",
    "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine",
    "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska",
    "NV": "nevada", "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico",
    "NY": "new-york", "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio",
    "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania", "RI": "rhode-island",
    "SC": "south-carolina", "SD": "south-dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west-virginia", "WI": "wisconsin", "WY": "wyoming", "PR": "puerto-rico",
    # Canadian provinces
    "AB": "alberta", "BC": "british-columbia", "MB": "manitoba",
    "NB": "new-brunswick", "NL": "newfoundland-and-labrador", "NS": "nova-scotia",
    "ON": "ontario", "PE": "prince-edward-island", "QC": "quebec",
    "SK": "saskatchewan",
}


def city_page_url(city: str, region: str) -> Optional[str]:
    """Build /gasprices/<state>/<city> from a city name + state abbreviation."""
    state = STATE_SLUGS.get((region or "").strip().upper())
    if not state or not city:
        return None
    city_slug = re.sub(r"[^a-z0-9]+", "-", city.strip().lower()).strip("-")
    if not city_slug:
        return None
    return f"{BASE}/gasprices/{state}/{city_slug}"

# A normal-looking browser header set. Helps the requests engine a little.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------
@dataclass
class StationPrice:
    station_id: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None          # state
    is_verified: Optional[bool] = None    # the blue "verified" badge
    rating: Optional[float] = None        # star rating
    review_count: Optional[int] = None
    fuel: Optional[str] = None
    price: Optional[float] = None
    reporter: Optional[str] = None        # user id / "Owner"
    posted_time: Optional[str] = None     # "2 Hours Ago"
    payment: Optional[str] = None         # "Cash"/"Credit" badge, if shown
    station_url: Optional[str] = None
    source: Optional[str] = None          # "graphql" or "html"


# ============================================================================
# STRATEGY 1 — GraphQL
# ============================================================================
# GasBuddy's web client fetches station data through a GraphQL endpoint.
# The query below mirrors the public LocationBySearchTerm query the site uses.
# If GasBuddy changes the schema this query may need a tweak, but field names
# are far more stable than CSS hashes.

# The query GasBuddy's own web client sends ("SearchPrices") asks only for id +
# prices. Since we build the request ourselves we ask for the fuller field set
# the schema exposes — name, address, rating, brand. priority:"locality" + the
# cursor are what the site uses to page through the whole metro (100+ stations),
# not just the ~10 the page renders.
GRAPHQL_QUERY = """
query SearchPrices($fuel: Int, $cursor: String, $search: String, $maxAge: Int) {
  locationBySearchTerm(search: $search, priority: "locality") {
    displayName
    stations(fuel: $fuel, cursor: $cursor, maxAge: $maxAge, priority: "locality") {
      count
      cursor { next }
      results {
        id
        name
        starRating
        ratingsCount
        address { line1 locality region postalCode }
        brands { name }
        prices {
          fuelProduct
          credit { price formattedPrice nickname postedTime }
          cash { price formattedPrice nickname postedTime }
        }
      }
    }
  }
}
""".strip()

# Forward map: our fuel grade -> the GraphQL fuelProduct string in results.
FUEL_PRODUCTS = {
    "regular": "regular_gas",
    "midgrade": "midgrade_gas",
    "premium": "premium_gas",
    "diesel": "diesel",
    "e85": "e85",
    "unl88": "unl88",
}


def _gql_result_to_station(st: dict, fuel: str) -> Optional[StationPrice]:
    """Convert one GraphQL station result into a StationPrice for `fuel`."""
    want = FUEL_PRODUCTS.get(fuel, fuel)
    price_entry = next((p for p in (st.get("prices") or [])
                        if p.get("fuelProduct") == want), None)
    addr = st.get("address") or {}
    brands = st.get("brands") or []
    brand_name = brands[0]["name"] if brands else st.get("name")

    chosen, pay = {}, None
    if price_entry:
        credit = price_entry.get("credit") or {}
        cash = price_entry.get("cash") or {}
        # a missing price comes back as 0 / "- - -"; prefer credit, then cash
        if credit.get("price"):
            chosen, pay = credit, "credit"
        elif cash.get("price"):
            chosen, pay = cash, "cash"

    sid = st.get("id")
    return StationPrice(
        station_id=str(sid) if sid is not None else None,
        name=brand_name,
        address=addr.get("line1"),
        city=addr.get("locality"),
        region=addr.get("region"),
        rating=st.get("starRating"),
        review_count=st.get("ratingsCount"),
        fuel=fuel,
        price=_to_float(chosen.get("price")) if chosen.get("price") else None,
        reporter=chosen.get("nickname"),
        posted_time=chosen.get("postedTime"),
        payment=pay,
        station_url=f"{BASE}/station/{sid}" if sid is not None else None,
        source="graphql",
    )


def _dump(path: str, content: str) -> None:
    """Write raw content to a debug file and announce it."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"      [debug] wrote {len(content):,} chars -> {path}", file=sys.stderr)
    except OSError as e:
        print(f"      [debug] could not write {path}: {e}", file=sys.stderr)


# JS run inside the page to POST a GraphQL query with the site's own session.
# The two headers are what a plain `requests` call is missing: gbcsrf (the
# site's CSRF token) and apollo-require-preflight (Apollo's CSRF guard). Without
# them GasBuddy returns "400 Bad Request" before the query is even parsed.
_GQL_FETCH_JS = """
async ([query, csrf, variables]) => {
    const r = await fetch('/graphql', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'gbcsrf': csrf,
            'apollo-require-preflight': 'true',
        },
        body: JSON.stringify({operationName: 'SearchPrices', query, variables}),
    });
    return {status: r.status, json: r.status === 200 ? await r.json() : null};
}
"""


def _gql_result_to_all_grades(st: dict) -> dict:
    """
    Flatten one GraphQL station result into a WIDE row: the station's identity
    plus, for every fuel grade, its price + who reported it + when. A grade the
    station doesn't sell (or has no current report for) comes back as None.
    """
    addr = st.get("address") or {}
    brands = st.get("brands") or []
    sid = st.get("id")
    row = {
        "station_id": str(sid) if sid is not None else None,
        "name": brands[0]["name"] if brands else st.get("name"),
        "address": addr.get("line1"),
        "city": addr.get("locality"),
        "region": addr.get("region"),
        "rating": st.get("starRating"),
        "review_count": st.get("ratingsCount"),
        "station_url": f"{BASE}/station/{sid}" if sid is not None else None,
    }
    by_product = {p.get("fuelProduct"): p for p in (st.get("prices") or [])}
    for grade, product in FUEL_PRODUCTS.items():
        p = by_product.get(product) or {}
        credit, cash = p.get("credit") or {}, p.get("cash") or {}
        chosen = credit if credit.get("price") else (cash if cash.get("price") else {})
        row[f"{grade}_price"] = _to_float(chosen.get("price")) if chosen.get("price") else None
        row[f"{grade}_reporter"] = chosen.get("nickname")
        row[f"{grade}_posted"] = chosen.get("postedTime")
    return row


def _collect_graphql_raw(location: str, fuel_id: int, max_age: int = 0,
                         debug: bool = False) -> list[dict]:
    """
    Walk GasBuddy's GraphQL cursor and return the RAW station result dicts for
    the whole metro. Run from inside a Playwright page so the request carries
    the site's session + CSRF token. Each result already contains ALL fuel
    grades. Returns [] on any failure so callers can fall back to HTML.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return []

    captured: dict = {}

    def grab_csrf(req):
        if (req.url.endswith("/graphql") and req.post_data
                and "SearchPrices" in req.post_data):
            captured["csrf"] = req.headers.get("gbcsrf")

    raw: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
        page = ctx.new_page()
        page.on("request", grab_csrf)
        try:
            # Load /home so the site issues its own SearchPrices call and we can
            # lift the CSRF token from it.
            home = build_search_url(location, fuel_id, 0, max_age)
            page.goto(home, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector('a[href^="/station/"]', timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3000)

            csrf = captured.get("csrf")
            if not csrf:
                if debug:
                    print("      [debug] could not capture gbcsrf token", file=sys.stderr)
                return []

            def fetch_page(search: str, cursor: Optional[str]) -> Optional[dict]:
                variables = {"fuel": fuel_id, "search": search,
                             "cursor": cursor, "maxAge": max_age}
                for _ in range(6):                 # retry on 429 with backoff
                    res = page.evaluate(_GQL_FETCH_JS, [GRAPHQL_QUERY, csrf, variables])
                    if res["status"] == 200:
                        return res["json"]
                    if res["status"] == 429:
                        page.wait_for_timeout(6000)
                        continue
                    if debug:
                        print(f"      [debug] GraphQL HTTP {res['status']}", file=sys.stderr)
                    return None
                if debug:
                    print("      [debug] gave up after repeated 429", file=sys.stderr)
                return None

            # A bare ZIP search is narrow (only a few stations). Resolve it to
            # "City, State" from the first response, then page that — which
            # returns the whole metro.
            first = fetch_page(location, None)
            if not first:
                return []
            loc = (first.get("data") or {}).get("locationBySearchTerm") or {}
            results0 = (loc.get("stations") or {}).get("results") or []
            disp = loc.get("displayName")
            region = (results0[0].get("address") or {}).get("region") if results0 else None
            search = f"{disp}, {region}" if disp and region else location

            cursor: Optional[str] = None
            seen: set = set()
            total = None
            data = first if search.lower() == location.lower() else fetch_page(search, None)
            while data:
                stations = ((data.get("data") or {})
                            .get("locationBySearchTerm") or {}).get("stations") or {}
                total = stations.get("count")
                batch = stations.get("results") or []
                new = 0
                for st in batch:
                    sid = st.get("id")
                    if sid in seen:
                        continue
                    seen.add(sid)
                    raw.append(st)
                    new += 1
                cursor = (stations.get("cursor") or {}).get("next")
                print(f"      GraphQL: {len(raw)}/{total or '?'} stations", end="\r")
                if not cursor or (new == 0 and len(raw) > 0):
                    break
                page.wait_for_timeout(2500)        # throttle to dodge the 429
                data = fetch_page(search, cursor)
            print()
            if total and len(raw) < total:
                print(f"      (collected {len(raw)} of {total}; rate-limit cut it short)")
        except Exception as e:
            if debug:
                print(f"      [debug] GraphQL browser error: {e}", file=sys.stderr)
        finally:
            browser.close()

    return raw


def scrape_graphql(location: str, fuel: str, fuel_id: int,
                   max_age: int = 0, debug: bool = False) -> list[StationPrice]:
    """Full metro station list for a SINGLE fuel grade (CLI --fuel path)."""
    raw = _collect_graphql_raw(location, fuel_id, max_age, debug)
    return [sp for sp in (_gql_result_to_station(st, fuel) for st in raw) if sp]


def scrape_all_grades(location: str, engine: str = "browser",
                      max_age: int = 0, debug: bool = False) -> list[dict]:
    """
    Full metro station list with EVERY fuel grade per station (wide rows).
    Browser engine uses GraphQL (all grades in one pass); if that fails — or for
    the requests engine — it falls back to the HTML path, which can only fill
    the regular grade.
    """
    if engine == "browser":
        print(f"[1/2] GraphQL API (all grades) for '{location}' ...")
        raw = _collect_graphql_raw(location, FUEL_IDS["regular"], max_age, debug)
        if raw:
            print(f"      GraphQL succeeded: {len(raw)} stations.")
            return [_gql_result_to_all_grades(st) for st in raw]
        print("      GraphQL unavailable, falling back to HTML (regular grade only).")

    # Fallback: HTML single-grade. Widen each StationPrice into the all-grades
    # shape with only the regular columns populated.
    sp_rows = scrape(location, "regular", pages=1, engine=engine,
                     max_age=max_age, debug=debug, _skip_graphql=True)
    out: list[dict] = []
    for r in sp_rows:
        row = {
            "station_id": r.station_id, "name": r.name, "address": r.address,
            "city": r.city, "region": r.region, "rating": r.rating,
            "review_count": r.review_count, "station_url": r.station_url,
        }
        for grade in FUEL_PRODUCTS:
            is_reg = grade == "regular"
            row[f"{grade}_price"] = r.price if is_reg else None
            row[f"{grade}_reporter"] = r.reporter if is_reg else None
            row[f"{grade}_posted"] = r.posted_time if is_reg else None
        out.append(row)
    return out


# ============================================================================
# STRATEGY 2 — HTML parsing (fallback)
# ============================================================================
# Anchored on STABLE things only:
#   * panel id="162805"        -> station id
#   * a[href^="/station/"]      -> station id + name
#   * <script type="application/ld+json">  -> address, rating, review count
#   * class *prefix* substring  -> e.g. "StationDisplayPrice-module__price"
#     (we match the part BEFORE the "___hash", which survives redeploys)


def _cls(*names: str):
    """
    BeautifulSoup class matcher: matches if ANY class on the element equals one
    of the given module class names once the hashed suffix is stripped.

    GasBuddy classes look like "StationDisplayPrice-module__price___3rARL"; we
    compare against the part before "___" (here "StationDisplayPrice-module__price").
    Exact match — NOT startswith — because "...module__price" is a prefix of
    "...module__priceContainer", and a startswith match would grab the wrapper
    (whose text bundles price + reporter + time) instead of the price span.
    """
    def matcher(value):
        if not value:
            return False
        classes = value if isinstance(value, list) else value.split()
        return any(c.split("___")[0] == n for c in classes for n in names)
    return matcher


def _to_float(x) -> Optional[float]:
    if x is None:
        return None
    s = re.sub(r"[^\d.]", "", str(x))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _to_int(x) -> Optional[int]:
    if x is None:
        return None
    s = re.sub(r"[^\d]", "", str(x))
    return int(s) if s else None


def parse_html(html: str, fuel_label: str) -> list[StationPrice]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[StationPrice] = []

    # --- 1. Pull ld+json for clean structured data keyed by station ---------
    ldjson_by_addr = {}
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            blob = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = blob if isinstance(blob, list) else [blob]
        for it in items:
            name = it.get("name")
            if name:
                ldjson_by_addr[name] = it

    # --- 2. Each station card is a panel whose class prefix is "panel" and
    #        which carries the numeric station id. We locate via the id +
    #        the station-name anchor, both stable. -----------------------------
    # The station name link is the most reliable anchor:
    station_links = soup.find_all("a", href=re.compile(r"^/station/\d+"))

    seen = set()
    for link in station_links:
        href = link.get("href", "")
        m = re.search(r"/station/(\d+)", href)
        if not m:
            continue
        sid = m.group(1)

        # Walk up to the enclosing station card.
        card = link
        for _ in range(8):
            if card is None:
                break
            cls = card.get("class") if hasattr(card, "get") else None
            # Match ONLY the specific list-item token. "module__station" is too
            # broad — it also matches StationDisplay-module__stationNameHeader,
            # the tiny name element, which would stop the walk short of the card.
            if cls and any("stationListItem" in c for c in cls):
                break
            card = card.parent
        card = card or link.parent

        if sid in seen:
            continue
        seen.add(sid)

        name = link.get_text(strip=True) or None

        sp = StationPrice(
            station_id=sid,
            name=name,
            fuel=fuel_label,
            station_url=BASE + href,
            source="html",
        )

        # verified badge: an <img> whose src mentions "Verified"
        if card.find("img", src=re.compile(r"Verified", re.I)):
            sp.is_verified = True

        # rating: GasBuddy renders five star svgs and fills them via colour,
        # not icon type, so we must count only the GOLD ones. A filled star's
        # parent <span> carries style="color: rgb(246, 204, 28)" (brand gold);
        # data-icon="star" is a full point, "star-half-stroke" is half.
        stars_block = card.find(class_=_cls("StarRating-module__stars"))
        if stars_block:
            full = half = 0
            for svg in stars_block.find_all("svg"):
                parent = svg.find_parent("span")
                if not (parent and "rgb(246, 204, 28)" in (parent.get("style") or "")):
                    continue  # unfilled star
                icon = svg.get("data-icon", "")
                if "half" in icon:
                    half += 1
                elif icon == "star":
                    full += 1
            if full or half:
                sp.rating = full + 0.5 * half

        # review count: span "StationDisplay-module__numberOfReviews"
        rc = card.find(class_=_cls("StationDisplay-module__numberOfReviews"))
        if rc:
            sp.review_count = _to_int(rc.get_text())

        # address: div "StationDisplay-module__address" -> "6020 34th St" <br> "Lubbock, TX"
        addr_el = card.find(class_=_cls("StationDisplay-module__address"))
        if addr_el:
            parts = [t.strip() for t in addr_el.stripped_strings]
            if parts:
                sp.address = parts[0]
            if len(parts) > 1 and "," in parts[1]:
                city, _, region = parts[1].partition(",")
                sp.city = city.strip()
                sp.region = region.strip()

        # price: span "StationDisplayPrice-module__price" (exact — avoids __priceContainer)
        price_el = card.find(class_=_cls("StationDisplayPrice-module__price"))
        if price_el:
            sp.price = _to_float(price_el.get_text())

        # reporter (user id / "Owner"): link "ReportedBy-module__memberLink"
        rep = card.find(class_=_cls("ReportedBy-module__memberLink"))
        if rep:
            sp.reporter = rep.get_text(strip=True) or None

        # posted time: span "ReportedBy-module__postedTime"
        pt = card.find(class_=_cls("ReportedBy-module__postedTime"))
        if pt:
            sp.posted_time = pt.get_text(strip=True) or None

        # enrich from ld+json if the station name matched
        if name and name in ldjson_by_addr:
            it = ldjson_by_addr[name]
            addr = it.get("address") or {}
            sp.address = sp.address or addr.get("streetAddress")
            sp.city = sp.city or addr.get("addressLocality")
            sp.region = sp.region or addr.get("addressRegion")
            agg = it.get("aggregateRating") or {}
            sp.rating = sp.rating or _to_float(agg.get("ratingValue"))
            sp.review_count = sp.review_count or _to_int(agg.get("reviewCount"))

        out.append(sp)

    return out


# ============================================================================
# Fetching layer — two engines
# ============================================================================
def build_search_url(location: str, fuel_id: int, cursor: int, max_age: int = 0) -> str:
    from urllib.parse import urlencode
    q = {"search": location, "fuel": fuel_id, "method": "all", "maxAge": max_age}
    if cursor:
        q["cursor"] = cursor
    return f"{SEARCH_URL}?{urlencode(q)}"


def fetch_html_requests(session, url: str) -> Optional[str]:
    try:
        r = session.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and "station" in r.text.lower():
            return r.text
    except Exception as e:
        print(f"  [requests] error: {e}", file=sys.stderr)
    return None


def _wait_for_stable_stations(page, rounds: int = 25, min_rounds: int = 6) -> None:
    """
    Station cards lazy-load below the fold in batches, so a fixed sleep (or an
    early "looks stable" exit) under-counts: the first batch can sit unchanged
    for a few seconds before the next loads. Scroll to the bottom each round to
    force rendering, and only allow the stability exit after min_rounds so a
    slow second batch isn't missed.
    """
    prev, stable = -1, 0
    for i in range(rounds):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        n = len(page.query_selector_all('a[href^="/station/"]'))
        stable = stable + 1 if n == prev else 0
        prev = n
        if i + 1 >= min_rounds and stable >= 3 and n > 0:
            break


def fetch_html_browser(url: str, select_fuel_id: Optional[int] = None) -> Optional[str]:
    """
    Render a GasBuddy page with Playwright (beats the bot detection that blocks
    plain requests). If select_fuel_id is given, drive the page's hidden
    <select id="fuelType"> to that fuel after load — the city listing page
    ignores a ?fuel= query param and only switches via that control.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
        page = ctx.new_page()
        try:
            # GasBuddy holds long-lived connections open, so "networkidle"
            # never settles and times out. Use domcontentloaded, then wait
            # for station links to appear and the count to stop growing.
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector('a[href^="/station/"]', timeout=20000)
            _wait_for_stable_stations(page)

            if select_fuel_id is not None:
                # The <select> is visually hidden behind a custom dropdown, so
                # set its value in the DOM and fire React's change event.
                page.evaluate(
                    """(v) => {
                        const s = document.querySelector('select#fuelType');
                        if (s) {
                            s.value = String(v);
                            s.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }""",
                    select_fuel_id,
                )
                page.wait_for_timeout(1500)   # let the new prices render
                _wait_for_stable_stations(page)

            html = page.content()
        except Exception as e:
            print(f"  [browser] error: {e}", file=sys.stderr)
            html = page.content()  # return whatever we have
        finally:
            browser.close()
    return html


# ============================================================================
# Orchestration
# ============================================================================
def scrape(location: str, fuel: str, pages: int = 1, engine: str = "browser",
           max_age: int = 0, debug: bool = False,
           _skip_graphql: bool = False) -> list[StationPrice]:
    fuel = fuel.lower()
    if fuel not in FUEL_IDS:
        raise ValueError(f"fuel must be one of {list(FUEL_IDS)}")
    fuel_id = FUEL_IDS[fuel]

    # --- Strategy 1: GraphQL (browser engine only) ----------------------------
    # GraphQL needs the browser's session + CSRF token, and it returns the FULL
    # metro list (100+) rather than the ~10 the HTML pages render — so it's the
    # primary path. The requests engine can't reach it (no JS), so it skips
    # straight to HTML. (_skip_graphql lets the all-grades fallback avoid a
    # redundant second GraphQL attempt.)
    if engine == "browser" and not _skip_graphql:
        print(f"[1/2] GraphQL API for '{location}' / {fuel} ...")
        results = scrape_graphql(location, fuel, fuel_id, max_age, debug)
        if results:
            print(f"      GraphQL succeeded: {len(results)} stations.")
            return results
        print("      GraphQL unavailable, falling back to HTML.")

    # --- Strategy 2: HTML -----------------------------------------------------
    all_rows: list[StationPrice] = []

    if engine == "browser":
        # Step A: the /home search resolves the area (works for ZIP or city)
        # and gives us a handful of stations to read the city/state from.
        home_url = build_search_url(location, fuel_id, 0, max_age)
        print(f"[2/2] Resolving area via search: {home_url}")
        home_html = fetch_html_browser(home_url)
        home_rows = parse_html(home_html, fuel) if home_html else []
        if not home_html:
            print("      Search returned no HTML (blocked or empty).", file=sys.stderr)

        # Step B: upgrade to the dedicated city page for the full ~20-station
        # list. /home renders only ~7 and has no working pagination.
        anchor = next((r for r in home_rows if r.city and r.region), None)
        city_url = city_page_url(anchor.city, anchor.region) if anchor else None
        if city_url:
            print(f"      Full city listing for {anchor.city}, {anchor.region}: {city_url}")
            city_html = fetch_html_browser(city_url, select_fuel_id=fuel_id)
            city_rows = parse_html(city_html, fuel) if city_html else []
            print(f"      city page: {len(city_rows)} stations.")
            if city_rows:
                all_rows = city_rows
            else:
                if debug and city_html:
                    _dump("debug_city.html", city_html)
                all_rows = home_rows  # fall back to the search results
        else:
            print("      Could not resolve a city page; using search results only.")
            all_rows = home_rows

    else:
        # requests engine: page through /home (JS-less; frequently blocked).
        import requests
        try:
            import cloudscraper
            session = cloudscraper.create_scraper()
        except ImportError:
            session = requests.Session()
        for i in range(pages):
            cursor = i * PAGE_SIZE
            url = build_search_url(location, fuel_id, cursor, max_age)
            print(f"[2/2] HTML page {i + 1}/{pages} (cursor={cursor}) via requests: {url}")
            html = fetch_html_requests(session, url)
            if not html:
                print("      No HTML returned (blocked or empty). Stopping.", file=sys.stderr)
                break
            rows = parse_html(html, fuel)
            if not rows:
                print("      No stations parsed on this page. Stopping.")
                if debug:
                    _dump(f"debug_page_{i + 1}.html", html)
                break
            all_rows.extend(rows)
            time.sleep(1.5)

    # de-dup on station id, keep first
    deduped = {}
    for r in all_rows:
        if r.station_id not in deduped:
            deduped[r.station_id] = r
    return list(deduped.values())


def to_csv(rows: Iterable[StationPrice], path: str) -> None:
    import pandas as pd
    df = pd.DataFrame([asdict(r) for r in rows])
    df.to_csv(path, index=False)
    print(f"\nSaved {len(df)} rows -> {path}")
    if not df.empty:
        cols = [c for c in ["name", "price", "address", "city",
                            "reporter", "posted_time", "rating", "review_count"]
                if c in df.columns]
        print(df[cols].to_string(index=False))


def to_csv_wide(rows: list[dict], path: str) -> None:
    """Write the all-grades (wide) rows, sorted cheapest-regular first."""
    import pandas as pd
    df = pd.DataFrame(rows)
    if "regular_price" in df.columns:
        df = df.sort_values("regular_price", na_position="last").reset_index(drop=True)
    df.to_csv(path, index=False)
    print(f"\nSaved {len(df)} stations -> {path}")
    if not df.empty:
        cols = [c for c in ["name", "regular_price", "midgrade_price",
                            "premium_price", "diesel_price", "address", "city"]
                if c in df.columns]
        print(df[cols].head(15).to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description="Scrape fuel prices from GasBuddy.")
    ap.add_argument("--location", required=True,
                    help="City name or ZIP code, e.g. 'lubbock' or '79401'")
    ap.add_argument("--fuel", default="regular", choices=list(FUEL_IDS),
                    help="Fuel grade (default: regular)")
    ap.add_argument("--pages", type=int, default=1,
                    help="requests engine only: how many /home pages to pull. "
                         "The browser engine ignores this and loads the full "
                         "city listing (~20 stations) in one pass.")
    ap.add_argument("--engine", choices=["browser", "requests"], default="browser",
                    help="Scrape engine. 'browser' (Playwright) beats bot "
                         "detection and reaches the full ~20-station city page; "
                         "'requests' is lighter, ~7 stations, and blocked more often.")
    ap.add_argument("--max-age", type=int, default=0,
                    help="maxAge query param (0 = no limit)")
    ap.add_argument("--out", default="gasbuddy_prices.csv", help="Output CSV path")
    ap.add_argument("--all-grades", action="store_true",
                    help="One row per station with EVERY fuel grade's price, "
                         "reporter and time (wide format). Ignores --fuel.")
    ap.add_argument("--debug", action="store_true",
                    help="On a parse failure, dump the raw GraphQL response "
                         "(debug_graphql.json/.txt) and HTML (debug_page_N.html) "
                         "to the current directory for inspection.")
    args = ap.parse_args()

    if args.all_grades:
        rows = scrape_all_grades(args.location, args.engine, args.max_age, args.debug)
        if not rows:
            print("\nNo data scraped. GasBuddy likely blocked the request — "
                  "try --engine browser, or re-run (rate limits are transient).",
                  file=sys.stderr)
            sys.exit(1)
        to_csv_wide(rows, args.out)
    else:
        rows = scrape(args.location, args.fuel, args.pages, args.engine,
                      args.max_age, args.debug)
        if not rows:
            print("\nNo data scraped. GasBuddy likely blocked the request — "
                  "try --engine browser, or reduce request frequency.", file=sys.stderr)
            sys.exit(1)
        to_csv(rows, args.out)


if __name__ == "__main__":
    main()

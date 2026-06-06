"""
Gas Price Scraper — Streamlit front-end
=======================================

A web UI over Gasbuddy.py. Paste one or more locations (ZIP or "City, State"),
pick an engine, click Scrape, and get a table of every station with all fuel
grades (price + who reported it + when), downloadable as CSV.

Run it with:
    python3 -m streamlit run app.py

(Use the same `python3` that has the deps installed — Playwright, pandas, etc.)

Each location is scraped as its own Gasbuddy.py subprocess, run sequentially in
its own process group so the Stop button can abort the whole batch — browser
children included.
"""

import html
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

GRADES = ["regular", "midgrade", "premium", "diesel", "e85", "unl88"]
TABLE_GRADES = ["regular", "midgrade", "premium", "diesel"]   # shown in the UI table
GRADE_LABEL = {"regular": "REGULAR", "midgrade": "MID",
               "premium": "PREMIUM", "diesel": "DIESEL"}

HERE = Path(__file__).resolve().parent
SCRAPER = HERE / "Gasbuddy.py"
TMP = Path(tempfile.gettempdir())

st.set_page_config(page_title="Gas Price Scraper", page_icon="⛽", layout="centered")

# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #
st.markdown("""
<style>
.block-container {max-width: 880px; padding-top: 2.2rem;}
#MainMenu, footer {visibility: hidden;}

.gp-header {display:flex; align-items:center; gap:14px; margin-bottom:4px;}
.gp-icon {width:46px; height:46px; border-radius:13px; background:#e8482f;
          display:flex; align-items:center; justify-content:center;
          box-shadow:0 6px 16px rgba(232,72,47,.35);}
.gp-icon div {width:18px; height:18px; border-radius:50%; background:#fff;}
.gp-title {font-size:30px; font-weight:800; letter-spacing:-.02em; margin:0;}
.gp-sub {color:#6b7280; font-size:15px; margin:8px 0 22px; line-height:1.5;}

/* metrics */
.gp-metrics {display:flex; gap:46px; align-items:flex-end;}
.gp-metric .lab {font-size:12px; color:#9ca3af; text-transform:uppercase;
                 letter-spacing:.05em; margin-bottom:2px;}
.gp-metric .val {font-size:25px; font-weight:800;
                 font-family:'SF Mono',Menlo,Consolas,monospace;}
.gp-metric .val.green {color:#16a34a;}

/* table */
.gp-wrap {border:1px solid #ececec; border-radius:14px; overflow:hidden;
          margin-top:6px; box-shadow:0 1px 3px rgba(0,0,0,.04);}
.gp-scroll {max-height:560px; overflow-y:auto;}
table.gp {width:100%; border-collapse:collapse; font-size:14px;}
table.gp th {position:sticky; top:0; background:#fafafa; z-index:1;
             text-align:right; color:#9ca3af; font-size:11px; font-weight:600;
             text-transform:uppercase; letter-spacing:.05em;
             padding:12px 16px; border-bottom:1px solid #ececec;}
table.gp th.l {text-align:left;}
table.gp td {padding:14px 16px; border-bottom:1px solid #f4f4f4; text-align:right;
             font-family:'SF Mono',Menlo,Consolas,monospace; font-weight:600;
             color:#111827; white-space:nowrap;}
table.gp tr:last-child td {border-bottom:none;}
table.gp td.l {text-align:left; font-family:inherit; font-weight:400;}
table.gp tr.cheapest {background:#f0fdf4;}
.gp-name {font-weight:700; color:#111827;}
.gp-addr {color:#9ca3af; font-size:12.5px; margin-top:2px;}
.gp-badge {background:#16a34a; color:#fff; font-size:10px; font-weight:700;
           padding:2px 7px; border-radius:5px; margin-left:8px;
           letter-spacing:.03em; vertical-align:middle;}
.gp-green {color:#16a34a;}
.gp-muted {color:#d1d5db; font-weight:500;}
.gp-rep {font-family:inherit; font-weight:400; color:#6b7280; font-size:13px;}

/* buttons */
div[data-testid="stButton"] button {border-radius:9px; font-weight:600; height:46px;}
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="gp-header"><div class="gp-icon"><div></div></div>'
    '<div class="gp-title">Gas Price Scraper</div></div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="gp-sub">Pull every station GasBuddy lists for an area — all '
    'fuel grades, who reported each price and when — then export to CSV.</div>',
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
ss = st.session_state
ss.setdefault("status", "idle")     # idle | running | done | aborted | error
ss.setdefault("proc", None)
ss.setdefault("queue", [])
ss.setdefault("partial", [])        # list of per-location DataFrames

running = ss.status == "running"

# --------------------------------------------------------------------------- #
# Inputs — Location(s) + Engine
# --------------------------------------------------------------------------- #
c1, c2 = st.columns([3, 1])
with c1:
    loc_text = st.text_area(
        "LOCATION(S)", value=ss.get("loc_text", "79401"), height=90,
        disabled=running,
        help="USA ZIP codes only — one or more 5-digit US ZIP codes (e.g. "
             "79401), separated by commas or new lines. Each is scraped in "
             "turn. International postal codes are not supported.",
        placeholder="79401, 78701\n75201",
    )
    st.caption("🇺🇸 USA ZIP codes only (5-digit). Separate multiple with commas or new lines.")
with c2:
    engine = st.selectbox(
        "ENGINE", ["browser", "requests"], disabled=running,
        index=["browser", "requests"].index(ss.get("engine", "browser")),
        help="browser: full metro list w/ all grades via GraphQL (~1 min each). "
             "requests: lightweight, ~7 stations, regular grade only.",
    )

b1, b2, b3, _ = st.columns([1, 1, 1, 2])
start = b1.button("🔍 Scrape", type="primary", disabled=running, use_container_width=True)
stop = b2.button("⏹ Stop", disabled=not running, use_container_width=True)
clear = b3.button("🗑 Clear All", use_container_width=True,
                  help="Stop any run, clear results, caches and temp files.")


def parse_locations(text: str) -> list[str]:
    parts = [x.strip() for x in re.split(r"[\n,]+", text or "") if x.strip()]
    seen, out = set(), []
    for p in parts:                       # dedupe, preserve order
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def start_subprocess(location: str):
    csv_path = TMP / "gps_cur.csv"
    log_path = TMP / "gps_cur.log"
    if csv_path.exists():
        csv_path.unlink()
    cmd = [sys.executable, str(SCRAPER), "--location", location,
           "--engine", ss.engine, "--all-grades", "--out", str(csv_path)]
    log_file = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                            text=True, cwd=str(HERE), start_new_session=True)
    ss.proc = proc
    ss.cur_loc = location
    ss.cur_csv = str(csv_path)
    ss.cur_log = str(log_path)
    ss.cur_start = time.time()


# --------------------------------------------------------------------------- #
# Start / Stop
# --------------------------------------------------------------------------- #
if start:
    locations = parse_locations(loc_text)
    if not locations:
        st.error("Please enter at least one location.")
        st.stop()
    ss.loc_text = loc_text
    ss.engine = engine
    ss.queue = locations
    ss.total = len(locations)
    ss.done_n = 0
    ss.partial = []
    ss.proc = None
    ss.start_time = time.time()
    ss.status = "running"
    st.rerun()

if stop and ss.proc is not None:
    try:
        os.killpg(os.getpgid(ss.proc.pid), signal.SIGTERM)
        ss.proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(ss.proc.pid), signal.SIGKILL)
        except Exception:
            pass
    ss.proc = None
    ss.queue = []
    ss.end_time = time.time()
    ss.status = "aborted"
    st.rerun()

if clear:
    # kill any running scrape, then wipe all state, caches and temp files.
    if ss.proc is not None:
        try:
            os.killpg(os.getpgid(ss.proc.pid), signal.SIGKILL)
        except Exception:
            pass
    for f in ("gps_cur.csv", "gps_cur.log", "gasbuddy_streamlit.csv",
              "gasbuddy_streamlit.log"):
        try:
            (TMP / f).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
    try:
        st.cache_data.clear()
        st.cache_resource.clear()
    except Exception:
        pass
    ss.clear()                 # drop every session_state key -> fresh start
    st.rerun()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def fmt_dur(secs: float) -> str:
    secs = int(secs)
    return f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60:02d}s"


def rel_time(iso) -> str:
    if not iso or (isinstance(iso, float) and pd.isna(iso)):
        return ""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return ""
    delta = (datetime.now(timezone.utc) - t).total_seconds()
    delta = max(delta, 0)
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def price_cell(v, green=False) -> str:
    if v is None or pd.isna(v):
        return '<td class="gp-muted">- - -</td>'
    cls = "gp-green" if green else ""
    return f'<td class="{cls}">${v:.2f}</td>'


def render_table(df: pd.DataFrame) -> str:
    cheapest = df["regular_price"].min() if "regular_price" in df else None
    head = ('<tr><th class="l">Station</th>'
            + "".join(f"<th>{GRADE_LABEL[g]}</th>" for g in TABLE_GRADES)
            + '<th>Reported</th></tr>')
    body = []
    for _, r in df.iterrows():
        reg = r.get("regular_price")
        is_cheap = pd.notna(reg) and cheapest is not None and reg == cheapest
        badge = '<span class="gp-badge">CHEAPEST</span>' if is_cheap else ""
        name = html.escape(str(r.get("name") or "—"))
        addr_bits = [str(r.get("address") or "").strip(), str(r.get("city") or "").strip()]
        addr = html.escape(", ".join([b for b in addr_bits if b]))
        # reporter + time come from the regular grade (fall back to any grade)
        reporter, posted = r.get("regular_reporter"), r.get("regular_posted")
        if not (isinstance(reporter, str) and reporter):
            for g in TABLE_GRADES[1:]:
                if isinstance(r.get(f"{g}_reporter"), str) and r.get(f"{g}_reporter"):
                    reporter, posted = r.get(f"{g}_reporter"), r.get(f"{g}_posted")
                    break
        rep = ""
        if isinstance(reporter, str) and reporter:
            rt = rel_time(posted)
            rep = html.escape(reporter) + (f" · {rt}" if rt else "")
        cells = "".join(
            price_cell(r.get(f"{g}_price"), green=(g == "regular" and is_cheap))
            for g in TABLE_GRADES
        )
        body.append(
            f'<tr class="{"cheapest" if is_cheap else ""}">'
            f'<td class="l"><span class="gp-name">{name}</span>{badge}'
            f'<div class="gp-addr">{addr}</div></td>'
            f'{cells}<td class="gp-rep">{rep}</td></tr>'
        )
    return (f'<div class="gp-wrap"><div class="gp-scroll"><table class="gp">'
            f'{head}{"".join(body)}</table></div></div>')


def combined() -> pd.DataFrame:
    if not ss.partial:
        return pd.DataFrame()
    df = pd.concat(ss.partial, ignore_index=True)
    if "regular_price" in df.columns:
        df = df.sort_values("regular_price", na_position="last").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# Running — drive the batch, live timer
# --------------------------------------------------------------------------- #
if ss.status == "running":
    # advance the queue / start the next location
    if ss.proc is None:
        if ss.queue:
            start_subprocess(ss.queue.pop(0))
        else:
            ss.end_time = time.time()
            ss.status = "done"
            st.rerun()

    proc = ss.proc
    if proc is not None:
        if proc.poll() is None:
            elapsed = time.time() - ss.start_time
            t1, t2 = st.columns([1, 3])
            t1.markdown(
                f'<div class="gp-metric"><div class="lab">⏱ Elapsed</div>'
                f'<div class="val">{fmt_dur(elapsed)}</div></div>',
                unsafe_allow_html=True)
            t2.info(f"Scraping **{ss.done_n + 1} of {ss.total}** — "
                    f"`{ss.cur_loc}` ({ss.engine}). Click **Stop** to abort.")
            try:
                tail = "\n".join(Path(ss.cur_log).read_text().splitlines()[-10:])
            except Exception:
                tail = ""
            if tail.strip():
                st.code(tail)
            time.sleep(1)
            st.rerun()
        else:
            # current location finished — collect its CSV, move on
            if proc.returncode == 0 and Path(ss.cur_csv).exists():
                try:
                    piece = pd.read_csv(ss.cur_csv)
                    piece.insert(0, "search", ss.cur_loc)
                    ss.partial.append(piece)
                except Exception:
                    pass
            ss.done_n += 1
            ss.proc = None
            st.rerun()

# --------------------------------------------------------------------------- #
# Results (done / aborted / error)
# --------------------------------------------------------------------------- #
if ss.status in ("done", "aborted"):
    df = combined()
    dur = ss.get("end_time", ss.start_time) - ss.start_time

    if df.empty:
        msg = f"⏹ Aborted after {fmt_dur(dur)}." if ss.status == "aborted" \
            else f"Finished in {fmt_dur(dur)} but found no stations."
        st.warning(msg + " Try a ZIP code, or re-run (rate limits are transient).")
        st.stop()

    reg = df["regular_price"] if "regular_price" in df else pd.Series(dtype=float)
    note = (f"⏹ Stopped after {fmt_dur(dur)} — partial results below."
            if ss.status == "aborted"
            else f"✅ Done in {fmt_dur(dur)} across {ss.get('total', 1)} location(s).")
    (st.warning if ss.status == "aborted" else st.success)(note)

    # show which areas resolved
    areas = []
    for s in df["search"].unique() if "search" in df else []:
        sub = df[df["search"] == s]
        city = sub["city"].dropna().iloc[0] if sub["city"].notna().any() else ""
        region = sub["region"].dropna().iloc[0] if sub["region"].notna().any() else ""
        areas.append(f"{s} · {city}, {region}".rstrip(", "))
    if areas:
        st.caption("  •  ".join(areas))

    # metrics + download row
    mcol, dcol = st.columns([3, 1])
    spread = (reg.max() - reg.min()) if reg.notna().any() else None
    metrics_html = '<div class="gp-metrics">'
    metrics_html += f'<div class="gp-metric"><div class="lab">Stations</div><div class="val">{len(df)}</div></div>'
    if reg.notna().any():
        metrics_html += f'<div class="gp-metric"><div class="lab">Cheapest</div><div class="val green">${reg.min():.2f}</div></div>'
        metrics_html += f'<div class="gp-metric"><div class="lab">Average</div><div class="val">${reg.mean():.2f}</div></div>'
        metrics_html += f'<div class="gp-metric"><div class="lab">Spread</div><div class="val">${spread:.2f}</div></div>'
    metrics_html += '</div>'
    mcol.markdown(metrics_html, unsafe_allow_html=True)
    with dcol:
        st.download_button(
            "⬇️ Download CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name="gas_prices.csv", mime="text/csv",
            use_container_width=True,
        )

    st.markdown(render_table(df), unsafe_allow_html=True)

elif ss.status == "error":
    dur = ss.get("end_time", ss.start_time) - ss.start_time
    st.error(f"Scrape failed after {fmt_dur(dur)}. Try a ZIP, or re-run.")

import requests
import feedparser
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# -----------------------
# Page setup
# -----------------------
st.set_page_config(page_title="Outage Watch", layout="wide")
st.title("Outage Watch")
st.caption("Auto-refreshes every 60s; network responses cached for 60s.")
st_autorefresh(interval=60_000, key="auto_refresh")

# -----------------------
# Providers to poll
# -----------------------
PROVIDERS = [
    {
        "name": "AWS",
        "kind": "rss",
        "url": "https://status.aws.amazon.com/rss/all.rss",
        "status_page": "https://health.aws.amazon.com/health/status",
    },
    {
        "name": "Azure",
        "kind": "rss",
        "url": "https://azurestatuscdn.azureedge.net/en-us/status/feed/",
        "status_page": "https://azure.status.microsoft",
    },
    {
        "name": "Google Cloud (GCP)",
        "kind": "gcp_incidents",
        "url": "https://status.cloud.google.com/incidents.json",
        "status_page": "https://status.cloud.google.com",
    },
    {
        "name": "Microsoft 365",
        "kind": "link_only",
        "url": "",  # not used
        "status_page": "https://status.cloud.microsoft",
        "note": "Public status page only (tenant service health API requires admin access).",
    },
]

# -----------------------
# Networking (cached)
# -----------------------
DEFAULT_TIMEOUT = 12

@st.cache_data(ttl=60, show_spinner=False)
def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    headers = {
        "User-Agent": "OutageWatch/1.0 (+streamlit)",
        "Accept": "*/*",
    }
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.content

def fetch_json(url: str):
    raw = fetch_url(url)
    return requests.models.complexjson.loads(raw.decode("utf-8", errors="replace"))

# -----------------------
# Summarizers
# -----------------------
def _rss_level_from_title(title_lower: str) -> str:
    major_words = ["major outage", "outage", "unavailable", "down"]
    degraded_words = [
        "degraded", "investigating", "identified", "monitoring",
        "issue", "error", "latency", "impact", "connectivity",
        "disruption", "partial"
    ]
    resolved_words = ["resolved", "operating normally", "recovered", "restored"]

    if any(w in title_lower for w in resolved_words):
        return "ok"
    if any(w in title_lower for w in major_words):
        return "major"
    if any(w in title_lower for w in degraded_words):
        return "degraded"
    return "ok"

def summarize_rss(url):
    try:
        content = fetch_url(url)
        feed = feedparser.parse(content)
    except Exception as e:
        return "unknown", [f"Fetch/parse error: {e}"]

    entries = feed.entries or []
    if not entries:
        return "ok", []

    window = entries[:5]
    levels = []
    details = []

    for e in window:
        t = unescape(getattr(e, "title", "Update"))
        lvl = _rss_level_from_title(t.lower())
        levels.append(lvl)

        ts = getattr(e, "published", "") or getattr(e, "updated", "")
        details.append(f"{t} — {ts}")

    if "major" in levels:
        level = "major"
    elif "degraded" in levels:
        level = "degraded"
    else:
        level = "ok"

    return level, details[:3]

def summarize_gcp_incidents(url):
    try:
        incidents = fetch_json(url)  # incidents.json is an array
    except Exception as e:
        return "unknown", [f"Fetch/parse error: {e}"]

    if not incidents:
        return "ok", []

    active = []
    for inc in incidents:
        end = inc.get("end") or inc.get("resolved")
        if not end:
            active.append(inc)

    if not active:
        return "ok", []

    level = "degraded"
    details = []
    for inc in active[:3]:
        title = inc.get("title") or inc.get("service_name") or "Incident"
        begin = inc.get("begin") or inc.get("start") or ""
        severity = (inc.get("severity") or inc.get("impact") or "").lower()

        if "high" in severity or "major" in severity:
            level = "major"

        details.append(f"{title} — started: {begin} — severity/impact: {severity or 'n/a'}")

    return level, details

def summarize_link_only(provider):
    note = provider.get("note") or "See official status page."
    return "info", [note]

def summarize(provider):
    kind = provider["kind"]
    url = provider.get("url", "")

    if kind == "rss":
        return summarize_rss(url)
    if kind == "gcp_incidents":
        return summarize_gcp_incidents(url)
    if kind == "link_only":
        return summarize_link_only(provider)

    return "unknown", [f"Unsupported provider kind: {kind}"]

# -----------------------
# UI controls
# -----------------------
severity_order = {"major": 0, "degraded": 1, "unknown": 2, "info": 3, "ok": 4}
emoji = {"ok": "✅", "degraded": "🟡", "major": "🔴", "unknown": "⚪", "info": "🔵"}

left, mid, right = st.columns([2, 2, 3])
with left:
    show = st.multiselect(
        "Show severities",
        options=["major", "degraded", "unknown", "info", "ok"],
        default=["major", "degraded", "unknown", "info", "ok"],
    )
with mid:
    search = st.text_input("Search providers", value="", placeholder="e.g., AWS, Azure, GCP, Microsoft").strip().lower()
with right:
    st.write("")
    st.caption("Click provider names to open official status pages.")

st.divider()

# -----------------------
# Poll in parallel
# -----------------------
results = []
max_workers = min(12, max(4, len(PROVIDERS)))

with ThreadPoolExecutor(max_workers=max_workers) as ex:
    future_map = {ex.submit(summarize, p): p for p in PROVIDERS}
    for fut in as_completed(future_map):
        p = future_map[fut]
        try:
            level, details = fut.result()
        except Exception as e:
            level, details = "unknown", [f"Unhandled error: {e}"]
        results.append({**p, "level": level, "details": details})

results.sort(key=lambda r: (severity_order.get(r["level"], 99), r["name"].lower()))

# -----------------------
# Render cards (clickable provider -> official status page)
# -----------------------
for r in results:
    if r["level"] not in show:
        continue
    if search and search not in r["name"].lower():
        continue

    c1, c2 = st.columns([2, 6])
    with c1:
        title = r["name"]
        link = r.get("status_page")

        if link:
            st.subheader(f"{emoji.get(r['level'], '⚪')} [{title}]({link})")
            st.link_button("Open official status page", link)
        else:
            st.subheader(f"{emoji.get(r['level'], '⚪')} {title}")

        st.caption(f"Kind: {r['kind']}")
        st.caption("Last checked: just now")

    with c2:
        if r["level"] == "ok":
            st.success("Operational")
        elif r["level"] == "degraded":
            st.warning("Degraded / incident or recent issue")
        elif r["level"] == "major":
            st.error("Major outage or incident")
        elif r["level"] == "info":
            st.info("Check official status page")
        else:
            st.info("Unknown")

        if r["details"]:
            with st.expander("Details", expanded=(r["level"] != "ok")):
                for d in r["details"]:
                    st.write("• " + d)

    st.divider()

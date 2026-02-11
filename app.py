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
    # Statuspage-powered (JSON)
    {"name": "Cloudflare", "kind": "statuspage", "url": "https://www.cloudflarestatus.com/api/v2/summary.json"},
    {"name": "GitHub",     "kind": "statuspage", "url": "https://www.githubstatus.com/api/v2/summary.json"},
    {"name": "OpenAI",     "kind": "statuspage", "url": "https://status.openai.com/api/v2/summary.json"},

    # Slack official status API (JSON)
    {"name": "Slack", "kind": "slack", "url": "https://slack-status.com/api/v2.0.0/current"},

    # RSS feeds
    {"name": "Azure", "kind": "rss", "url": "https://azurestatuscdn.azureedge.net/en-us/status/feed/"},
    {"name": "AWS",   "kind": "rss", "url": "https://status.aws.amazon.com/rss/all.rss"},
]

# -----------------------
# Networking (cached)
# -----------------------
DEFAULT_TIMEOUT = 12

@st.cache_data(ttl=60, show_spinner=False)
def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """
    Single cached fetch for ALL providers. Caches raw bytes for 60s.
    """
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
def summarize_statuspage(url):
    try:
        data = fetch_json(url)
    except Exception as e:
        return "unknown", [f"Fetch/parse error: {e}"]

    status = data.get("status", {}) or {}
    indicator = status.get("indicator", "none")

    incidents = (data.get("incidents") or []) + (data.get("scheduled_maintenances") or [])

    major = indicator in {"major", "critical"} or any((i.get("impact") in {"major", "critical"}) for i in incidents)
    degraded = (indicator == "minor") or bool(incidents)

    level = "major" if major else ("degraded" if degraded else "ok")

    details = []
    for i in incidents[:3]:
        title = i.get("name", "Incident")
        impact = i.get("impact", "n/a")
        upd = i.get("updated_at") or i.get("created_at") or ""
        details.append(f"{title} — impact: {impact} — updated: {upd}")

    return level, details

def summarize_slack(url):
    try:
        data = fetch_json(url)
    except Exception as e:
        return "unknown", [f"Fetch/parse error: {e}"]

    status = (data.get("status") or "ok").lower()
    incidents = data.get("active_incidents") or data.get("incidents") or []

    if status == "ok" and not incidents:
        return "ok", []

    # If there are incidents, assume degraded unless explicitly outage
    level = "major" if any((inc.get("type") or "").lower() == "outage" for inc in incidents) else "degraded"

    details = []
    for inc in incidents[:3]:
        details.append(
            f"{inc.get('title','Incident')} — status: {inc.get('status','')} — updated: {inc.get('date_updated','')}"
        )

    return level, details

def _rss_level_from_title(title_lower: str) -> str:
    # words that usually indicate an active problem
    major_words = ["major outage", "outage", "unavailable", "down"]
    degraded_words = ["degraded", "investigating", "identified", "monitoring", "issue", "error", "latency",
                      "impact", "connectivity", "disruption", "partial"]

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

    # Evaluate a small window of the most recent items:
    # - If any look major, mark major
    # - Else if any look degraded, mark degraded
    # - Else ok
    window = entries[:5]
    levels = []
    details = []

    for e in window:
        t = unescape(getattr(e, "title", "Update"))
        tl = t.lower()
        lvl = _rss_level_from_title(tl)
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

def summarize(provider):
    kind, url = provider["kind"], provider["url"]
    if kind == "statuspage":
        return summarize_statuspage(url)
    if kind == "slack":
        return summarize_slack(url)
    if kind == "rss":
        return summarize_rss(url)
    return "unknown", [f"Unsupported provider kind: {kind}"]

# -----------------------
# UI controls
# -----------------------
severity_order = {"major": 0, "degraded": 1, "unknown": 2, "ok": 3}

left, mid, right = st.columns([2, 2, 3])
with left:
    show = st.multiselect(
        "Show severities",
        options=["major", "degraded", "unknown", "ok"],
        default=["major", "degraded", "unknown", "ok"],
    )
with mid:
    search = st.text_input("Search providers", value="", placeholder="e.g., AWS, Azure, Slack").strip().lower()
with right:
    st.write("")  # spacing
    st.caption("Tip: add providers in `PROVIDERS` and they’ll be polled in parallel.")

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

# sort so major items float to top
results.sort(key=lambda r: (severity_order.get(r["level"], 99), r["name"].lower()))

# -----------------------
# Render cards
# -----------------------
emoji = {"ok": "✅", "degraded": "🟡", "major": "🔴", "unknown": "⚪"}

for r in results:
    if r["level"] not in show:
        continue
    if search and search not in r["name"].lower():
        continue

    c1, c2 = st.columns([2, 6])
    with c1:
        st.subheader(f"{emoji.get(r['level'], '⚪')} {r['name']}")
        st.caption(f"Kind: {r['kind']}")
        st.caption("Last checked: just now")
    with c2:
        if r["level"] == "ok":
            st.success("Operational")
        elif r["level"] == "degraded":
            st.warning("Degraded / incident or recent issue")
        elif r["level"] == "major":
            st.error("Major outage or incident")
        else:
            st.info("Unknown")

        if r["details"]:
            with st.expander("Details", expanded=(r["level"] != "ok")):
                for d in r["details"]:
                    st.write("• " + d)

    st.divider()

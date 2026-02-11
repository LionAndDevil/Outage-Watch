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
        "kind": "m365_graph",
        "url": "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/healthOverviews",
        "status_page": "https://status.cloud.microsoft",
    },

    # Optional extras (keep or delete as you like)
    {
        "name": "Cloudflare",
        "kind": "statuspage",
        "url": "https://www.cloudflarestatus.com/api/v2/summary.json",
        "status_page": "https://www.cloudflarestatus.com",
    },
    {
        "name": "OpenAI",
        "kind": "statuspage",
        "url": "https://status.openai.com/api/v2/summary.json",
        "status_page": "https://status.openai.com",
    },
]

# -----------------------
# Networking (cached)
# -----------------------
DEFAULT_TIMEOUT = 12

@st.cache_data(ttl=60, show_spinner=False)
def fetch_url(url: str, headers_items=None, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """
    Cached fetch for ALL providers. Caches raw bytes for 60s.
    headers_items is a tuple of (key, value) pairs so Streamlit can cache it.
    """
    headers = {
        "User-Agent": "OutageWatch/1.0 (+streamlit)",
        "Accept": "*/*",
    }
    if headers_items:
        for k, v in headers_items:
            headers[k] = v

    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.content

def fetch_json(url: str, headers: dict | None = None):
    headers_items = tuple(sorted(headers.items())) if headers else None
    raw = fetch_url(url, headers_items=headers_items)
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
        # if "end" (or similar) is absent, treat as ongoing
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

def summarize_m365_graph(url):
    token = st.secrets.get("M365_GRAPH_TOKEN", "")
    if not token:
        return "unknown", ["Microsoft 365 requires Graph auth. Add M365_GRAPH_TOKEN in Streamlit Secrets."]

    headers = {"Authorization": f"Bearer {token}"}

    try:
        data = fetch_json(url, headers=headers)
    except Exception as e:
        return "unknown", [f"Fetch/parse error (Graph): {e}"]

    items = data.get("value", []) or []
    if not items:
        return "ok", []

    bad = [x for x in items if (x.get("status") or "").lower() != "serviceoperational"]
    if not bad:
        return "ok", []

    level = "degraded"
    details = []
    for x in bad[:3]:
        service = x.get("service", "Service")
        status = x.get("status", "unknown")
        details.append(f"{service} — {status}")

        if status.lower() in {"serviceinterruption", "serviceoutage"}:
            level = "major"

    return level, details

def summarize(provider):
    kind, url = provider["kind"], provider["url"]
    if kind == "statuspage":
        return summarize_statuspage(url)
    if kind == "rss":
        return summarize_rss(url)
    if kind == "gcp_incidents":
        return summarize_gcp_incidents(url)
    if kind == "m365_graph":
        return summarize_m365_graph(url)
    return "unknown", [f"Unsupported provider kind: {kind}"]

# -----------------------
# UI controls
# -----------------------
severity_order = {"major": 0, "degraded": 1, "unknown": 2, "ok": 3}
emoji = {"ok": "✅", "degraded": "🟡", "major": "🔴", "unknown": "⚪"}

left, mid, right = st.columns([2, 2, 3])
with left:
    show = st.multiselect(
        "Show severities",
        options=["major", "degraded", "unknown", "ok"],
        default=["major", "degraded", "unknown", "ok"],
    )
with mid:
    search = st.text_input("Search providers", value="", placeholder="e.g., AWS, Azure, GCP").strip().lower()
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
        else:
            st.info("Unknown")

        if r["details"]:
            with st.expander("Details", expanded=(r["level"] != "ok")):
                for d in r["details"]:
                    st.write("• " + d)

    st.divider()

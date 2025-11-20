import requests, feedparser
from html import unescape
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# --- Page setup ---
st.set_page_config(page_title="Outage Watch", layout="wide")
st.title("Outage Watch")
st.caption("Auto-refreshes every 60s; network responses cached for 60s.")
st_autorefresh(interval=60_000, key="auto_refresh")

# --- Providers to poll (add/remove freely) ---
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

# --- Helpers ---
@st.cache_data(ttl=60)
def fetch_bytes(url, timeout=10):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

def summarize_statuspage(url):
    try:
        data = requests.get(url, timeout=10).json()
    except Exception as e:
        return "unknown", [f"Fetch error: {e}"]

    indicator = (data.get("status", {}) or {}).get("indicator", "none")
    incidents = (data.get("incidents") or []) + (data.get("scheduled_maintenances") or [])

    major = indicator in {"major", "critical"} or any(i.get("impact") in {"major", "critical"} for i in incidents)
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
        data = requests.get(url, timeout=10).json()
    except Exception as e:
        return "unknown", [f"Fetch error: {e}"]

    status = data.get("status", "ok")
    incidents = data.get("active_incidents") or data.get("incidents") or []

    if status == "ok" and not incidents:
        return "ok", []

    # If there are incidents, assume degraded unless an item is explicitly marked outage
    level = "major" if any((inc.get("type") or "").lower() == "outage" for inc in incidents) else "degraded"
    details = []
    for inc in incidents[:3]:
        details.append(f"{inc.get('title','Incident')} — status: {inc.get('status')} — updated: {inc.get('date_updated')}")
    return level, details

def summarize_rss(url):
    try:
        content = fetch_bytes(url)
        feed = feedparser.parse(content)
    except Exception as e:
        return "unknown", [f"Fetch error: {e}"]

    entries = feed.entries or []
    if not entries:
        return "ok", []

    # Look at the latest item to decide "ok/degraded/major"
    latest = entries[0]
    title = unescape(getattr(latest, "title", "")).lower()
    published = getattr(latest, "published", "") or getattr(latest, "updated", "")

    # Best-effort heuristic: treat certain words as trouble signals
    bad_words = ["outage", "degraded", "investigating", "issue", "error", "latency",
                 "impact", "connectivity", "unavailable", "disruption"]
    level = "degraded" if any(w in title for w in bad_words) else "ok"
    if "major" in title or "outage" in title:
        level = "major"
    if "resolved" in title or "operating normally" in title:
        level = "ok"

    # Show a few recent items for context
    details = []
    for e in entries[:3]:
        t = unescape(getattr(e, "title", "Update"))
        ts = getattr(e, "published", "") or getattr(e, "updated", "")
        details.append(f"{t} — {ts}")

    return level, details

def summarize(provider):
    kind, url = provider["kind"], provider["url"]
    if kind == "statuspage":
        return summarize_statuspage(url)
    if kind == "slack":
        return summarize_slack(url)
    if kind == "rss":
        return summarize_rss(url)
    return "unknown", [f"Unsupported provider kind: {kind}"]

# --- Render cards ---
for p in PROVIDERS:
    level, details = summarize(p)
    emoji = {"ok": "✅", "degraded": "🟡", "major": "🔴", "unknown": "⚪"}.get(level, "⚪")
    st.subheader(f"{emoji} {p['name']}")
    if level == "ok":
        st.success("Operational")
    elif level == "degraded":
        st.warning("Degraded / incident or recent issue")
    elif level == "major":
        st.error("Major outage or incident")
    else:
        st.info("Unknown")
    if details:
        with st.expander("Details", expanded=(level != "ok")):
            for d in details:
                st.write("• " + d)

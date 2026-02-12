import re
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

DEFAULT_TIMEOUT = 12

# -----------------------
# Crowd signals (Option A allowlist)
# Free via RSSHub -> Outage.Report RSS
# Route: /outagereport/:name/:count?  (example: https://rsshub.app/outagereport/ubisoft/5)
# -----------------------
RSSHUB_INSTANCE = "https://rsshub.app"
RSSHUB_OUTAGEREPORT_TEMPLATE = RSSHUB_INSTANCE + "/outagereport/{slug}/{count}"

CROWD_ALLOWLIST = [
    # NOTE: Slugs must match outage.report naming style (lowercase, hyphen-separated).
    # If any slug is wrong, you’ll just see fewer/no crowd alerts — we can tune later.
    {"name": "American Express", "slug": "american-express", "threshold": 30},
    {"name": "Visa",            "slug": "visa",            "threshold": 30},
    {"name": "Mastercard",      "slug": "mastercard",      "threshold": 30},
    {"name": "PayPal",          "slug": "paypal",          "threshold": 25},
    {"name": "Stripe",          "slug": "stripe",          "threshold": 25},
    {"name": "Fiserv",          "slug": "fiserv",          "threshold": 20},
    {"name": "Worldpay",        "slug": "worldpay",        "threshold": 20},
    {"name": "Adyen",           "slug": "adyen",           "threshold": 20},
]

# -----------------------
# Official providers (free + official sources)
# -----------------------
PROVIDERS = [
    # Cloud providers
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
        "name": "Google Workspace",
        "kind": "gws_incidents_json",
        "url": "https://www.google.com/appsstatus/dashboard/incidents.json",
        "status_page": "https://www.google.com/appsstatus/dashboard/",
    },

    # Microsoft 365 (link-only for now)
    {
        "name": "Microsoft 365",
        "kind": "link_only",
        "url": "",
        "status_page": "https://status.cloud.microsoft",
        "note": "Public status page only (tenant service health API requires admin access).",
    },

    # Payments / PSPs
    {
        "name": "PayPal",
        "kind": "rss",
        "url": "https://www.paypal-status.com/feed/rss",
        "status_page": "https://www.paypal-status.com/product/production",
    },
    {
        "name": "Stripe",
        "kind": "stripe_json",
        "url": "https://status.stripe.com/current/full",
        "status_page": "https://status.stripe.com/",
    },
    {
        "name": "Adyen",
        "kind": "link_only",
        "url": "",
        "status_page": "https://status.adyen.com/",
        "note": "Official status page (no simple public JSON feed wired yet).",
    },

    # Worldpay
    {
        "name": "Worldpay (Access Worldpay)",
        "kind": "statuspage",
        "url": "https://status.access.worldpay.com/api/v2/summary.json",
        "status_page": "https://status.access.worldpay.com/",
    },
    {
        "name": "Worldpay (WPG)",
        "kind": "link_only",
        "url": "",
        "status_page": "https://status.wpg.worldpay.com/",
        "note": "Official WPG status page (link-only).",
    },

    # Schemes / scheme-adjacent
    {
        "name": "Visa Acceptance Solutions",
        "kind": "statuspage",
        "url": "https://status.visaacceptance.com/api/v2/summary.json",
        "status_page": "https://status.visaacceptance.com/",
    },
    {
        "name": "Mastercard Developers API Status",
        "kind": "link_only",
        "url": "",
        "status_page": "https://developer.mastercard.com/api-status",
        "note": "Developer API status (not a global network health indicator).",
    },
    {
        "name": "American Express Developers",
        "kind": "link_only",
        "url": "",
        "status_page": "https://developer.americanexpress.com/",
        "note": "Developer portal (no official public incident feed wired).",
    },
]

# -----------------------
# Networking (cached)
# -----------------------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    headers = {
        "User-Agent": "OutageWatch/1.0 (+streamlit)",
        "Accept": "*/*",
    }
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.content

@st.cache_data(ttl=60, show_spinner=False)
def fetch_json(url: str):
    raw = fetch_url(url)
    return requests.models.complexjson.loads(raw.decode("utf-8", errors="replace"))

# -----------------------
# Helpers: scoring + levels
# -----------------------
severity_order = {"major": 0, "degraded": 1, "unknown": 2, "info": 3, "ok": 4}
emoji = {"ok": "✅", "degraded": "🟡", "major": "🔴", "unknown": "⚪", "info": "🔵"}

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

# -----------------------
# Summarizers: Official sources
# -----------------------
def summarize_statuspage(url):
    try:
        data = fetch_json(url)
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

def summarize_google_workspace_incidents(url):
    """
    Uses Google Workspace dashboard incidents.json.
    We treat incidents with no 'end' as active.
    Status values can include SERVICE_OUTAGE (major) or SERVICE_DISRUPTION (degraded).
    """
    try:
        incidents = fetch_json(url)  # array
    except Exception as e:
        return "unknown", [f"Fetch/parse error: {e}"]

    if not incidents:
        return "ok", []

    active = [i for i in incidents if not i.get("end")]
    if not active:
        return "ok", []

    level = "degraded"
    details = []
    for inc in active[:3]:
        most = inc.get("most_recent_update") or {}
        status = (most.get("status") or "").upper()
        begin = inc.get("begin") or ""
        ext = (inc.get("external_desc") or "").strip().splitlines()[0] if inc.get("external_desc") else ""
        title = "Google Workspace incident"

        if status == "SERVICE_OUTAGE":
            level = "major"

        if ext:
            title = ext[:120]

        details.append(f"{title} — status: {status or 'n/a'} — began: {begin}")

    return level, details

def summarize_stripe_json(url):
    """
    Stripe current/full endpoint.
    Best-effort parsing to detect degraded/major signals.
    """
    try:
        data = fetch_json(url)
    except Exception as e:
        return "unknown", [f"Fetch/parse error: {e}"]

    details = []
    level = "ok"

    def norm(s):
        return (s or "").strip().lower()

    summary = None
    for key in ["status", "summary", "indicator"]:
        if key in data:
            summary = data.get(key)
            break

    components = data.get("components") or data.get("services") or []
    if isinstance(components, dict):
        components = components.get("components") or components.get("services") or []

    major_markers = {"major_outage", "partial_outage", "outage", "down", "critical"}
    degraded_markers = {"degraded", "degraded_performance", "partial", "warning", "investigating", "identified", "monitoring"}

    if isinstance(summary, str):
        s = norm(summary)
        if any(m in s for m in major_markers):
            level = "major"
        elif any(m in s for m in degraded_markers):
            level = "degraded"

    if isinstance(summary, dict):
        ind = norm(summary.get("indicator") or summary.get("status") or "")
        if ind in {"major", "critical"}:
            level = "major"
        elif ind in {"minor", "degraded"}:
            level = "degraded"

    for c in (components or [])[:20]:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("title")
        stt = c.get("status") or c.get("indicator") or c.get("state")
        if not name or not stt:
            continue
        s = norm(stt)
        if s in major_markers or "major" in s or "outage" in s or "down" in s:
            level = "major"
            details.append(f"{name} — {stt}")
        elif s in degraded_markers or "degraded" in s or "partial" in s:
            if level != "major":
                level = "degraded"
            details.append(f"{name} — {stt}")

    messages = data.get("incidents") or data.get("messages") or []
    if isinstance(messages, list):
        for m in messages[:3]:
            if isinstance(m, dict):
                title = m.get("title") or m.get("name") or m.get("message")
                if title:
                    details.append(str(title)[:160])

    if level == "ok":
        return "ok", []
    return level, details[:3] if details else ["See official Stripe status page for details."]

def summarize_link_only(provider):
    note = provider.get("note") or "See official status page."
    return "info", [note]

def summarize(provider):
    kind = provider["kind"]
    url = provider.get("url", "")

    if kind == "statuspage":
        return summarize_statuspage(url)
    if kind == "rss":
        return summarize_rss(url)
    if kind == "gcp_incidents":
        return summarize_gcp_incidents(url)
    if kind == "gws_incidents_json":
        return summarize_google_workspace_incidents(url)
    if kind == "stripe_json":
        return summarize_stripe_json(url)
    if kind == "link_only":
        return summarize_link_only(provider)

    return "unknown", [f"Unsupported provider kind: {kind}"]

# -----------------------
# Crowd signals logic
# -----------------------
def get_crowd_signals():
    triggered = []

    for s in CROWD_ALLOWLIST:
        feed_url = RSSHUB_OUTAGEREPORT_TEMPLATE.format(slug=s["slug"], count=10)

        try:
            content = fetch_url(feed_url)
            feed = feedparser.parse(content)
            entries = feed.entries or []
        except Exception:
            continue

        if not entries:
            continue

        max_reports = None
        best_title = None
        best_time = None

        for e in entries[:5]:
            title = unescape(getattr(e, "title", "Update"))
            t_lower = title.lower()

            m = re.search(r"(\d+)\s+reports?", t_lower) or re.search(r"reports?\s*[:\-]\s*(\d+)", t_lower)
            if m:
                n = int(m.group(1))
                if max_reports is None or n > max_reports:
                    max_reports = n
                    best_title = title
                    best_time = getattr(e, "published", "") or getattr(e, "updated", "")
            elif best_title is None:
                best_title = title
                best_time = getattr(e, "published", "") or getattr(e, "updated", "")

        if max_reports is not None and max_reports >= s["threshold"]:
            triggered.append({
                "name": s["name"],
                "reports": max_reports,
                "threshold": s["threshold"],
                "title": best_title or "Crowd activity",
                "time": best_time or "",
                "link": f"https://outage.report/{s['slug']}",
                "feed_url": feed_url,
            })

    triggered.sort(key=lambda x: x["reports"], reverse=True)
    return triggered

# -----------------------
# UI controls
# -----------------------
left, mid, right = st.columns([2, 2, 3])
with left:
    show = st.multiselect(
        "Show severities",
        options=["major", "degraded", "unknown", "info", "ok"],
        default=["major", "degraded", "unknown", "info", "ok"],
    )
with mid:
    search = st.text_input("Search providers", value="", placeholder="e.g., AWS, PayPal, Stripe").strip().lower()
with right:
    st.write("")
    st.caption("Click provider names to open official status pages.")

# -----------------------
# Crowd signals section (list monitored services + thresholds)
# -----------------------
st.subheader("Crowd signals")
monitoring = ", ".join([f"{s['name']} (≥{s['threshold']})" for s in CROWD_ALLOWLIST])
st.caption(f"Monitoring: {monitoring}")

crowd = get_crowd_signals()

if not crowd:
    st.caption("No crowd-report spikes detected for your allowlist.")
else:
    for c in crowd:
        st.error(f"🔴 {c['name']} — {c['reports']} reports (threshold: {c['threshold']})")
        cols = st.columns([3, 2])
        with cols[0]:
            st.write(f"• {c['title']}")
            if c["time"]:
                st.write(f"• {c['time']}")
        with cols[1]:
            st.link_button("Open crowd-signal source", c["link"])
            st.link_button("Open RSS feed", c["feed_url"])

st.divider()

# -----------------------
# Poll official providers in parallel
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
# Render official status cards
# -----------------------
st.subheader("Official status")
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

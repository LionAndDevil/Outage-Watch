import re
import requests
import feedparser
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

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
# Outage.Report -> RSSHub RSS
# Route: /outagereport/:slug/:count (slug can include country paths like us/verizon)
# -----------------------
# Public RSSHub instances (fallback order). Public instances can rate-limit or block.
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://hub.slarker.me",
    "https://rsshub.pseudoyu.com",
    "https://rsshub.rss.tips",
    "https://rsshub.ktachibana.party",
    "https://rsshub.woodland.cafe",
    "https://rss.owo.nz",
    "https://rss.wudifeixue.com",
    "https://yangzhi.app",
    "https://rss.littlebaby.lol",
    "https://rsshub.henry.wang",
    "https://rss.peachyjoy.top",
    "https://rsshub.speednet.icu",
    "https://hub.rss.direct",
    "https://rsshub.umzzz.com",
]

RSSHUB_OUTAGEREPORT_PATH_TEMPLATE = "/outagereport/{slug}/{count}"

def _telco_threshold(name: str) -> int:
    """
    Starter thresholds for telecoms. Increase to reduce noise.
    """
    n = name.lower()
    if any(x in n for x in ["verizon", "t-mobile", "at&t", "o2", "ee", "bt", "vodafone uk", "virgin media"]):
        return 35
    return 30

CROWD_ALLOWLIST = [
    # Payments
    {"name": "American Express", "slug": "american-express", "threshold": 30},
    {"name": "Visa",            "slug": "visa",             "threshold": 30},
    {"name": "Mastercard",      "slug": "mastercard",       "threshold": 30},
    {"name": "PayPal",          "slug": "paypal",           "threshold": 25},
    {"name": "Stripe",          "slug": "stripe",           "threshold": 25},
    {"name": "Fiserv",          "slug": "fiserv",           "threshold": 20},
    {"name": "Worldpay",        "slug": "worldpay",         "threshold": 20},
    {"name": "Adyen",           "slug": "adyen",            "threshold": 20},

    # Telecoms (US + UK country-path slugs where known)
    {"name": "Verizon",           "slug": "us/verizon",       "threshold": _telco_threshold("Verizon")},
    {"name": "T-Mobile US",       "slug": "us/t-mobile",      "threshold": _telco_threshold("T-Mobile US")},
    {"name": "AT&T",              "slug": "us/att",           "threshold": _telco_threshold("AT&T")},
    {"name": "Vodafone UK",       "slug": "gb/vodafone",      "threshold": _telco_threshold("Vodafone UK")},
    {"name": "BT (UK)",           "slug": "gb/bt",            "threshold": _telco_threshold("BT (UK)")},
    {"name": "EE (UK)",           "slug": "gb/ee",            "threshold": _telco_threshold("EE (UK)")},
    {"name": "Virgin Media (UK)", "slug": "gb/virgin-media",  "threshold": _telco_threshold("Virgin Media (UK)")},

    # Telecoms (trial slugs; validate via Crowd feed checks)
    {"name": "China Mobile",      "slug": "china-mobile",      "threshold": _telco_threshold("China Mobile")},
    {"name": "Bharti Airtel",     "slug": "bharti-airtel",     "threshold": _telco_threshold("Bharti Airtel")},
    {"name": "Reliance Jio",      "slug": "reliance-jio",      "threshold": _telco_threshold("Reliance Jio")},
    {"name": "China Telecom",     "slug": "china-telecom",     "threshold": _telco_threshold("China Telecom")},
    {"name": "China Unicom",      "slug": "china-unicom",      "threshold": _telco_threshold("China Unicom")},
    {"name": "América Móvil",     "slug": "america-movil",     "threshold": _telco_threshold("America Movil")},
    {"name": "Vodafone Group",    "slug": "vodafone",          "threshold": _telco_threshold("Vodafone")},
    {"name": "Orange",            "slug": "orange",            "threshold": _telco_threshold("Orange")},
    {"name": "Telefónica",        "slug": "telefonica",        "threshold": _telco_threshold("Telefonica")},
    {"name": "MTN Group",         "slug": "mtn",               "threshold": _telco_threshold("MTN")},
    {"name": "Deutsche Telekom",  "slug": "deutsche-telekom",  "threshold": _telco_threshold("Deutsche Telekom")},
    {"name": "Iliad Group",       "slug": "iliad",             "threshold": _telco_threshold("Iliad")},
    {"name": "TIM (Telecom Italia)", "slug": "tim",            "threshold": _telco_threshold("TIM")},
    {"name": "Swisscom",          "slug": "swisscom",          "threshold": _telco_threshold("Swisscom")},
    {"name": "Telia Company",     "slug": "telia",             "threshold": _telco_threshold("Telia")},
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

    # TRY: Adyen (attempt Statuspage-style endpoints, then fallback)
    {
        "name": "Adyen",
        "kind": "statuspage_try",
        "url": "https://status.adyen.com",
        "status_page": "https://status.adyen.com/",
        "note": "Attempts public Statuspage-style JSON; if blocked/JS-only, falls back to link-only.",
    },

    # End-user transaction-focused Worldpay view (WPG)
    {
        "name": "Worldpay Payments Gateway (WPG)",
        "kind": "statuspage_html",
        "url": "https://status.wpg.worldpay.com/",
        "status_page": "https://status.wpg.worldpay.com/",
        "note": "Parsed from the public WPG status page HTML.",
    },

    # Schemes / scheme-adjacent
    {
        "name": "Visa Acceptance Solutions",
        "kind": "statuspage",
        "url": "https://status.visaacceptance.com/api/v2/summary.json",
        "status_page": "https://status.visaacceptance.com/",
    },

    # TRY: Mastercard Developers API Status (HTML keyword parsing)
    {
        "name": "Mastercard Developers API Status",
        "kind": "mastercard_dev_html",
        "url": "https://developer.mastercard.com/api-status",
        "status_page": "https://developer.mastercard.com/api-status",
        "note": "Attempts to classify by parsing the public page text; may be JS-driven and not parseable.",
    },

    # Amex Developers (still link-only; no reliable public incident feed)
    {
        "name": "American Express Developers",
        "kind": "link_only",
        "url": "",
        "status_page": "https://developer.americanexpress.com/",
        "note": "No public status RSS/JSON endpoint found; link-only.",
    },
]

# -----------------------
# Networking (cached)
# -----------------------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_url_with_time(url: str, timeout: int = DEFAULT_TIMEOUT):
    """
    Returns (bytes, fetched_at_utc). fetched_at reflects when the cached value was created.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return r.content, fetched_at

@st.cache_data(ttl=60, show_spinner=False)
def fetch_json(url: str):
    raw, _ = fetch_url_with_time(url)
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

def summarize_statuspage_try(base_url: str):
    tried = []
    for endpoint in ["/api/v2/summary.json", "/api/v2/status.json"]:
        url = base_url.rstrip("/") + endpoint
        tried.append(endpoint)
        try:
            data = fetch_json(url)
        except Exception:
            continue

        status_obj = data.get("status") if isinstance(data, dict) else None
        if isinstance(status_obj, dict):
            indicator = (status_obj.get("indicator") or "none").lower()
            if indicator in {"major", "critical"}:
                return "major", [f"Status indicator: {indicator}"]
            if indicator in {"minor"}:
                return "degraded", [f"Status indicator: {indicator}"]
            return "ok", []

        return "info", ["Fetched JSON but format was unexpected; see official status page."]

    return "info", [f"No public JSON endpoints responded ({', '.join(tried)})."]

def summarize_rss(url):
    try:
        content, _ = fetch_url_with_time(url)
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
        incidents = fetch_json(url)  # array
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
        title = ext[:120] if ext else "Google Workspace incident"

        if status == "SERVICE_OUTAGE":
            level = "major"

        details.append(f"{title} — status: {status or 'n/a'} — began: {begin}")

    return level, details

def summarize_stripe_json(url):
    try:
        data = fetch_json(url)
    except Exception as e:
        return "unknown", [f"Fetch/parse error: {e}"]

    # Conservative parsing (avoid false alarms)
    indicator = None
    if isinstance(data, dict):
        status = data.get("status")
        if isinstance(status, dict):
            indicator = (status.get("indicator") or "").lower()
        elif isinstance(status, str):
            indicator = status.lower()

    if indicator in {"major", "critical"}:
        return "major", ["See official Stripe status page for details."]
    if indicator in {"minor", "degraded"}:
        return "degraded", ["See official Stripe status page for details."]
    return "ok", []

def summarize_statuspage_html(url):
    try:
        html, _ = fetch_url_with_time(url)
        html = html.decode("utf-8", errors="replace").lower()
    except Exception as e:
        return "unknown", [f"Fetch error: {e}"]

    top = html.split("past incidents", 1)[0]

    if "major outage" in top or "partial outage" in top:
        return "major", ["See official status page for details."]
    if any(k in top for k in ["degraded performance", "investigating", "identified", "monitoring"]):
        return "degraded", ["See official status page for details."]
    if "all systems operational" in top or "all services are operational" in top:
        return "ok", []
    return "unknown", ["See official status page for details."]

def summarize_mastercard_dev_html(url):
    try:
        html, _ = fetch_url_with_time(url)
        html = html.decode("utf-8", errors="replace")
    except Exception as e:
        return "unknown", [f"Fetch error: {e}"]

    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip().lower()

    if len(text) < 200:
        return "info", ["Status page is likely JS-driven; unable to extract status text reliably."]

    if "unreachable" in text or "not available" in text:
        return "major", ["One or more services reported as Unreachable/Not available (page text)."]
    if "partially degraded" in text or "degraded" in text:
        return "degraded", ["One or more services reported as Partially Degraded (page text)."]
    if "healthy" in text:
        return "ok", []

    return "info", ["Unable to classify from page text; see official status page."]

def summarize_link_only(provider):
    return "info", [provider.get("note") or "See official status page."]

def summarize(provider):
    kind = provider["kind"]
    url = provider.get("url", "")

    if kind == "statuspage":
        return summarize_statuspage(url)
    if kind == "statuspage_try":
        return summarize_statuspage_try(url)
    if kind == "rss":
        return summarize_rss(url)
    if kind == "gcp_incidents":
        return summarize_gcp_incidents(url)
    if kind == "gws_incidents_json":
        return summarize_google_workspace_incidents(url)
    if kind == "stripe_json":
        return summarize_stripe_json(url)
    if kind == "statuspage_html":
        return summarize_statuspage_html(url)
    if kind == "mastercard_dev_html":
        return summarize_mastercard_dev_html(url)
    if kind == "link_only":
        return summarize_link_only(provider)

    return "unknown", [f"Unsupported provider kind: {kind}"]

# -----------------------
# Crowd signals logic (try next instance on 403/429/5xx)
# -----------------------
def build_outagereport_feed_url(instance: str, slug: str, count: int) -> str:
    return instance.rstrip("/") + RSSHUB_OUTAGEREPORT_PATH_TEMPLATE.format(slug=slug.strip("/"), count=count)

def fetch_crowd_feed_with_fallback(slug: str, count: int = 10):
    last_err = None
    for inst in RSSHUB_INSTANCES:
        url = build_outagereport_feed_url(inst, slug, count)
        try:
            content, fetched_at = fetch_url_with_time(url)
            feed = feedparser.parse(content)
            entries = feed.entries or []
            return url, entries, fetched_at, inst, None
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in {403, 429, 500, 502, 503, 504}:
                last_err = e
                continue
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue
    return None, [], None, None, last_err

def get_crowd_signals():
    triggered = []
    checks = []

    for s in CROWD_ALLOWLIST:
        feed_url, entries, fetched_at, inst_used, err = fetch_crowd_feed_with_fallback(s["slug"], count=10)

        checks.append({
            "name": s["name"],
            "slug": s["slug"],
            "threshold": s["threshold"],
            "feed_url": feed_url,
            "fetched_at": fetched_at,
            "instance": inst_used,
            "ok": err is None,
            "error": str(err) if err else "",
        })

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
                "source_link": f"https://outage.report/{s['slug'].strip('/')}",
                "feed_url": feed_url or "",
                "fetched_at": fetched_at or "",
                "instance": inst_used or "",
            })

    triggered.sort(key=lambda x: x["reports"], reverse=True)
    return triggered, checks

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
    search = st.text_input("Search providers", value="", placeholder="e.g., AWS, PayPal, Verizon").strip().lower()
with right:
    st.write("")
    st.caption("Click provider names to open official status pages.")

# -----------------------
# Crowd signals section
# -----------------------
st.subheader("Crowd signals")
monitoring = ", ".join([f"{s['name']} (≥{s['threshold']})" for s in CROWD_ALLOWLIST])
st.caption(f"Monitoring: {monitoring}")

crowd, crowd_checks = get_crowd_signals()

with st.expander("Crowd feed checks (sources & last fetched)", expanded=False):
    st.caption("Each service is pulled from Outage.Report via RSSHub; the app tries multiple public instances.")
    for chk in crowd_checks:
        status_icon = "✅" if chk["ok"] else "⚠️"
        line = f"{status_icon} {chk['name']} — threshold ≥{chk['threshold']}"
        if chk["fetched_at"]:
            line += f" — last fetched: {chk['fetched_at']}"
        if chk["instance"]:
            line += f" — via: {chk['instance']}"
        st.write(line)
        if chk["feed_url"]:
            st.link_button("Open RSS feed", chk["feed_url"], key=f"feed_{chk['slug']}")
        if chk["error"]:
            st.caption(f"Error: {chk['error']}")

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
            if c["fetched_at"]:
                st.write(f"• Last fetched: {c['fetched_at']} (via {c['instance']})")
        with cols[1]:
            st.link_button("Open crowd-signal source", c["source_link"], key=f"src_{c['name']}")
            if c["feed_url"]:
                st.link_button("Open RSS feed", c["feed_url"], key=f"rss_{c['name']}")

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

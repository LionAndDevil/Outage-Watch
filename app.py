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
    {"name": "American Express", "slug": "american-express", "threshold": 30, "link": "https://outage.report"},
    {"name": "Visa",            "slug": "visa",            "threshold": 30, "link": "https://outage.report"},
    {"name": "Mastercard",      "slug": "mastercard",      "threshold": 30, "link": "https://outage.report"},
    {"name": "PayPal",          "slug": "paypal",          "threshold": 25, "link": "https://outage.report"},
    {"name": "Stripe",          "slug": "stripe",          "threshold": 25, "link": "https://outage.report"},
    {"name": "Fiserv",          "slug": "fiserv",          "threshold": 20, "link": "https://outage.report"},
    {"name": "Worldpay",        "slug": "worldpay",        "threshold": 20, "link": "https://outage.report"},
    {"name": "Adyen",           "slug": "adyen",           "threshold": 20, "link": "https://outage.report"},
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
        "kind": "paypal_api",
        "url": "https://www.paypal-status.com/api/production",
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

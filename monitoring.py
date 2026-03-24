import re
import json
import time
import requests
import feedparser
from html import unescape
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 10
CROWD_TIMEOUT = 6  # shorter timeout for crowd RSS
MAX_WORKERS = 8  # parallel crowd fetch workers

# -----------------------
# Crowd signals (Option A) - On demand checks (two groups)
# -----------------------
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
MAX_RSSHUB_ATTEMPTS = 3  # cap fallback attempts for performance
RSSHUB_OUTAGEREPORT_PATH_TEMPLATE = "/outagereport/{slug}/{count}"


def _telco_threshold(name: str) -> int:
    n = name.lower()
    if any(x in n for x in ["verizon", "t-mobile", "at&t", "o2", "ee", "bt", "vodafone uk", "virgin media"]):
        return 35
    return 30


# Tag each crowd item with a group: "payments" or "telecoms"
CROWD_ALLOWLIST = [
    # Payments / banks / card schemes / PSPs
    {"group": "payments", "name": "American Express", "slug": "american-express", "threshold": 30},
    {"group": "payments", "name": "Visa",            "slug": "visa",             "threshold": 30},
    {"group": "payments", "name": "Mastercard",      "slug": "mastercard",       "threshold": 30},
    {"group": "payments", "name": "PayPal",          "slug": "paypal",           "threshold": 25},
    {"group": "payments", "name": "Stripe",          "slug": "stripe",           "threshold": 25},
    {"group": "payments", "name": "Fiserv",          "slug": "fiserv",           "threshold": 20},
    {"group": "payments", "name": "Worldpay",        "slug": "worldpay",         "threshold": 20},
    {"group": "payments", "name": "Adyen",           "slug": "adyen",            "threshold": 20},

    # Telecoms (US + UK)
    {"group": "telecoms", "name": "Verizon",           "slug": "us/verizon",       "threshold": _telco_threshold("Verizon")},
    {"group": "telecoms", "name": "T-Mobile US",       "slug": "us/t-mobile",      "threshold": _telco_threshold("T-Mobile US")},
    {"group": "telecoms", "name": "AT&T",              "slug": "us/att",           "threshold": _telco_threshold("AT&T")},
    {"group": "telecoms", "name": "Vodafone UK",       "slug": "gb/vodafone",      "threshold": _telco_threshold("Vodafone UK")},
    {"group": "telecoms", "name": "BT (UK)",           "slug": "gb/bt",            "threshold": _telco_threshold("BT (UK)")},
    {"group": "telecoms", "name": "EE (UK)",           "slug": "gb/ee",            "threshold": _telco_threshold("EE (UK)")},
    {"group": "telecoms", "name": "Virgin Media (UK)", "slug": "gb/virgin-media",  "threshold": _telco_threshold("Virgin Media (UK)")},

    # Telecoms (trial slugs; validate)
    {"group": "telecoms", "name": "China Mobile",         "slug": "china-mobile",      "threshold": _telco_threshold("China Mobile")},
    {"group": "telecoms", "name": "Bharti Airtel",        "slug": "bharti-airtel",     "threshold": _telco_threshold("Bharti Airtel")},
    {"group": "telecoms", "name": "Reliance Jio",         "slug": "reliance-jio",      "threshold": _telco_threshold("Reliance Jio")},
    {"group": "telecoms", "name": "China Telecom",        "slug": "china-telecom",     "threshold": _telco_threshold("China Telecom")},
    {"group": "telecoms", "name": "China Unicom",         "slug": "china-unicom",      "threshold": _telco_threshold("China Unicom")},
    {"group": "telecoms", "name": "América Móvil",        "slug": "america-movil",     "threshold": _telco_threshold("America Movil")},
    {"group": "telecoms", "name": "Vodafone Group",       "slug": "vodafone",          "threshold": _telco_threshold("Vodafone")},
    {"group": "telecoms", "name": "Orange",               "slug": "orange",            "threshold": _telco_threshold("Orange")},
    {"group": "telecoms", "name": "Telefónica",           "slug": "telefonica",        "threshold": _telco_threshold("Telefonica")},
    {"group": "telecoms", "name": "MTN Group",            "slug": "mtn",               "threshold": _telco_threshold("MTN")},
    {"group": "telecoms", "name": "Deutsche Telekom",     "slug": "deutsche-telekom",  "threshold": _telco_threshold("Deutsche Telekom")},
    {"group": "telecoms", "name": "Iliad Group",          "slug": "iliad",             "threshold": _telco_threshold("Iliad")},
    {"group": "telecoms", "name": "TIM (Telecom Italia)", "slug": "tim",               "threshold": _telco_threshold("TIM")},
    {"group": "telecoms", "name": "Swisscom",             "slug": "swisscom",          "threshold": _telco_threshold("Swisscom")},
    {"group": "telecoms", "name": "Telia Company",        "slug": "telia",             "threshold": _telco_threshold("Telia")},
]

# -----------------------
# Official providers
# -----------------------
PROVIDERS = [
    {"name": "AWS", "kind": "rss", "url": "https://status.aws.amazon.com/rss/all.rss",
     "status_page": "https://health.aws.amazon.com/health/status"},
    {"name": "Cloudflare", "kind": "statuspage", "url": "https://www.cloudflarestatus.com/api/v2/summary.json",
     "status_page": "https://www.cloudflarestatus.com/"},
    {"name": "Azure", "kind": "rss", "url": "https://azurestatuscdn.azureedge.net/en-us/status/feed/",
     "status_page": "https://azure.status.microsoft"},
    {"name": "Google Cloud (GCP)", "kind": "gcp_incidents", "url": "https://status.cloud.google.com/incidents.json",
     "status_page": "https://status.cloud.google.com"},
    {"name": "Google Workspace", "kind": "gws_incidents_json", "url": "https://www.google.com/appsstatus/dashboard/incidents.json",
     "status_page": "https://www.google.com/appsstatus/dashboard/"},
    {"name": "Microsoft 365", "kind": "link_only", "url": "", "status_page": "https://status.cloud.microsoft",
     "note": "Public status page only (tenant service health API requires admin access)."},
    {"name": "PayPal", "kind": "rss", "url": "https://www.paypal-status.com/feed/rss",
     "status_page": "https://www.paypal-status.com/product/production"},
    {"name": "Stripe", "kind": "stripe_json", "url": "https://status.stripe.com/current/full",
     "status_page": "https://status.stripe.com/"},
    {"name": "Adyen", "kind": "statuspage_try", "url": "https://status.adyen.com",
     "status_page": "https://status.adyen.com/",
     "note": "Attempts public Statuspage-style JSON; if blocked/JS-only, falls back to link-only."},
    {"name": "Worldpay Payments Gateway (WPG)", "kind": "statuspage_html", "url": "https://status.wpg.worldpay.com/",
     "status_page": "https://status.wpg.worldpay.com/",
     "note": "Parsed from the public WPG status page HTML."},
    {"name": "Visa Acceptance Solutions", "kind": "statuspage", "url": "https://status.visaacceptance.com/api/v2/summary.json",
     "status_page": "https://status.visaacceptance.com/"},
    {"name": "Mastercard Developers API Status", "kind": "mastercard_dev_html", "url": "https://developer.mastercard.com/api-status",
     "status_page": "https://developer.mastercard.com/api-status",
     "note": "Attempts to classify by parsing the public page text; may be JS-driven and not parseable."},
    {"name": "American Express Developers", "kind": "link_only", "url": "", "status_page": "https://developer.americanexpress.com/",
     "note": "No public status RSS/JSON endpoint found; link-only."},
]

def fetch_url_with_time(url: str, timeout: int = DEFAULT_TIMEOUT):
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

def fetch_json(url: str):
    raw, _ = fetch_url_with_time(url)
    return requests.models.complexjson.loads(raw.decode("utf-8", errors="replace"))

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
# Official summarizers
# -----------------------
def summarize_statuspage(url):
    try:
        data = fetch_json(url)
    except Exception as e:
        return "unknown", [f"Fetch error: {e}"]

    indicator = (data.get("status", {}) or {}).get("indicator", "none")

    incidents = data.get("incidents") or []
    maint = data.get("scheduled_maintenances") or []

    # Only incidents affect severity (ignore maintenance)
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
        incidents = fetch_json(url)
    except Exception as e:
        return "unknown", [f"Fetch/parse error: {e}"]

    if not incidents:
        return "ok", []

    active = [inc for inc in incidents if not (inc.get("end") or inc.get("resolved"))]
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
        incidents = fetch_json(url)
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

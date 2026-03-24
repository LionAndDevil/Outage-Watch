import re
import json
import time
import requestsrequests.get
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
    try:
        r = requests.get(url, timeout=3, headers=headers)
        r.raise_for_status()
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return r.content, fetched_at, True, None
    except Exception as e:
        return None, None, False, str(e)

def fetch_json(url: str):
    raw, fetched_at, ok, error = fetch_url_with_time(url)

    if not ok or raw is None:
        return None, fetched_at, ok, error

    try:
        data = requests.models.complexjson.loads(
            raw.decode("utf-8", errors="replace")
        )
        return data, fetched_at, True, None
    except Exception as e:
        return None, fetched_at, False, str(e)

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

# -----------------------
# Crowd signals helpers (on demand)
# -----------------------
def build_outagereport_feed_url(instance: str, slug: str, count: int) -> str:
    return instance.rstrip("/") + RSSHUB_OUTAGEREPORT_PATH_TEMPLATE.format(slug=slug.strip("/"), count=count)


def fetch_crowd_feed_with_fallback(slug: str, count: int = 10):
    last_err = None
    for inst in RSSHUB_INSTANCES[:MAX_RSSHUB_ATTEMPTS]:
        url = build_outagereport_feed_url(inst, slug, count)
        try:
            content, fetched_at = fetch_url_with_time(url, timeout=CROWD_TIMEOUT)
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


def run_crowd_signals_for_group(group_name: str):
    import time, traceback

    t0 = time.time()

    group_items = [s for s in CROWD_ALLOWLIST if s.get("group") == group_name]
    triggered = []
    checks = []

    internal_diag = {
        "group_name": group_name,
        "group_items_len": len(group_items),
        "entered_loop": False,
        "checks_len_end": 0,
        "checkpoint_before_loop": True,
        "checkpoint_after_loop": False,
        "elapsed_ms": None,
        "crash_error": "",
    }

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_map = {
                executor.submit(fetch_crowd_feed_with_fallback, s["slug"], 10): s
                for s in group_items
            }

            for future in as_completed(future_map):
                s = future_map[future]
                svc_t0 = time.time()

                check = {
                    "name": s.get("name", ""),
                    "slug": s.get("slug", ""),
                    "threshold": s.get("threshold", None),
                    "feed_url": "",
                    "fetched_at": "",
                    "instance": "",
                    "ok": False,
                    "error": "",
                    "error_type": "",
                    "elapsed_ms": None,
                }

                try:
                    feed_url, entries, fetched_at, inst_used, err = future.result()

                    check["feed_url"] = feed_url if isinstance(feed_url, str) else ""
                    check["fetched_at"] = fetched_at or ""
                    check["instance"] = inst_used or ""
                    check["ok"] = err is None
                    check["error"] = str(err) if err else ""
                    check["error_type"] = type(err).__name__ if err else ""

                    if entries:
                        max_reports = None
                        best_title = None
                        best_time = None

                        for e in entries[:5]:
                            raw_title = getattr(e, "title", None)
                            title = unescape(raw_title) if isinstance(raw_title, str) else "Update"
                            t_lower = title.lower()

                            m = (
                                re.search(r"(\d+)\s+reports?", t_lower)
                                or re.search(r"reports?\s*[:\-]\s*(\d+)", t_lower)
                            )

                            if m:
                                try:
                                    n = int(m.group(1))
                                except Exception:
                                    continue

                                if max_reports is None or n > max_reports:
                                    max_reports = n
                                    best_title = title
                                    best_time = getattr(e, "published", "") or getattr(e, "updated", "")
                            elif best_title is None:
                                best_title = title
                                best_time = getattr(e, "published", "") or getattr(e, "updated", "")

                        threshold = check["threshold"]
                        if max_reports is not None and threshold is not None and max_reports >= threshold:
                            triggered.append({
                                "name": check["name"],
                                "reports": max_reports,
                                "threshold": threshold,
                                "title": best_title or "Crowd activity",
                                "time": best_time or "",
                                "source_link": f"https://outage.report/{check['slug'].strip('/')}",
                                "feed_url": check["feed_url"],
                                "fetched_at": check["fetched_at"],
                                "instance": check["instance"],
                            })

                except Exception as e:
                    check["ok"] = False
                    check["error_type"] = type(e).__name__
                    check["error"] = str(e)[:300]

                finally:
                    check["elapsed_ms"] = int((time.time() - svc_t0) * 1000)
                    checks.append(check)   

        triggered.sort(key=lambda x: x.get("reports", 0), reverse=True)

        internal_diag["checkpoint_after_loop"] = True
        internal_diag["checks_len_end"] = len(checks)
        internal_diag["elapsed_ms"] = int((time.time() - t0) * 1000)

    except Exception:
        internal_diag["crash_error"] = traceback.format_exc()[-4000:]
        internal_diag["checks_len_end"] = len(checks)
        internal_diag["elapsed_ms"] = int((time.time() - t0) * 1000)

    return triggered, checks, internal_diag


def _now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def get_official_results():
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

            results.append({
                "name": p["name"],
                "kind": p["kind"],
                "status_page": p.get("status_page", ""),
                "level": level,
                "details": details,
                "source_type": "official",
            })

    return sorted(results, key=lambda r: r["name"].lower())


def get_crowd_results(group_name: str):
    triggered, checks, diag = run_crowd_signals_for_group(group_name)
    return {
        "group": group_name,
        "triggered": triggered,
        "checks": checks,
        "diag": diag,
        "source_type": "crowd",
    }

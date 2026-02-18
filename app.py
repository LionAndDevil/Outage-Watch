import re
import requests
import feedparser
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import streamlit as st

# -----------------------
# Page setup
# -----------------------
st.set_page_config(page_title="Outage Watch", layout="wide")
st.title("Outage Watch")

# Build marker (helps confirm Streamlit is running the latest commit)
st.caption("BUILD: 2026-02-17 internal-diag-v4-run-exception")

DEFAULT_TIMEOUT = 10

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

# -----------------------
# Networking (cached)
# -----------------------
@st.cache_data(ttl=60, show_spinner=False)
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

@st.cache_data(ttl=60, show_spinner=False)
def fetch_json(url: str):
    raw, _ = fetch_url_with_time(url)
    return requests.models.complexjson.loads(raw.decode("utf-8", errors="replace"))

# -----------------------
# Helpers
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
# Official summarizers
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
        for s in group_items:
            internal_diag["entered_loop"] = True
            svc_t0 = time.time()

            # Pre-fill a check record so even early failures produce an entry
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
                feed_url, entries, fetched_at, inst_used, err = fetch_crowd_feed_with_fallback(
                    s["slug"], count=10
                )

                check.update({
                    "feed_url": feed_url or "",
                    "fetched_at": fetched_at or "",
                    "instance": inst_used or "",
                    "ok": err is None,
                    "error": str(err) if err else "",
                    "error_type": type(err).__name__ if err else "",
                })

                # If fetch returned no entries (or errored), we still keep the check entry
                if not entries:
                    continue

                max_reports = None
                best_title = None
                best_time = None

                for e in entries[:5]:
                    # Be defensive about entry structure and title value
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
                            # If parsing fails, ignore this entry and continue
                            continue

                        if max_reports is None or n > max_reports:
                            max_reports = n
                            best_title = title
                            best_time = getattr(e, "published", "") or getattr(e, "updated", "")
                    elif best_title is None:
                        best_title = title
                        best_time = getattr(e, "published", "") or getattr(e, "updated", "")

                if max_reports is not None and check["threshold"] is not None and max_reports >= check["threshold"]:
                    triggered.append({
                        "name": check["name"],
                        "reports": max_reports,
                        "threshold": check["threshold"],
                        "title": best_title or "Crowd activity",
                        "time": best_time or "",
                        "source_link": f"https://outage.report/{check['slug'].strip('/')}",
                        "feed_url": check["feed_url"],
                        "fetched_at": check["fetched_at"],
                        "instance": check["instance"],
                    })

            except Exception as e:
                # Per-service hard failure: still record a check entry
                check["ok"] = False
                check["error_type"] = type(e).__name__
                check["error"] = str(e)[:300]
                # Optional: keep a small tail of traceback for debugging
                internal_diag["last_service_trace"] = traceback.format_exc()[-2000:]
            finally:
                check["elapsed_ms"] = int((time.time() - svc_t0) * 1000)
                checks.append(check)

        triggered.sort(key=lambda x: x.get("reports", 0), reverse=True)

        internal_diag["checkpoint_after_loop"] = True
        internal_diag["checks_len_end"] = len(checks)
        internal_diag["elapsed_ms"] = int((time.time() - t0) * 1000)

    except Exception:
        # Truly unexpected failure outside per-service handling
        internal_diag["crash_error"] = traceback.format_exc()[-4000:]
        internal_diag["checks_len_end"] = len(checks)
        internal_diag["elapsed_ms"] = int((time.time() - t0) * 1000)

    return triggered, checks, internal_diag

def _now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# -----------------------
# Session state
# -----------------------
if "crowd_payments" not in st.session_state:
    st.session_state["crowd_payments"] = {"ran": False, "ran_at": "", "triggered": [], "checks": [], "error": "", "diag": {}}
if "crowd_telecoms" not in st.session_state:
    st.session_state["crowd_telecoms"] = {"ran": False, "ran_at": "", "triggered": [], "checks": [], "error": "", "diag": {}}

# -----------------------
# Auto-refresh behavior
# -----------------------
disable_autorefresh = bool(st.session_state["crowd_payments"].get("ran")) or bool(st.session_state["crowd_telecoms"].get("ran"))

if disable_autorefresh:
    st.caption("Auto-refresh is paused after running Crowd checks (to keep results visible).")
    if st.button("Resume auto-refresh (60s)"):
        st.session_state["crowd_payments"]["ran"] = False
        st.session_state["crowd_telecoms"]["ran"] = False
        st.rerun()
else:
    st.caption("Auto-refreshes every 60s; network responses cached for 60s.")

# -----------------------
# SAFE runner (indentation-safe + checkpoints + run_exception + exception surfaced into JSON)
# -----------------------
def safe_run_group(state_key: str, group_name: str):
    import traceback

    # Ensure the state object exists and has the expected shape
    st.session_state.setdefault(state_key, {})
    st.session_state[state_key].setdefault("ran", False)
    st.session_state[state_key].setdefault("ran_at", "")
    st.session_state[state_key].setdefault("error", "")
    st.session_state[state_key].setdefault("triggered", [])
    st.session_state[state_key].setdefault("checks", [])
    st.session_state[state_key].setdefault("diag", {})

    # Initialize run state immediately (never rely on prior values)
    st.session_state[state_key]["ran"] = True
    st.session_state[state_key]["ran_at"] = _now_utc_str()
    st.session_state[state_key]["error"] = ""
    st.session_state[state_key]["triggered"] = []
    st.session_state[state_key]["checks"] = []
    st.session_state[state_key]["diag"] = {}

    # Always set before/after checkpoints in this wrapper
    st.session_state[state_key]["diag"]["checkpoint_before_run"] = True
    st.session_state[state_key]["diag"]["checkpoint_after_run"] = False

    try:
        seen_groups = sorted(set([str(s.get("group")) for s in CROWD_ALLOWLIST if isinstance(s, dict)]))
        group_items = [s for s in CROWD_ALLOWLIST if isinstance(s, dict) and s.get("group") == group_name]

        st.session_state[state_key]["diag"].update({
            "group_name_requested": group_name,
            "allowlist_len": len(CROWD_ALLOWLIST),
            "unique_groups_seen": seen_groups,
            "items_in_group": len(group_items),
            "sample_items": [
                {"name": s.get("name"), "group": s.get("group"), "slug": s.get("slug")}
                for s in group_items[:3]
            ],
        })

        if len(group_items) == 0:
            st.session_state[state_key]["error"] = (
                f"No items found for group='{group_name}'. "
                f"Groups seen: {seen_groups}"
            )
            return

        try:
            trig, chk, internal = run_crowd_signals_for_group(group_name)

            # Persist results no matter what they contain
            st.session_state[state_key]["triggered"] = trig or []
            st.session_state[state_key]["checks"] = chk or []
            st.session_state[state_key]["diag"]["internal"] = internal or {}

        except Exception as e:
            st.session_state[state_key]["error"] = str(e)
            st.session_state[state_key]["diag"]["run_exception"] = str(e)
            st.session_state[state_key]["diag"]["run_trace"] = traceback.format_exc()[-4000:]
            st.session_state[state_key]["diag"]["internal"] = {
                "exception": str(e),
                "where": "run_crowd_signals_for_group",
                "note": "Exception raised during crowd group run."
            }

    except Exception as e:
        st.session_state[state_key]["error"] = str(e)
        st.session_state[state_key]["diag"]["safe_trace"] = traceback.format_exc()[-4000:]
        st.session_state[state_key]["diag"]["internal"] = {
            "exception": str(e),
            "where": "safe_run_group",
            "note": "Exception raised in safe_run_group wrapper."
        }

    finally:
        st.session_state[state_key].setdefault("diag", {})
        st.session_state[state_key]["diag"]["checkpoint_after_run"] = True
        st.session_state[state_key]["diag"]["run_completed"] = True
        st.session_state[state_key]["diag"]["run_failed"] = bool(st.session_state[state_key].get("error"))


        if not st.session_state[state_key].get("checks"):
            err = st.session_state[state_key].get("error", "")
            st.session_state[state_key]["checks"] = [{
                "name": "(runner)",
                "slug": "",
                "threshold": "",
                "feed_url": "",
                "fetched_at": "",
                "instance": "",
                "ok": False,
                "error": err or "No checks were recorded.",
                "error_type": "RunnerError" if err else "NoChecks",
            }]
            st.session_state[state_key]["diag"]["checks_empty_reason"] = (
                "No checks were persisted to session_state; inserted fallback runner check."
            )
    
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
# Crowd signals section (on-demand)
# -----------------------
st.subheader("Crowd signals (on-demand)")

payments_items = [s for s in CROWD_ALLOWLIST if s["group"] == "payments"]
telecom_items  = [s for s in CROWD_ALLOWLIST if s["group"] == "telecoms"]

with st.expander("What is monitored (by group)", expanded=False):
    st.markdown("**Payments & Banks**")
    st.caption(", ".join([f"{s['name']} (≥{s['threshold']})" for s in payments_items]) or "(none)")
    st.markdown("**Telecoms**")
    st.caption(", ".join([f"{s['name']} (≥{s['threshold']})" for s in telecom_items]) or "(none)")

b1, b2, b3 = st.columns([2, 2, 6])
with b1:
    run_payments = st.button("Run crowd check: Payments & Banks", use_container_width=True, key="btn_payments")
with b2:
    run_telecoms = st.button("Run crowd check: Telecoms", use_container_width=True, key="btn_telecoms")
with b3:
    st.caption("Crowd checks do not run automatically; use the buttons to fetch crowd signals.")

if run_payments:
    with st.spinner("Running crowd check (Payments & Banks)…"):
        safe_run_group("crowd_payments", "payments")

if run_telecoms:
    with st.spinner("Running crowd check (Telecoms)…"):
        safe_run_group("crowd_telecoms", "telecoms")

# Render results (Payments)
st.markdown("### Crowd results: Payments & Banks")
cp = st.session_state["crowd_payments"]
if not cp["ran"]:
    st.info("Not run yet. Click **Run crowd check: Payments & Banks**.")
else:
    st.caption(f"Last run: {cp['ran_at']}")
    if cp.get("diag"):
        st.json(cp["diag"])
    if cp["error"]:
        st.error(f"Payments crowd check error: {cp['error']}")
    if not cp["triggered"]:
        st.success("No crowd-report spikes detected (Payments & Banks).")
    else:
        for c in cp["triggered"]:
            st.error(f"🔴 {c['name']} — {c['reports']} reports (threshold: {c['threshold']})")
            cols = st.columns([3, 2])
            with cols[0]:
                st.write(f"• {c['title']}")
                if c["time"]:
                    st.write(f"• {c['time']}")
                if c["fetched_at"]:
                    st.write(f"• Last fetched: {c['fetched_at']} (via {c['instance']})")
            with cols[1]:
                st.link_button("Open crowd-signal source", c["source_link"], key=f"pay_src_{c['name']}")
                if c["feed_url"]:
                    st.link_button("Open RSS feed", c["feed_url"], key=f"pay_rss_{c['name']}")

    with st.expander("Payments crowd feed checks (sources & last fetched)", expanded=False):
        if not cp["checks"]:
            st.info("No checks recorded (unexpected).")
        else:
            for chk in cp["checks"]:
                status_icon = "✅" if chk["ok"] else "⚠️"
                st.write(f"{status_icon} {chk['name']} — threshold ≥{chk['threshold']}")
feed_url = chk.get("feed_url")
slug = str(chk.get("slug", ""))

# Only render a link if it's a clean http(s) URL string
if isinstance(feed_url, str):
    feed_url = feed_url.strip()
else:
    feed_url = ""

if feed_url.startswith("http://") or feed_url.startswith("https://"):
    st.link_button("Open RSS feed", feed_url, key=f"pay_feed_{slug}")
if chk.get("error"):
                    st.caption(f"Error: {chk.get('error')}")

st.divider()

# Render results (Telecoms)
st.markdown("### Crowd results: Telecoms")
ct = st.session_state["crowd_telecoms"]
if not ct["ran"]:
    st.info("Not run yet. Click **Run crowd check: Telecoms**.")
else:
    st.caption(f"Last run: {ct['ran_at']}")
    if ct.get("diag"):
        st.json(ct["diag"])
    if ct["error"]:
        st.error(f"Telecoms crowd check error: {ct['error']}")
    if not ct["triggered"]:
        st.success("No crowd-report spikes detected (Telecoms).")
    else:
        for c in ct["triggered"]:
            st.error(f"🔴 {c['name']} — {c['reports']} reports (threshold: {c['threshold']})")
            cols = st.columns([3, 2])
            with cols[0]:
                st.write(f"• {c['title']}")
                if c["time"]:
                    st.write(f"• {c['time']}")
                if c["fetched_at"]:
                    st.write(f"• Last fetched: {c['fetched_at']} (via {c['instance']})")
            with cols[1]:
                st.link_button("Open crowd-signal source", c["source_link"], key=f"tel_src_{c['name']}")
                if c["feed_url"]:
                    st.link_button("Open RSS feed", c["feed_url"], key=f"tel_rss_{c['name']}")

    with st.expander("Telecoms crowd feed checks (sources & last fetched)", expanded=False):
        if not ct["checks"]:
            st.info("No checks recorded (unexpected).")
        else:
            for chk in ct["checks"]:
                status_icon = "✅" if chk["ok"] else "⚠️"
                st.write(f"{status_icon} {chk['name']} — threshold ≥{chk['threshold']}")
                if chk["feed_url"]:
                    st.link_button("Open RSS feed", chk["feed_url"], key=f"tel_feed_{chk['slug']}")
                if chk.get("error"):
                    st.caption(f"Error: {chk.get('error')}")

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

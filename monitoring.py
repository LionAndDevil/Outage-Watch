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

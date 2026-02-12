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
    {"name": "PayPal",

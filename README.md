# Outage Watch (Streamlit Starter)

A tiny Streamlit app that polls a few vendor status pages (JSON/RSS) and shows green/yellow/red for outages.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy
1. Push this folder to a GitHub repo.
2. In Streamlit Community Cloud, connect your GitHub, select the repo/branch, and set `app.py` as the main file.
3. Click Deploy. Each `git push` will auto-redeploy.

## Customize providers
Edit the `PROVIDERS` list in `app.py` — add more entries of kind `statuspage`, `rss`, or `slack`.

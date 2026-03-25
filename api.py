from fastapi import FastAPI
from monitoring import get_official_results, get_crowd_results

app = FastAPI(
    title="Outage Watch API",
    version="1.0.0",
    servers=[
        {
            "url": "https://outage-watch-api.onrender.com/",
            "description": "Production server"
        }
    ]
)

@app.get("/")
def root():
    return {"status": "ok", "service": "outage-watch-api"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/official")
def official():
    return {"results": get_official_results()}

@app.get("/crowd/payments")
def crowd_payments():
    return get_crowd_results("payments")

@app.get("/crowd/telecoms")
def crowd_telecoms():
    return get_crowd_results("telecoms")

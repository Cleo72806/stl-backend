# ============================================================
# main.py — STL Dashboard FastAPI Backend
# Deploy on Render (free tier). Set env vars in Render dashboard.
# ============================================================

from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import os
import pandas as pd
from datetime import date, timedelta
from typing import Optional
import logging

from storage import load_latest, load_remaining, load_for_date, save_parquet
from preprocessor import process_uploaded_file

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── In-memory cache (lives for the lifetime of the Render process)
_master_df: Optional[pd.DataFrame] = None
_loaded_files: list[str] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the latest parquet from Drive on startup."""
    global _master_df, _loaded_files
    try:
        result = load_latest()
        if result["data"] is not None:
            _master_df = result["data"]
            _loaded_files = list(result["loaded"])
            logger.info(f"Startup: loaded {result['loaded']} ({len(_master_df)} rows)")
    except Exception as e:
        logger.warning(f"Startup load failed: {e}")
    yield


app = FastAPI(title="STL Dashboard API", lifespan=lifespan)

# ── CORS — allow all origins so any Vercel URL works without config changes
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

CORRECT_PASSWORD = os.getenv("CORRECT_PASSWORD", "STL01.")
UPLOAD_PASSWORD  = os.getenv("UPLOAD_PASSWORD",  "STL01!")


# ── Helpers ──────────────────────────────────────────────────

def _get_df() -> Optional[pd.DataFrame]:
    return _master_df


def _filter(
    df: pd.DataFrame,
    provider: str,
    customer: str,
    date_start: date,
    date_end: date,
) -> pd.DataFrame:
    mask = (
        (df["Provider"] == provider)
        & (df["Customer"] == customer)
        & (df["Date"] >= pd.Timestamp(date_start))
        & (df["Date"] <= pd.Timestamp(date_end))
    )
    sub = df[mask].copy()
    if sub.empty:
        return sub

    agg = sub.groupby(["Date", "Metric"], as_index=False)["Value"].sum()
    wide = agg.pivot(index="Date", columns="Metric", values="Value").reset_index()
    wide.columns.name = None

    for col in ["aFm", "bFm", "xFm", "yFm", "zFm", "xFh"]:
        if col not in wide.columns:
            wide[col] = 0.0

    bkwh = wide["bFm"] * 1000
    wide["aRate"] = wide["aFm"].div(bkwh).where(bkwh != 0, 0)
    wide["yRate"] = wide["yFm"].div(bkwh).where(bkwh != 0, 0)
    wide["zRate"] = wide["zFm"].div(bkwh).where(bkwh != 0, 0)
    wide["xRate"] = wide["xFm"].div(bkwh).where(bkwh != 0, 0)

    wide = wide.sort_values("Date")
    wide["Date"] = wide["Date"].dt.strftime("%Y-%m-%d")
    return wide


# ── Auth ─────────────────────────────────────────────────────

@app.post("/auth/login")
def login(body: dict):
    if body.get("password") == CORRECT_PASSWORD:
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Incorrect password")


# ── Data ─────────────────────────────────────────────────────

@app.get("/data/customers")
def get_customers(provider: str = Query(...)):
    df = _get_df()
    if df is None or df.empty:
        return {"customers": []}
    custs = sorted(df[df["Provider"] == provider]["Customer"].unique().tolist())
    if "All Customers" in custs:
        custs = ["All Customers"] + [c for c in custs if c != "All Customers"]
    return {"customers": custs}


@app.get("/data/daily")
def get_daily(
    provider: str = Query(...),
    customer: str = Query(...),
    date_start: date = Query(...),
    date_end: date   = Query(...),
):
    df = _get_df()
    if df is None:
        raise HTTPException(503, "Data not loaded yet. Try again in a moment.")

    result = _filter(df, provider, customer, date_start, date_end)
    if result.empty:
        return {"rows": [], "message": f"No data for {customer} in selected range."}

    return {"rows": result.to_dict(orient="records")}


@app.get("/data/load-remaining")
def trigger_load_remaining(
    date_code: Optional[str] = Query(None, description="YYYYMM — load specific month")
):
    global _master_df, _loaded_files

    try:
        if date_code:
            chunk = load_for_date(date_code, already=_loaded_files)
            label = date_code
        else:
            chunk = load_remaining(already=_loaded_files)
            label = "remaining"

        if chunk is not None and not chunk.empty:
            if _master_df is not None:
                _master_df = pd.concat([_master_df, chunk], ignore_index=True)
            else:
                _master_df = chunk
            new_files = chunk["_source_file"].dropna().unique().tolist() if "_source_file" in chunk.columns else []
            _loaded_files.extend(new_files)
            return {"ok": True, "rows_added": len(chunk), "label": label}
        return {"ok": True, "rows_added": 0, "label": label}
    except Exception as e:
        logger.error(f"load-remaining error: {e}")
        return {"ok": False, "error": str(e)}


# ── Upload ───────────────────────────────────────────────────

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    x_admin_key: str = Header(None),
):
    if x_admin_key != UPLOAD_PASSWORD:
        raise HTTPException(403, "Invalid admin key")

    contents = await file.read()
    try:
        output_name = process_uploaded_file(contents, file.filename)
        global _master_df, _loaded_files
        result = load_latest()
        _master_df = result["data"]
        _loaded_files = list(result["loaded"])
        return {"ok": True, "file": output_name}
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise HTTPException(500, str(e))


# ── Health ───────────────────────────────────────────────────

@app.get("/health")
def health():
    rows = len(_master_df) if _master_df is not None else 0
    return {"status": "ok", "rows_in_memory": rows}
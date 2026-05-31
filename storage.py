import os
import io
import re
import logging
import tempfile
from typing import Optional
import pandas as pd

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

logger = logging.getLogger(__name__)

GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "0AIJ4hO71eD3yUk9PVA")
MASTER_FILENAME  = "master.parquet"
SCOPES = ["https://www.googleapis.com/auth/drive"]

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "stl_parquet_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

_service = None


def _get_service():
    global _service
    if _service is not None:
        return _service
    sa_json = os.getenv("GOOGLE_SA_JSON")
    if sa_json:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
        tmp.write(sa_json)
        tmp.close()
        creds = service_account.Credentials.from_service_account_file(tmp.name, scopes=SCOPES)
    else:
        local_path = os.path.join("credentials", "service-account.json")
        if not os.path.exists(local_path):
            raise FileNotFoundError("Set GOOGLE_SA_JSON env var or place JSON at credentials/service-account.json")
        creds = service_account.Credentials.from_service_account_file(local_path, scopes=SCOPES)
    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def _list_parquets() -> list[dict]:
    svc = _get_service()
    results = svc.files().list(
        q=f"'{GDRIVE_FOLDER_ID}' in parents and trashed = false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    files = [f for f in files if f["name"].endswith(".parquet")]

    def sort_key(f):
        if f["name"] == MASTER_FILENAME:
            return "999999"
        m = re.search(r"\d{6}", f["name"])
        return m.group() if m else "000000"

    return sorted(files, key=sort_key, reverse=True)


def _download_one(file_id: str, fname: str) -> Optional[pd.DataFrame]:
    cache_path = os.path.join(_CACHE_DIR, fname)
    if os.path.exists(cache_path):
        try:
            df = pd.read_parquet(cache_path)
            df["Date"] = pd.to_datetime(df["Date"])
            logger.info(f"  ~ cache hit: {fname}")
            return df
        except Exception:
            pass
    svc = _get_service()
    buf = io.BytesIO()
    request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    try:
        df = pd.read_parquet(buf)
        df["Date"] = pd.to_datetime(df["Date"])
        df["_source_file"] = fname
        df.to_parquet(cache_path)
        logger.info(f"  v downloaded: {fname} ({len(df)} rows)")
        return df
    except Exception as e:
        logger.warning(f"  x failed '{fname}': {e}")
        return None


def load_latest() -> dict:
    files = _list_parquets()
    if not files:
        return {"data": None, "loaded": [], "remaining": [], "all_files": []}

    # Try master.parquet first — one file, all history, fast cold start
    master_file = next((f for f in files if f["name"] == MASTER_FILENAME), None)
    if master_file:
        df = _download_one(master_file["id"], master_file["name"])
        if df is not None:
            logger.info(f"Startup: loaded master.parquet ({len(df)} rows)")
            return {"data": df, "loaded": [MASTER_FILENAME], "remaining": [], "all_files": files}

    # Fallback: no master yet, load latest monthly file
    monthly = [f for f in files if f["name"] != MASTER_FILENAME]
    if not monthly:
        return {"data": None, "loaded": [], "remaining": [], "all_files": files}
    latest = monthly[0]
    df = _download_one(latest["id"], latest["name"])
    remaining = [f["name"] for f in monthly[1:]]
    return {"data": df, "loaded": [latest["name"]], "remaining": remaining, "all_files": files}


def load_remaining(already: list[str]) -> Optional[pd.DataFrame]:
    files = _list_parquets()
    to_load = [f for f in files if f["name"] not in already and f["name"] != MASTER_FILENAME]
    if not to_load:
        return None
    chunks = []
    for f in to_load:
        df = _download_one(f["id"], f["name"])
        if df is not None:
            chunks.append(df)
    return pd.concat(chunks, ignore_index=True) if chunks else None


def load_for_date(date_code: str, already: list[str]) -> Optional[pd.DataFrame]:
    files = _list_parquets()
    matches = [f for f in files if date_code in f["name"] and f["name"] not in already and f["name"] != MASTER_FILENAME]
    if not matches:
        return None
    return _download_one(matches[0]["id"], matches[0]["name"])


def save_parquet(df: pd.DataFrame, filename: str):
    if df is None or df.empty:
        logger.warning(f"save_parquet: empty df, skipping '{filename}'")
        return
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)
    svc = _get_service()
    files = _list_parquets()
    existing = [f for f in files if f["name"] == filename]
    media = MediaIoBaseUpload(buf, mimetype="application/octet-stream", resumable=False)
    if existing:
        svc.files().update(fileId=existing[0]["id"], media_body=media, supportsAllDrives=True).execute()
        logger.info(f"Google Drive: updated '{filename}'")
    else:
        svc.files().create(body={"name": filename, "parents": [GDRIVE_FOLDER_ID]}, media_body=media, supportsAllDrives=True).execute()
        logger.info(f"Google Drive: created '{filename}'")
    cache_path = os.path.join(_CACHE_DIR, filename)
    buf.seek(0)
    with open(cache_path, "wb") as f:
        f.write(buf.read())


def rebuild_master():
    files = _list_parquets()
    monthly = [f for f in files if f["name"] != MASTER_FILENAME]
    if not monthly:
        logger.warning("rebuild_master: no monthly files found, skipping.")
        return
    logger.info(f"rebuild_master: merging {len(monthly)} monthly files...")
    chunks = []
    for f in monthly:
        df = _download_one(f["id"], f["name"])
        if df is not None:
            chunks.append(df)
    if not chunks:
        logger.warning("rebuild_master: all downloads failed, skipping.")
        return
    master = pd.concat(chunks, ignore_index=True)
    master["Date"] = pd.to_datetime(master["Date"])
    if "_source_file" in master.columns:
        master = master.drop(columns=["_source_file"])
    save_parquet(master, MASTER_FILENAME)
    logger.info(f"rebuild_master: done. master.parquet = {len(master)} rows.")
    cached = os.path.join(_CACHE_DIR, MASTER_FILENAME)
    if os.path.exists(cached):
        os.remove(cached)
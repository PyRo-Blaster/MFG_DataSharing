import csv
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles


DAYS = 18
PARAM_KEYS = ["vcd", "viability", "glucose", "lactate", "nh4", "ph", "pco2", "osm"]
BATCH_KEYS = ["50L", "2L", "satellite", "500L"]

CSV_BATCH_MAP = {
    "500l_gmp": "500L",
    "500l": "500L",
    "3908260401": "500L",
    "5l_satellite": "satellite",
    "satellite": "satellite",
    "2l_scout": "2L",
    "2l": "2L",
    "50l_pilot": "50L",
    "50l": "50L",
}
CSV_PARAM_MAP = {
    "vcd": "vcd",
    "via": "viability",
    "viability": "viability",
    "gluc": "glucose",
    "glucose": "glucose",
    "lac": "lactate",
    "lactate": "lactate",
    "nh4": "nh4",
    "nh4+": "nh4",
    "ammonium": "nh4",
    "ph": "ph",
    "pco2": "pco2",
    "co2": "pco2",
    "osm": "osm",
    "osmolality": "osm",
}
WRITE_LOCK = Lock()
SESSION_COOKIE_NAME = "mfg_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSIONS: dict[str, dict[str, Any]] = {}

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DATA_PATH = BASE_DIR / "data.json"

app = FastAPI(title="MFG Data Sharing")
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


def get_data_path() -> Path:
    return Path(os.environ.get("MFG_DATA_PATH", str(DEFAULT_DATA_PATH)))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_batch(value: str) -> str | None:
    return CSV_BATCH_MAP.get(value.strip().lower().replace(" ", ""))


def normalize_param(value: str) -> str | None:
    cleaned = value.strip().lower().replace(" ", "").replace("⁺", "+")
    return CSV_PARAM_MAP.get(cleaned)


def empty_store() -> dict[str, Any]:
    store: dict[str, Any] = {}
    for batch_key in BATCH_KEYS:
        store[batch_key] = {}
        for param_key in PARAM_KEYS:
            store[batch_key][param_key] = [None] * DAYS
    store["updated_at"] = None
    return store


def build_default_data() -> dict[str, Any]:
    return empty_store()


def ensure_data_shape(data: dict[str, Any]) -> dict[str, Any]:
    base = build_default_data()
    for batch_key in BATCH_KEYS:
        batch = data.get(batch_key, {})
        for param_key in PARAM_KEYS:
            values = batch.get(param_key, [])
            normalized = list(values[:DAYS]) + [None] * max(0, DAYS - len(values))
            base[batch_key][param_key] = normalized[:DAYS]
    updated_at = data.get("updated_at")
    base["updated_at"] = updated_at if updated_at else base["updated_at"]
    return base


def write_data(data: dict[str, Any]) -> None:
    data_path = get_data_path()
    data_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = ensure_data_shape(data)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=data_path.parent) as tmp:
        json.dump(normalized, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        temp_name = tmp.name
    try:
        os.replace(temp_name, data_path)
    except OSError as exc:
        # Docker bind-mounted single files can reject atomic replace with Errno 16.
        if exc.errno != 16:
            raise
        with open(temp_name, "r", encoding="utf-8") as src, data_path.open("w", encoding="utf-8") as dst:
            dst.write(src.read())
        os.unlink(temp_name)


def load_data() -> dict[str, Any]:
    data_path = get_data_path()
    if not data_path.exists():
        data = build_default_data()
        write_data(data)
        return data
    with data_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return ensure_data_shape(raw)

def get_login_credentials() -> tuple[str, str]:
    username = os.environ.get("MFG_USERNAME")
    password = os.environ.get("MFG_PASSWORD")
    if not username or not password:
        raise HTTPException(status_code=500, detail="Login credentials are not configured")
    return username, password


def purge_expired_sessions() -> None:
    now = datetime.now(timezone.utc).timestamp()
    expired = [
        session_id
        for session_id, payload in SESSIONS.items()
        if now - payload["created_at"] > SESSION_TTL_SECONDS
    ]
    for session_id in expired:
        SESSIONS.pop(session_id, None)


def get_active_session(request: Request) -> dict[str, Any] | None:
    purge_expired_sessions()
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return SESSIONS.get(session_id)


def require_login_api(request: Request) -> dict[str, Any]:
    session = get_active_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


def parse_csv_upload(text: str, data: dict[str, Any]) -> int:
    reader = csv.reader(text.splitlines())
    rows = list(reader)
    if len(rows) < 2:
        raise HTTPException(status_code=400, detail="CSV file is empty")

    header = [cell.strip().lower() for cell in rows[0]]
    day_columns: list[tuple[int, int]] = []
    for index, cell in enumerate(header):
        if cell.startswith("d") and cell[1:].isdigit():
            day = int(cell[1:])
            if 0 <= day < DAYS:
                day_columns.append((index, day))
    if not day_columns:
        raise HTTPException(status_code=400, detail="CSV header must include D0..D17 columns")

    cells_updated = 0
    for row in rows[1:]:
        if len(row) < 2:
            continue
        batch_key = normalize_batch(row[0])
        param_key = normalize_param(row[1])
        if not batch_key or not param_key or batch_key == "50L":
            continue
        for column_index, day in day_columns:
            if column_index >= len(row):
                continue
            raw_value = row[column_index].strip()
            if raw_value == "":
                continue
            try:
                value = float(raw_value)
            except ValueError:
                continue
            data[batch_key][param_key][day] = value
            cells_updated += 1
    data["updated_at"] = now_iso()
    return cells_updated


@app.get("/api/data")
def get_data(request: Request) -> dict[str, Any]:
    require_login_api(request)
    with WRITE_LOCK:
        return load_data()


@app.post("/upload")
async def upload_csv(request: Request, file: UploadFile = File(...)) -> Response:
    if not get_active_session(request):
        return RedirectResponse(
            url="/login?message=session%20%E5%B7%B2%E8%BF%87%E6%9C%9F%EF%BC%8C%E8%AF%B7%E9%87%8D%E6%96%B0%E7%99%BB%E5%BD%95%E5%90%8E%E5%86%8D%E4%B8%8A%E4%BC%A0",
            status_code=303,
        )
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV file must be UTF-8 encoded") from exc

    with WRITE_LOCK:
        data = load_data()
        cells_updated = parse_csv_upload(text, data)
        write_data(data)

    return RedirectResponse(url=f"/?uploaded={cells_updated}", status_code=303)


@app.get("/")
def index(request: Request) -> Response:
    if not get_active_session(request):
        return RedirectResponse(url="/login", status_code=307)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
def login_page(request: Request) -> Response:
    if get_active_session(request):
        return RedirectResponse(url="/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)) -> Response:
    expected_username, expected_password = get_login_credentials()
    if username != expected_username or password != expected_password:
        return FileResponse(STATIC_DIR / "login.html", status_code=401)

    session_id = secrets.token_urlsafe(32)
    SESSIONS[session_id] = {
        "username": username,
        "created_at": datetime.now(timezone.utc).timestamp(),
    }
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        SESSIONS.pop(session_id, None)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response

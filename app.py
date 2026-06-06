import base64
import csv
import hashlib
import hmac
import io
import json
import logging
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


logging.basicConfig(
    level=os.environ.get("MFG_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("mfg")

DAYS = 18
PARAM_KEYS = ["vcd", "viability", "glucose", "lactate", "nh4", "ph", "pco2", "osm"]
BATCH_KEYS = ["50L", "2L", "satellite", "500L"]

# Plausible value ranges per parameter for a CHO fed-batch culture. Uploaded
# values outside these bounds are almost always unit or typo errors, so they are
# rejected rather than merged. Bounds are intentionally generous.
PARAM_RANGES: dict[str, tuple[float, float]] = {
    "vcd": (0.0, 100.0),
    "viability": (0.0, 100.0),
    "glucose": (0.0, 30.0),
    "lactate": (0.0, 20.0),
    "nh4": (0.0, 30.0),
    "ph": (5.0, 9.0),
    "pco2": (0.0, 300.0),
    "osm": (150.0, 600.0),
}

# Export labels round-trip back through the CSV import normalizers.
CSV_BATCH_EXPORT = {
    "50L": "50L_Pilot",
    "2L": "2L_Scout",
    "satellite": "5L_Satellite",
    "500L": "500L_GMP",
}
CSV_PARAM_EXPORT = {
    "vcd": "VCD",
    "viability": "VIA",
    "glucose": "Gluc",
    "lactate": "Lac",
    "nh4": "NH4",
    "ph": "pH",
    "pco2": "pCO2",
    "osm": "OSM",
}

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

# Process-stable fallback so sessions survive within a running process. Set
# MFG_SECRET_KEY in the environment to keep sessions valid across restarts and
# across multiple worker processes.
_EPHEMERAL_SECRET = secrets.token_bytes(32)

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


def get_secret_key() -> bytes:
    configured = os.environ.get("MFG_SECRET_KEY")
    if configured:
        return configured.encode("utf-8")
    return _EPHEMERAL_SECRET


def sign_session(username: str) -> str:
    created_at = int(datetime.now(timezone.utc).timestamp())
    payload = f"{username}|{created_at}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(get_secret_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_session(token: str) -> dict[str, Any] | None:
    if not token or token.count(".") != 1:
        return None
    encoded, signature = token.split(".", 1)
    expected = hmac.new(get_secret_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        username, created_raw = payload.rsplit("|", 1)
        created_at = int(created_raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if datetime.now(timezone.utc).timestamp() - created_at > SESSION_TTL_SECONDS:
        return None
    return {"username": username, "created_at": created_at}


def get_active_session(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return verify_session(token)


def require_login_api(request: Request) -> dict[str, Any]:
    session = get_active_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


def parse_csv_upload(text: str, data: dict[str, Any]) -> tuple[int, int]:
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
    cells_rejected = 0
    for row in rows[1:]:
        if len(row) < 2:
            continue
        batch_key = normalize_batch(row[0])
        param_key = normalize_param(row[1])
        if not batch_key or not param_key or batch_key == "50L":
            continue
        low, high = PARAM_RANGES.get(param_key, (float("-inf"), float("inf")))
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
            if not low <= value <= high:
                cells_rejected += 1
                logger.warning(
                    "rejected out-of-range value batch=%s param=%s day=%s value=%s",
                    batch_key,
                    param_key,
                    day,
                    value,
                )
                continue
            data[batch_key][param_key][day] = value
            cells_updated += 1
    data["updated_at"] = now_iso()
    return cells_updated, cells_rejected


def serialize_csv(data: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Batch", "Parameter"] + [f"D{day}" for day in range(DAYS)])
    for batch_key in BATCH_KEYS:
        for param_key in PARAM_KEYS:
            values = data[batch_key][param_key]
            cells = ["" if value is None else value for value in values]
            writer.writerow([CSV_BATCH_EXPORT[batch_key], CSV_PARAM_EXPORT[param_key]] + cells)
    return buffer.getvalue()


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with WRITE_LOCK:
            data = load_data()
        return {"status": "ok", "data_ok": True, "updated_at": data.get("updated_at")}
    except Exception as exc:  # pragma: no cover - defensive health signal
        logger.exception("health check failed")
        return {"status": "degraded", "data_ok": False, "error": str(exc)}


@app.get("/api/data")
def get_data(request: Request) -> dict[str, Any]:
    require_login_api(request)
    with WRITE_LOCK:
        return load_data()


@app.get("/api/export")
def export_csv(request: Request) -> Response:
    require_login_api(request)
    with WRITE_LOCK:
        data = load_data()
    csv_text = serialize_csv(data)
    filename = f"mfg-data-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
        cells_updated, cells_rejected = parse_csv_upload(text, data)
        write_data(data)

    logger.info("csv upload merged=%s rejected=%s", cells_updated, cells_rejected)
    return RedirectResponse(
        url=f"/?uploaded={cells_updated}&rejected={cells_rejected}", status_code=303
    )


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

    token = sign_session(username)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response

from contextlib import asynccontextmanager
import base64
import json
from datetime import date, datetime, timedelta, timezone
import mimetypes
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import psycopg
from openai import OpenAI
from psycopg.rows import dict_row
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict


# ============================================================
# DATABASE
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-5").strip()

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

SCHOLARSHIP_TABLE = "tg_scholarships"
TRADE_TABLE = "tg_trades"
MEDIA_TABLE = "tg_media_assets"

ALLOWED_MEDIA_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def get_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# ============================================================
# MODELS
# ============================================================

class Scholarship(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    provider: str
    description: str
    amount: str | None = None
    deadline: date | None = None
    eligibility: str | None = None
    source_channel: str
    source_message_id: str | None = None
    source_url: str | None = None
    posted_at: datetime
    is_reviewed: bool
    has_media: bool = False
    media_type: str | None = None
    media_file: str | None = None
    media_url: str | None = None


class ScholarshipCreate(BaseModel):
    title: str
    provider: str
    description: str
    amount: str | None = None
    deadline: date | None = None
    eligibility: str | None = None
    source_channel: str
    source_message_id: str | None = None
    source_url: str | None = None
    posted_at: datetime | None = None
    is_reviewed: bool = False


class TelegramScholarshipIn(BaseModel):
    source_group: str
    source_username: str | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int
    telegram_date: str | None = None
    captured_at: str
    original_text: str
    telegram_message_link: str | None = None
    has_media: bool = False
    media_type: str | None = None
    media_file: str | None = None
    media_url: str | None = None


class Trade(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_group: str
    source_username: str | None = None
    telegram_chat_id: str | None = None
    telegram_message_id: str
    telegram_date: str | None = None
    captured_at: str
    original_text: str | None = None
    telegram_message_link: str | None = None
    has_media: bool
    media_type: str | None = None
    media_file: str | None = None
    media_url: str | None = None
    status: str
    created_at: str | None = None


class TelegramTradeIn(BaseModel):
    source_group: str
    source_username: str | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int
    telegram_date: str | None = None
    captured_at: str
    original_text: str = ""
    telegram_message_link: str | None = None
    has_media: bool = False
    media_type: str | None = None
    media_file: str | None = None
    media_url: str | None = None


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def initialize_database() -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SCHOLARSHIP_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount TEXT,
                    deadline DATE,
                    eligibility TEXT,
                    source_channel TEXT NOT NULL,
                    source_message_id TEXT,
                    source_url TEXT,
                    posted_at TIMESTAMPTZ NOT NULL,
                    is_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
                    has_media BOOLEAN NOT NULL DEFAULT FALSE,
                    media_type TEXT,
                    media_file TEXT,
                    media_url TEXT,
                    UNIQUE(source_channel, source_message_id)
                )
                """
            )

            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TRADE_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    source_group TEXT NOT NULL,
                    source_username TEXT,
                    telegram_chat_id TEXT,
                    telegram_message_id TEXT NOT NULL,
                    telegram_date TEXT,
                    captured_at TEXT NOT NULL,
                    original_text TEXT,
                    telegram_message_link TEXT,
                    has_media BOOLEAN NOT NULL DEFAULT FALSE,
                    media_type TEXT,
                    media_file TEXT,
                    media_url TEXT,
                    status TEXT NOT NULL DEFAULT 'NEW',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(source_group, telegram_message_id)
                )
                """
            )

            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {MEDIA_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    category TEXT NOT NULL CHECK (category IN ('scholarship', 'trade')),
                    telegram_chat_id TEXT NOT NULL,
                    telegram_message_id TEXT NOT NULL,
                    filename TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL,
                    media_bytes BYTEA NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(category, telegram_chat_id, telegram_message_id)
                )
                """
            )

            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{SCHOLARSHIP_TABLE}_posted_at "
                f"ON {SCHOLARSHIP_TABLE}(posted_at DESC)"
            )
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{TRADE_TABLE}_captured_at "
                f"ON {TRADE_TABLE}(captured_at DESC)"
            )
        connection.commit()


# ============================================================
# HELPERS
# ============================================================

def row_to_scholarship(row: dict[str, Any]) -> Scholarship:
    values = dict(row)
    values["is_reviewed"] = bool(values["is_reviewed"])
    values["has_media"] = bool(values.get("has_media", False))
    return Scholarship(**values)


def row_to_trade(row: dict[str, Any]) -> Trade:
    values = dict(row)
    values["has_media"] = bool(values.get("has_media", False))
    if isinstance(values.get("created_at"), datetime):
        values["created_at"] = values["created_at"].isoformat()
    return Trade(**values)


def sanitize_media_identifier(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value).strip("_")
    return sanitized[:100] or "unknown"


def filename_from_media_url(media_url: str | None) -> str | None:
    if not media_url:
        return None
    path = urlparse(media_url).path
    filename = Path(path).name
    return filename or None


def scholarship_media_is_image(media_type: str | None) -> bool:
    if not media_type:
        return False
    lowered = media_type.lower()
    return lowered in {"photo", "image", "image document"} or lowered.startswith("image/")


def trade_media_is_image(media_type: str | None) -> bool:
    return scholarship_media_is_image(media_type)


def fetch_scholarships(
    where_sql: str = "",
    parameters: tuple[Any, ...] = (),
) -> list[Scholarship]:
    query = f"SELECT * FROM {SCHOLARSHIP_TABLE}"
    if where_sql:
        query += f" WHERE {where_sql}"
    query += " ORDER BY posted_at DESC"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, parameters)
            rows = cursor.fetchall()

    return [row_to_scholarship(row) for row in rows]


def get_media_asset(filename: str, category: str) -> dict[str, Any] | None:
    if Path(filename).name != filename:
        return None

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT filename, content_type, media_bytes
                FROM {MEDIA_TABLE}
                WHERE filename = %s AND category = %s
                """,
                (filename, category),
            )
            return cursor.fetchone()


# ============================================================
# STARTUP
# ============================================================

@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="telegram-scholarship-api",
    description="Scholarship and trade opportunities collected from monitored Telegram sources.",
    version="2.0.0",
    lifespan=lifespan,
)


# ============================================================
# ROOT / API INFO / HEALTH
# ============================================================

@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "telegram-scholarship-api",
        "message": "Scholarship and trade records collected from monitored Telegram sources.",
        "docs": "/docs",
    }


@app.get("/api")
def api_info() -> dict[str, Any]:
    return {
        "name": "telegram-scholarship-api",
        "version": app.version,
        "database": "PostgreSQL",
        "endpoints": [
            "POST /scholarships",
            "POST /scholarships/telegram",
            "POST /scholarships/media",
            "GET /scholarships/media/{filename}",
            "GET /scholarships/{item_id}/media",
            "GET /scholarships/{item_id}/image",
            "GET /scholarships/recent",
            "GET /scholarships/unreviewed",
            "GET /scholarships/today",
            "GET /scholarships/{item_id}",
            "POST /trades/telegram",
            "POST /trades/media",
            "GET /trades/media/{filename}",
            "GET /trades/{item_id}/media",
            "GET /trades/{item_id}/image",
            "POST /trades/{item_id}/vision",
            "GET /trades/recent",
            "GET /trades/{item_id}",
        ],
    }


@app.get("/api/healthz")
def health() -> dict[str, str]:
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return {"status": "ok", "database": "postgresql"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {type(exc).__name__}")


# ============================================================
# SCHOLARSHIPS
# ============================================================

@app.post("/scholarships", response_model=Scholarship, status_code=status.HTTP_201_CREATED)
def create_scholarship(scholarship: ScholarshipCreate) -> Scholarship:
    posted_at = scholarship.posted_at or datetime.now(timezone.utc)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {SCHOLARSHIP_TABLE}
                (
                    title, provider, description, amount, deadline, eligibility,
                    source_channel, source_message_id, source_url, posted_at, is_reviewed
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    scholarship.title,
                    scholarship.provider,
                    scholarship.description,
                    scholarship.amount,
                    scholarship.deadline,
                    scholarship.eligibility,
                    scholarship.source_channel,
                    scholarship.source_message_id,
                    scholarship.source_url,
                    posted_at,
                    scholarship.is_reviewed,
                ),
            )
            row = cursor.fetchone()
        connection.commit()

    if row is None:
        raise HTTPException(status_code=500, detail="Scholarship could not be created")
    return row_to_scholarship(row)


@app.post("/scholarships/telegram")
def receive_telegram_scholarship(item: TelegramScholarshipIn) -> dict[str, Any]:
    text = item.original_text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Telegram message contains no text")

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "Telegram Scholarship")
    title = first_line[:250]

    try:
        posted_at = datetime.fromisoformat((item.telegram_date or item.captured_at).replace("Z", "+00:00"))
    except Exception:
        posted_at = datetime.now(timezone.utc)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id
                FROM {SCHOLARSHIP_TABLE}
                WHERE source_channel = %s AND source_message_id = %s
                LIMIT 1
                """,
                (item.source_group, str(item.telegram_message_id)),
            )
            existing = cursor.fetchone()

            if existing is not None:
                return {
                    "status": "duplicate",
                    "id": existing["id"],
                    "telegram_message_id": item.telegram_message_id,
                }

            cursor.execute(
                f"""
                INSERT INTO {SCHOLARSHIP_TABLE}
                (
                    title, provider, description, amount, deadline, eligibility,
                    source_channel, source_message_id, source_url, posted_at, is_reviewed,
                    has_media, media_type, media_file, media_url
                )
                VALUES (%s, %s, %s, NULL, NULL, NULL, %s, %s, %s, %s, FALSE, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    title,
                    item.source_group,
                    text,
                    item.source_group,
                    str(item.telegram_message_id),
                    item.telegram_message_link,
                    posted_at,
                    bool(item.has_media and item.media_url),
                    item.media_type,
                    item.media_file,
                    item.media_url,
                ),
            )
            created = cursor.fetchone()
        connection.commit()

    return {
        "status": "saved",
        "id": created["id"],
        "telegram_message_id": item.telegram_message_id,
    }


@app.post("/scholarships/media")
async def upload_scholarship_media(
    request: Request,
    file: UploadFile = File(...),
    telegram_chat_id: str = Form(...),
    telegram_message_id: str = Form(...),
) -> dict[str, str]:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported scholarship media type")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty media file")

    chat = sanitize_media_identifier(telegram_chat_id)
    message = sanitize_media_identifier(telegram_message_id)
    extension = ALLOWED_MEDIA_TYPES[content_type]
    filename = f"scholarship_{chat}_{message}{extension}"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {MEDIA_TABLE}
                    (category, telegram_chat_id, telegram_message_id, filename, content_type, media_bytes)
                VALUES ('scholarship', %s, %s, %s, %s, %s)
                ON CONFLICT (category, telegram_chat_id, telegram_message_id)
                DO UPDATE SET
                    filename = EXCLUDED.filename,
                    content_type = EXCLUDED.content_type,
                    media_bytes = EXCLUDED.media_bytes,
                    created_at = NOW()
                """,
                (telegram_chat_id, telegram_message_id, filename, content_type, data),
            )
        connection.commit()

    media_url = str(request.url_for("get_scholarship_media", filename=filename))
    return {"status": "saved", "filename": filename, "media_url": media_url}


@app.get("/scholarships/media/{filename}", name="get_scholarship_media")
def get_scholarship_media(filename: str) -> Response:
    asset = get_media_asset(filename, "scholarship")
    if asset is None:
        raise HTTPException(status_code=404, detail="Scholarship media file not found")
    return Response(
        content=bytes(asset["media_bytes"]),
        media_type=asset["content_type"],
        headers={"Content-Disposition": f'inline; filename="{asset["filename"]}"'},
    )


@app.get("/scholarships/{item_id}/media")
def scholarship_media_info(item_id: int) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, title, has_media, media_type, media_file, media_url
                FROM {SCHOLARSHIP_TABLE}
                WHERE id = %s
                """,
                (item_id,),
            )
            row = cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Scholarship not found")
    if not row["has_media"]:
        raise HTTPException(status_code=404, detail="Scholarship has no media")

    return {
        "id": row["id"],
        "title": row["title"],
        "has_media": bool(row["has_media"]),
        "media_type": row["media_type"],
        "media_file": row["media_file"],
        "media_url": row["media_url"],
    }


@app.get("/scholarships/{item_id}/image", response_model=None)
def get_scholarship_image(item_id: int):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, has_media, media_type, media_url
                FROM {SCHOLARSHIP_TABLE}
                WHERE id = %s
                """,
                (item_id,),
            )
            row = cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Scholarship record not found")
    if not row["has_media"]:
        raise HTTPException(status_code=404, detail="Scholarship has no media")
    if not scholarship_media_is_image(row["media_type"]):
        raise HTTPException(status_code=400, detail="Scholarship media is not an image")

    filename = filename_from_media_url(row["media_url"])
    if not filename:
        return JSONResponse(
            status_code=404,
            content={"detail": "Scholarship image file not found", "media_url": row["media_url"]},
        )

    asset = get_media_asset(filename, "scholarship")
    if asset is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Scholarship image file not found", "media_url": row["media_url"]},
        )

    return Response(content=bytes(asset["media_bytes"]), media_type=asset["content_type"])


@app.get("/scholarships/recent", response_model=list[Scholarship])
def recent_scholarships(
    days: int = Query(default=7, ge=1, le=90, description="Look back this many days."),
) -> list[Scholarship]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return fetch_scholarships("posted_at >= %s", (cutoff,))


@app.get("/scholarships/unreviewed", response_model=list[Scholarship])
def unreviewed_scholarships() -> list[Scholarship]:
    return fetch_scholarships("is_reviewed = FALSE")


@app.get("/scholarships/today", response_model=list[Scholarship])
def today_scholarships() -> list[Scholarship]:
    today = datetime.now(timezone.utc).date()
    return fetch_scholarships("posted_at::date = %s", (today,))


@app.get("/scholarships/{item_id}", response_model=Scholarship)
def scholarship_by_id(item_id: int) -> Scholarship:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {SCHOLARSHIP_TABLE} WHERE id = %s",
                (item_id,),
            )
            row = cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Scholarship not found")
    return row_to_scholarship(row)


# ============================================================
# TRADES
# ============================================================

@app.post("/trades/telegram")
def receive_telegram_trade(item: TelegramTradeIn) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id FROM {TRADE_TABLE}
                WHERE source_group = %s AND telegram_message_id = %s
                LIMIT 1
                """,
                (item.source_group, str(item.telegram_message_id)),
            )
            existing = cursor.fetchone()

            if existing is not None:
                return {
                    "status": "duplicate",
                    "id": existing["id"],
                    "telegram_message_id": item.telegram_message_id,
                }

            cursor.execute(
                f"""
                INSERT INTO {TRADE_TABLE}
                (
                    source_group, source_username, telegram_chat_id,
                    telegram_message_id, telegram_date, captured_at,
                    original_text, telegram_message_link, has_media,
                    media_type, media_file, media_url, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'NEW')
                RETURNING id
                """,
                (
                    item.source_group,
                    item.source_username,
                    str(item.telegram_chat_id) if item.telegram_chat_id is not None else None,
                    str(item.telegram_message_id),
                    item.telegram_date,
                    item.captured_at,
                    item.original_text,
                    item.telegram_message_link,
                    bool(item.has_media and item.media_url),
                    item.media_type,
                    item.media_file,
                    item.media_url,
                ),
            )
            created = cursor.fetchone()
        connection.commit()

    return {
        "status": "saved",
        "id": created["id"],
        "telegram_message_id": item.telegram_message_id,
    }


@app.post("/trades/media")
async def upload_trade_media(
    request: Request,
    file: UploadFile = File(...),
    telegram_chat_id: str = Form(...),
    telegram_message_id: str = Form(...),
) -> dict[str, str]:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported trade media type")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty media file")

    chat = sanitize_media_identifier(telegram_chat_id)
    message = sanitize_media_identifier(telegram_message_id)
    extension = ALLOWED_MEDIA_TYPES[content_type]
    filename = f"trade_{chat}_{message}{extension}"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {MEDIA_TABLE}
                    (category, telegram_chat_id, telegram_message_id, filename, content_type, media_bytes)
                VALUES ('trade', %s, %s, %s, %s, %s)
                ON CONFLICT (category, telegram_chat_id, telegram_message_id)
                DO UPDATE SET
                    filename = EXCLUDED.filename,
                    content_type = EXCLUDED.content_type,
                    media_bytes = EXCLUDED.media_bytes,
                    created_at = NOW()
                """,
                (telegram_chat_id, telegram_message_id, filename, content_type, data),
            )
        connection.commit()

    media_url = str(request.url_for("get_trade_media", filename=filename))
    return {"status": "saved", "filename": filename, "media_url": media_url}


@app.get("/trades/media/{filename}", name="get_trade_media")
def get_trade_media(filename: str) -> Response:
    asset = get_media_asset(filename, "trade")
    if asset is None:
        raise HTTPException(status_code=404, detail="Trade media file not found")
    return Response(
        content=bytes(asset["media_bytes"]),
        media_type=asset["content_type"],
        headers={"Content-Disposition": f'inline; filename="{asset["filename"]}"'},
    )


@app.get("/trades/{item_id}/media")
def trade_media_info(item_id: int) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, has_media, media_type, media_file, media_url
                FROM {TRADE_TABLE}
                WHERE id = %s
                """,
                (item_id,),
            )
            row = cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Trade record not found")
    if not row["has_media"]:
        raise HTTPException(status_code=404, detail="Trade has no media")

    return {
        "id": row["id"],
        "has_media": bool(row["has_media"]),
        "media_type": row["media_type"],
        "media_file": row["media_file"],
        "media_url": row["media_url"],
    }


@app.get("/trades/{item_id}/image")
def get_trade_image(item_id: int):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, has_media, media_type, media_url
                FROM {TRADE_TABLE}
                WHERE id = %s
                """,
                (item_id,),
            )
            row = cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Trade record not found")
    if not row["has_media"]:
        raise HTTPException(status_code=404, detail="Trade has no media")
    if not trade_media_is_image(row["media_type"]):
        raise HTTPException(status_code=400, detail="Trade media is not an image")

    filename = filename_from_media_url(row["media_url"])
    if not filename:
        return JSONResponse(
            status_code=404,
            content={"detail": "Trade image file not found", "media_url": row["media_url"]},
        )

    asset = get_media_asset(filename, "trade")
    if asset is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Trade image file not found", "media_url": row["media_url"]},
        )

    return Response(content=bytes(asset["media_bytes"]), media_type=asset["content_type"])



@app.post("/trades/{item_id}/vision")
def analyze_trade_vision(item_id: int) -> dict[str, Any]:
    """
    Analyze the actual stored Telegram trade chart with an OpenAI vision-capable model.

    The image is loaded from PostgreSQL and sent as a base64 data URL, so the
    analysis does not depend on the public media URL being fetchable by ChatGPT.
    """

    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY environment variable is not configured",
        )

    # Load the trade record.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    id,
                    source_group,
                    telegram_message_id,
                    original_text,
                    has_media,
                    media_type,
                    media_url
                FROM {TRADE_TABLE}
                WHERE id = %s
                """,
                (item_id,),
            )
            trade = cursor.fetchone()

    if trade is None:
        raise HTTPException(status_code=404, detail="Trade record not found")

    if not trade["has_media"]:
        raise HTTPException(status_code=404, detail="Trade has no media")

    if not trade_media_is_image(trade["media_type"]):
        raise HTTPException(status_code=400, detail="Trade media is not an image")

    filename = filename_from_media_url(trade["media_url"])
    if not filename:
        raise HTTPException(status_code=404, detail="Trade image filename not found")

    asset = get_media_asset(filename, "trade")
    if asset is None:
        raise HTTPException(status_code=404, detail="Trade image file not found")

    image_bytes = bytes(asset["media_bytes"])
    content_type = asset["content_type"]
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    image_data_url = f"data:{content_type};base64,{image_b64}"

    prompt = f"""
You are analyzing a Telegram trading chart screenshot.

DATABASE RECORD ID: {item_id}
SOURCE GROUP: {trade["source_group"]}
TELEGRAM MESSAGE ID: {trade["telegram_message_id"]}
TELEGRAM TEXT:
{trade["original_text"] or "(no text/caption)"}

Inspect ONLY what is actually visible in the chart image.

Extract:
- instrument/symbol
- timeframe
- trade direction or bias
- visible/planned entry
- visible stop loss
- visible take profit
- whether BOS is visibly marked or clearly evidenced
- whether CHoCH/MSS is visibly marked or clearly evidenced
- whether a liquidity sweep is visibly marked or clearly evidenced
- whether inducement is visibly marked or clearly evidenced
- visible POI/order block/supply/demand/entry zone
- a confidence score from 0 to 1

Rules:
- Never invent missing values.
- Use null when a value cannot be confirmed.
- A boolean must be null if it cannot be confirmed from the image.
- Do not infer an exact price unless it is visibly readable.
- Keep POI concise and factual.
"""

    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "instrument": {"type": ["string", "null"]},
            "timeframe": {"type": ["string", "null"]},
            "direction": {
                "type": ["string", "null"],
                "enum": ["BUY", "SELL", "BULLISH", "BEARISH", None],
            },
            "entry": {"type": ["string", "null"]},
            "stop_loss": {"type": ["string", "null"]},
            "take_profit": {"type": ["string", "null"]},
            "bos": {"type": ["boolean", "null"]},
            "choch_mss": {"type": ["boolean", "null"]},
            "liquidity_sweep": {"type": ["boolean", "null"]},
            "inducement": {"type": ["boolean", "null"]},
            "poi": {"type": ["string", "null"]},
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
        },
        "required": [
            "id",
            "instrument",
            "timeframe",
            "direction",
            "entry",
            "stop_loss",
            "take_profit",
            "bos",
            "choch_mss",
            "liquidity_sweep",
            "inducement",
            "poi",
            "confidence",
        ],
        "additionalProperties": False,
    }

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.responses.create(
            model=OPENAI_VISION_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        },
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                            "detail": "high",
                        },
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "trade_vision_analysis",
                    "schema": schema,
                    "strict": True,
                }
            },
        )

        if not response.output_text:
            raise RuntimeError("Vision model returned no text output")

        result = json.loads(response.output_text)
        result["id"] = item_id
        return result

    except HTTPException:
        raise
    except Exception as exc:
        # Do not expose API keys or full upstream payloads in the public response.
        raise HTTPException(
            status_code=502,
            detail=f"Vision analysis failed: {type(exc).__name__}: {str(exc)[:300]}",
        )


@app.get("/trades/recent", response_model=list[Trade])
def recent_trades(
    limit: int = Query(default=20, ge=1, le=100, description="Maximum number of recent trade posts."),
) -> list[Trade]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM {TRADE_TABLE}
                ORDER BY captured_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()

    return [row_to_trade(row) for row in rows]


@app.get("/trades/{item_id}", response_model=Trade)
def trade_by_id(item_id: int) -> Trade:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {TRADE_TABLE} WHERE id = %s",
                (item_id,),
            )
            row = cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Trade record not found")
    return row_to_trade(row)

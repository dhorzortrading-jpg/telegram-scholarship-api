from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import mimetypes
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from urllib.parse import quote


# ============================================================
# DATABASE
# ============================================================

DATABASE_PATH = Path(__file__).with_name("scholarships.db")
MEDIA_DIRECTORY = Path(__file__).with_name("trade_media")

ALLOWED_MEDIA_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}
MEDIA_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


# ============================================================
# SCHOLARSHIP MODELS
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


# ============================================================
# TRADE MODELS
# ============================================================

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
    MEDIA_DIRECTORY.mkdir(parents=True, exist_ok=True)

    with get_connection() as connection:

        # ----------------------------------------------------
        # SCHOLARSHIPS TABLE
        # ----------------------------------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scholarships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                provider TEXT NOT NULL,
                description TEXT NOT NULL,
                amount TEXT,
                deadline TEXT,
                eligibility TEXT,
                source_channel TEXT NOT NULL,
                source_message_id TEXT,
                source_url TEXT,
                posted_at TEXT NOT NULL,
                is_reviewed INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # ----------------------------------------------------
        # TRADES TABLE
        # ----------------------------------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_group TEXT NOT NULL,
                source_username TEXT,
                telegram_chat_id TEXT,
                telegram_message_id TEXT NOT NULL,
                telegram_date TEXT,
                captured_at TEXT NOT NULL,
                original_text TEXT,
                telegram_message_link TEXT,
                has_media INTEGER NOT NULL DEFAULT 0,
                media_type TEXT,
                media_file TEXT,
                media_url TEXT,
                status TEXT NOT NULL DEFAULT 'NEW',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_group, telegram_message_id)
            )
            """
        )

        trade_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(trades)").fetchall()
        }
        if "media_url" not in trade_columns:
            connection.execute("ALTER TABLE trades ADD COLUMN media_url TEXT")

        # ----------------------------------------------------
        # EXISTING SAMPLE SCHOLARSHIP DATA
        # ----------------------------------------------------

        existing = connection.execute(
            "SELECT COUNT(*) FROM scholarships"
        ).fetchone()[0]

        if existing == 0:
            now = datetime.now(timezone.utc)

            connection.executemany(
                """
                INSERT INTO scholarships
                (
                    title,
                    provider,
                    description,
                    amount,
                    deadline,
                    eligibility,
                    source_channel,
                    source_message_id,
                    source_url,
                    posted_at,
                    is_reviewed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "Global Excellence Scholarship",
                        "Future Leaders Foundation",
                        "Merit-based support for students pursuing undergraduate study.",
                        "$10,000",
                        (date.today() + timedelta(days=45)).isoformat(),
                        "International students with strong academic records.",
                        "@scholarship_updates",
                        "1842",
                        "https://t.me/scholarship_updates/1842",
                        now.isoformat(),
                        0,
                    ),
                    (
                        "Women in Technology Award",
                        "TechForward",
                        "Funding for women beginning a degree in computer science or engineering.",
                        "$5,000",
                        (date.today() + timedelta(days=21)).isoformat(),
                        "Women enrolled or accepted into an eligible technology program.",
                        "@opportunities_hub",
                        "917",
                        "https://t.me/opportunities_hub/917",
                        (now - timedelta(days=1)).isoformat(),
                        1,
                    ),
                    (
                        "African Graduate Research Grant",
                        "Pan-African Research Network",
                        "Research funding for graduate students working on development-focused topics.",
                        "€7,500",
                        (date.today() + timedelta(days=60)).isoformat(),
                        "Citizens of an African country enrolled in a graduate program.",
                        "@africa_funding",
                        "331",
                        "https://t.me/africa_funding/331",
                        (now - timedelta(days=3)).isoformat(),
                        0,
                    ),
                ],
            )

        connection.commit()


# ============================================================
# DATABASE HELPERS
# ============================================================

def row_to_scholarship(row: sqlite3.Row) -> Scholarship:
    values: dict[str, Any] = dict(row)
    values["is_reviewed"] = bool(values["is_reviewed"])
    return Scholarship(**values)


def row_to_trade(row: sqlite3.Row) -> Trade:
    values: dict[str, Any] = dict(row)
    values["has_media"] = bool(values["has_media"])
    return Trade(**values)


def sanitize_media_identifier(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value).strip("_")
    return sanitized[:100] or "unknown"


def resolve_media_path(filename: str) -> Path:
    media_root = MEDIA_DIRECTORY.resolve()
    candidate = (media_root / filename).resolve()

    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Media file not found")

    try:
        candidate.relative_to(media_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail="Media file not found",
        ) from exc

    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")

    return candidate


def fetch_scholarships(
    where: str = "",
    parameters: tuple[Any, ...] = (),
) -> list[Scholarship]:

    query = "SELECT * FROM scholarships"

    if where:
        query += f" WHERE {where}"

    query += " ORDER BY posted_at DESC"

    with get_connection() as connection:
        rows = connection.execute(
            query,
            parameters,
        ).fetchall()

    return [row_to_scholarship(row) for row in rows]


# ============================================================
# STARTUP
# ============================================================

@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="telegram-scholarship-api",
    description=(
        "Scholarship and trade opportunities collected "
        "from monitored Telegram sources."
    ),
    version="1.2.0",
    lifespan=lifespan,
)


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "telegram-scholarship-api",
        "message": (
            "Scholarship and trade records collected "
            "from monitored Telegram sources."
        ),
        "docs": "/docs",
    }


# ============================================================
# API INFORMATION
# ============================================================

@app.get("/api")
def api_info() -> dict[str, Any]:
    return {
        "name": "telegram-scholarship-api",
        "version": app.version,
        "endpoints": [
            "POST /scholarships",
            "POST /scholarships/telegram",
            "GET /scholarships/recent",
            "GET /scholarships/unreviewed",
            "GET /scholarships/today",
            "GET /scholarships/{item_id}",
            "POST /trades/telegram",
            "POST /trades/media",
            "GET /trades/media/{filename}",
            "GET /trades/recent",
            "GET /trades/{item_id}",
        ],
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/api/healthz")
def health() -> dict[str, str]:
    return {
        "status": "ok",
    }


# ============================================================
# CREATE NEW SCHOLARSHIP
# ============================================================

@app.post(
    "/scholarships",
    response_model=Scholarship,
    status_code=status.HTTP_201_CREATED,
)
def create_scholarship(
    scholarship: ScholarshipCreate,
) -> Scholarship:

    posted_at = (
        scholarship.posted_at
        or datetime.now(timezone.utc)
    )

    with get_connection() as connection:

        cursor = connection.execute(
            """
            INSERT INTO scholarships
            (
                title,
                provider,
                description,
                amount,
                deadline,
                eligibility,
                source_channel,
                source_message_id,
                source_url,
                posted_at,
                is_reviewed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scholarship.title,
                scholarship.provider,
                scholarship.description,
                scholarship.amount,
                (
                    scholarship.deadline.isoformat()
                    if scholarship.deadline
                    else None
                ),
                scholarship.eligibility,
                scholarship.source_channel,
                scholarship.source_message_id,
                scholarship.source_url,
                posted_at.isoformat(),
                int(scholarship.is_reviewed),
            ),
        )

        connection.commit()

        new_id = cursor.lastrowid

        row = connection.execute(
            """
            SELECT *
            FROM scholarships
            WHERE id = ?
            """,
            (new_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=500,
            detail="Scholarship was created but could not be retrieved.",
        )

    return row_to_scholarship(row)


# ============================================================
# TELEGRAM SCHOLARSHIP INGESTION
# IMPORTANT: Must remain before /scholarships/{item_id}
# ============================================================

@app.post("/scholarships/telegram")
def receive_telegram_scholarship(
    item: TelegramScholarshipIn,
) -> dict[str, Any]:

    text = item.original_text.strip()

    if not text:
        raise HTTPException(
            status_code=400,
            detail="Telegram message contains no text",
        )

    first_line = next(
        (
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ),
        "Telegram Scholarship",
    )

    title = first_line[:250]
    posted_at = item.telegram_date or item.captured_at

    with get_connection() as connection:

        existing = connection.execute(
            """
            SELECT id
            FROM scholarships
            WHERE source_channel = ?
              AND source_message_id = ?
            LIMIT 1
            """,
            (
                item.source_group,
                str(item.telegram_message_id),
            ),
        ).fetchone()

        if existing is not None:
            return {
                "status": "duplicate",
                "id": existing["id"],
                "telegram_message_id": item.telegram_message_id,
            }

        cursor = connection.execute(
            """
            INSERT INTO scholarships
            (
                title,
                provider,
                description,
                amount,
                deadline,
                eligibility,
                source_channel,
                source_message_id,
                source_url,
                posted_at,
                is_reviewed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                item.source_group,
                text,
                None,
                None,
                None,
                item.source_group,
                str(item.telegram_message_id),
                item.telegram_message_link,
                posted_at,
                0,
            ),
        )

        connection.commit()
        scholarship_id = cursor.lastrowid

    return {
        "status": "saved",
        "id": scholarship_id,
        "telegram_message_id": item.telegram_message_id,
        "source": item.source_group,
    }


# ============================================================
# RECENT SCHOLARSHIPS
# ============================================================

@app.get(
    "/scholarships/recent",
    response_model=list[Scholarship],
)
def recent_scholarships(
    days: int = Query(
        default=7,
        ge=1,
        le=90,
        description="Look back this many days.",
    ),
) -> list[Scholarship]:

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=days)
    ).isoformat()

    return fetch_scholarships(
        "posted_at >= ?",
        (cutoff,),
    )


# ============================================================
# UNREVIEWED SCHOLARSHIPS
# ============================================================

@app.get(
    "/scholarships/unreviewed",
    response_model=list[Scholarship],
)
def unreviewed_scholarships() -> list[Scholarship]:
    return fetch_scholarships(
        "is_reviewed = 0"
    )


# ============================================================
# TODAY'S SCHOLARSHIPS
# ============================================================

@app.get(
    "/scholarships/today",
    response_model=list[Scholarship],
)
def today_scholarships() -> list[Scholarship]:

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    return fetch_scholarships(
        "date(posted_at) = ?",
        (today,),
    )


# ============================================================
# SCHOLARSHIP BY ID
# ============================================================

@app.get(
    "/scholarships/{item_id}",
    response_model=Scholarship,
)
def scholarship_by_id(
    item_id: int,
) -> Scholarship:

    with get_connection() as connection:

        row = connection.execute(
            """
            SELECT *
            FROM scholarships
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Scholarship not found",
        )

    return row_to_scholarship(row)


# ============================================================
# TELEGRAM TRADE INGESTION
# ============================================================

@app.post("/trades/telegram")
def receive_telegram_trade(
    item: TelegramTradeIn,
) -> dict[str, Any]:

    with get_connection() as connection:

        existing = connection.execute(
            """
            SELECT id
            FROM trades
            WHERE source_group = ?
              AND telegram_message_id = ?
            LIMIT 1
            """,
            (
                item.source_group,
                str(item.telegram_message_id),
            ),
        ).fetchone()

        if existing is not None:
            return {
                "status": "duplicate",
                "id": existing["id"],
                "telegram_message_id": item.telegram_message_id,
            }

        cursor = connection.execute(
            """
            INSERT INTO trades
            (
                source_group,
                source_username,
                telegram_chat_id,
                telegram_message_id,
                telegram_date,
                captured_at,
                original_text,
                telegram_message_link,
                has_media,
                media_type,
                media_file,
                media_url,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.source_group,
                item.source_username,
                (
                    str(item.telegram_chat_id)
                    if item.telegram_chat_id is not None
                    else None
                ),
                str(item.telegram_message_id),
                item.telegram_date,
                item.captured_at,
                item.original_text,
                item.telegram_message_link,
                int(item.has_media),
                item.media_type,
                item.media_file,
                item.media_url,
                "NEW",
            ),
        )

        connection.commit()
        trade_id = cursor.lastrowid

    return {
        "status": "saved",
        "id": trade_id,
        "telegram_message_id": item.telegram_message_id,
        "source": item.source_group,
    }


# ============================================================
# TRADE MEDIA UPLOAD AND RETRIEVAL
# ============================================================

@app.post("/trades/media")
async def upload_trade_media(
    request: Request,
    file: UploadFile = File(...),
    telegram_chat_id: str = Form(...),
    telegram_message_id: str = Form(...),
) -> dict[str, str]:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_MEDIA_TYPES:
        await file.close()
        raise HTTPException(
            status_code=400,
            detail="Unsupported media type. Use JPEG, PNG, or WebP.",
        )

    original_extension = Path(file.filename or "").suffix.lower()
    if original_extension not in ALLOWED_MEDIA_TYPES[content_type]:
        original_extension = MEDIA_TYPE_EXTENSIONS[content_type]

    safe_chat_id = sanitize_media_identifier(telegram_chat_id)
    safe_message_id = sanitize_media_identifier(telegram_message_id)
    filename = f"trade_{safe_chat_id}_{safe_message_id}{original_extension}"
    destination = MEDIA_DIRECTORY / filename

    if destination.exists():
        filename = (
            f"trade_{safe_chat_id}_{safe_message_id}_"
            f"{uuid4().hex[:8]}{original_extension}"
        )
        destination = MEDIA_DIRECTORY / filename

    contents = await file.read()
    await file.close()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded media is empty")

    MEDIA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(contents)

    media_url = (
        f"{str(request.base_url).rstrip('/')}/trades/media/{quote(filename, safe='')}"
    )
    return {
        "status": "saved",
        "filename": filename,
        "media_url": media_url,
    }


@app.get("/trades/media/{filename}")
def get_trade_media(filename: str) -> FileResponse:
    media_path = resolve_media_path(filename)
    media_type = mimetypes.guess_type(media_path.name)[0]
    return FileResponse(
        media_path,
        media_type=media_type or "application/octet-stream",
        filename=media_path.name,
    )


# ============================================================
# RECENT TRADES
# ============================================================

@app.get(
    "/trades/recent",
    response_model=list[Trade],
)
def recent_trades(
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of recent trade posts.",
    ),
) -> list[Trade]:

    with get_connection() as connection:

        rows = connection.execute(
            """
            SELECT *
            FROM trades
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        row_to_trade(row)
        for row in rows
    ]


# ============================================================
# TRADE BY ID
# ============================================================

@app.get(
    "/trades/{item_id}",
    response_model=Trade,
)
def trade_by_id(
    item_id: int,
) -> Trade:

    with get_connection() as connection:

        row = connection.execute(
            """
            SELECT *
            FROM trades
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Trade record not found",
        )

    return row_to_trade(row)
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict


DATABASE_PATH = Path(__file__).with_name("scholarships.db")


class Scholarship(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    provider: str
    description: str
    amount: str | None
    deadline: date | None
    eligibility: str | None
    source_channel: str
    source_message_id: str | None
    source_url: str | None
    posted_at: datetime
    is_reviewed: bool


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with get_connection() as connection:
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
        existing = connection.execute("SELECT COUNT(*) FROM scholarships").fetchone()[0]
        if existing == 0:
            now = datetime.now(timezone.utc)
            connection.executemany(
                """
                INSERT INTO scholarships
                (title, provider, description, amount, deadline, eligibility,
                 source_channel, source_message_id, source_url, posted_at, is_reviewed)
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


def row_to_scholarship(row: sqlite3.Row) -> Scholarship:
    values: dict[str, Any] = dict(row)
    values["is_reviewed"] = bool(values["is_reviewed"])
    return Scholarship(**values)


def fetch_scholarships(where: str = "", parameters: tuple[Any, ...] = ()) -> list[Scholarship]:
    query = "SELECT * FROM scholarships"
    if where:
        query += f" WHERE {where}"
    query += " ORDER BY posted_at DESC"
    with get_connection() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [row_to_scholarship(row) for row in rows]


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="telegram-scholarship-api",
    description="Scholarship opportunities collected from monitored Telegram sources.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "telegram-scholarship-api",
        "message": "Scholarship opportunities collected from monitored Telegram sources.",
        "docs": "/docs",
    }


@app.get("/api")
def api_info() -> dict[str, Any]:
    return {
        "name": "telegram-scholarship-api",
        "version": app.version,
        "endpoints": [
            "/scholarships/recent",
            "/scholarships/unreviewed",
            "/scholarships/today",
            "/scholarships/{item_id}",
        ],
    }


@app.get("/api/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/scholarships/recent", response_model=list[Scholarship])
def recent_scholarships(
    days: int = Query(default=7, ge=1, le=90, description="Look back this many days.")
) -> list[Scholarship]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return fetch_scholarships("posted_at >= ?", (cutoff,))


@app.get("/scholarships/unreviewed", response_model=list[Scholarship])
def unreviewed_scholarships() -> list[Scholarship]:
    return fetch_scholarships("is_reviewed = 0")


@app.get("/scholarships/today", response_model=list[Scholarship])
def today_scholarships() -> list[Scholarship]:
    today = date.today().isoformat()
    return fetch_scholarships("date(posted_at) = ?", (today,))


@app.get("/scholarships/{item_id}", response_model=Scholarship)
def scholarship_by_id(item_id: int) -> Scholarship:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM scholarships WHERE id = ?", (item_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Scholarship not found")
    return row_to_scholarship(row)

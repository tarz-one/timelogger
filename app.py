import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import gspread
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from google.oauth2.service_account import Credentials
from pydantic import BaseModel, Field

from parser import ParsedEntry, ParseError, parse_entry


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "aliases.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class LogRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Natural language time log entry")
    source: str = Field(default="api", description="Where the log came from")


class Settings(BaseModel):
    google_service_account_file: str
    google_sheet_name: str
    google_worksheet_name: str
    api_bearer_token: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Settings":
        missing = []
        for key in [
            "GOOGLE_SERVICE_ACCOUNT_FILE",
            "GOOGLE_SHEET_NAME",
            "GOOGLE_WORKSHEET_NAME",
        ]:
            if not os.getenv(key):
                missing.append(key)

        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        return cls(
            google_service_account_file=os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"],
            google_sheet_name=os.environ["GOOGLE_SHEET_NAME"],
            google_worksheet_name=os.environ["GOOGLE_WORKSHEET_NAME"],
            api_bearer_token=os.getenv("API_BEARER_TOKEN") or None,
        )

    @property
    def service_account_path(self) -> Path:
        path = Path(self.google_service_account_file).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path.resolve()


def load_config() -> Dict[str, Any]:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing config file at {CONFIG_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in config file: {exc}") from exc


class GoogleSheetsClient:
    def __init__(self, settings: Settings):
        credentials = Credentials.from_service_account_file(
            str(settings.service_account_path),
            scopes=SCOPES,
        )
        self.client = gspread.authorize(credentials)
        self.sheet = self.client.open(settings.google_sheet_name)
        self.worksheet = self.sheet.worksheet(settings.google_worksheet_name)

    def append_entry(self, entry: ParsedEntry, source: str) -> None:
        review_value = entry.review_notes if entry.review_notes else ("Needs review" if entry.needs_review else "")
        self.worksheet.insert_row(
            [
                entry.date.isoformat(),
                entry.client,
                entry.via,
                entry.project,
                entry.category,
                entry.task,
                entry.duration_hours,
                source,
                review_value,
            ],
            index=2,
            value_input_option="USER_ENTERED",
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings.from_env()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@lru_cache(maxsize=1)
def get_config() -> Dict[str, Any]:
    try:
        return load_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def verify_auth(
    authorization: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.api_bearer_token:
        return

    expected = f"Bearer {settings.api_bearer_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


app = FastAPI(
    title="timelogger",
    description="Lightweight backend for logging work time to Google Sheets.",
    version="1.0.0",
)


@app.get("/health")
def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/log", dependencies=[Depends(verify_auth)])
def create_log(
    payload: LogRequest,
    settings: Settings = Depends(get_settings),
    config: Dict[str, Any] = Depends(get_config),
) -> Dict[str, Any]:
    try:
        entry = parse_entry(payload.text, config)
        GoogleSheetsClient(settings).append_entry(entry, payload.source)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except gspread.GSpreadException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to write to Google Sheets: {exc}",
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "ok": True,
        "message": (
            f"Logged {entry.duration_hours:g}h"
            f"{' to ' + entry.project if entry.project else ''}"
            f" [{entry.category}] on {entry.date.isoformat()}"
        ),
        "entry": entry.model_dump(),
    }


@app.post("/parse", dependencies=[Depends(verify_auth)])
def preview_log(
    payload: LogRequest,
    config: Dict[str, Any] = Depends(get_config),
) -> Dict[str, Any]:
    try:
        entry = parse_entry(payload.text, config)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "message": "Parsed successfully.",
        "entry": entry.model_dump(),
    }


@app.post("/webhook", dependencies=[Depends(verify_auth)])
def webhook_log(
    payload: LogRequest,
    settings: Settings = Depends(get_settings),
    config: Dict[str, Any] = Depends(get_config),
) -> Dict[str, Any]:
    return create_log(payload=payload, settings=settings, config=config)

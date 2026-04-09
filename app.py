import json
import os
import threading
from calendar import month_name
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import gspread
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from google.oauth2.service_account import Credentials
from pydantic import BaseModel, Field

from parser import ParsedEntry, ParseError, parse_entry
from storage import (
    enqueue_delivery,
    load_queue,
    queue_size,
    save_queue,
    track_unknown_project,
)


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "aliases.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
STATE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
BACKGROUND_THREAD: Optional[threading.Thread] = None


class LogRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Natural language time log entry")
    source: str = Field(default="api", description="Where the log came from")


class Settings(BaseModel):
    google_service_account_file: Optional[str] = None
    google_service_account_json: Optional[str] = None
    google_sheet_name: str
    google_worksheet_name: str
    google_worksheet_template_name: Optional[str] = None
    api_bearer_token: Optional[str] = None
    queue_poll_seconds: int = 60
    auto_promote_threshold: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        missing = []
        for key in ["GOOGLE_SHEET_NAME", "GOOGLE_WORKSHEET_NAME"]:
            if not os.getenv(key):
                missing.append(key)

        if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") and not os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_FILE"
        ):
            missing.append("GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")

        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        return cls(
            google_service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or None,
            google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
            google_sheet_name=os.environ["GOOGLE_SHEET_NAME"],
            google_worksheet_name=os.environ["GOOGLE_WORKSHEET_NAME"],
            google_worksheet_template_name=os.getenv("GOOGLE_WORKSHEET_TEMPLATE_NAME")
            or None,
            api_bearer_token=os.getenv("API_BEARER_TOKEN") or None,
            queue_poll_seconds=int(os.getenv("QUEUE_POLL_SECONDS", "60")),
            auto_promote_threshold=int(os.getenv("AUTO_PROMOTE_THRESHOLD", "3")),
        )

    @property
    def service_account_path(self) -> Path:
        if not self.google_service_account_file:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_FILE is not set and no inline JSON credentials were provided."
            )
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
        self.settings = settings
        if settings.google_service_account_json:
            credentials = Credentials.from_service_account_info(
                json.loads(settings.google_service_account_json),
                scopes=SCOPES,
            )
        else:
            credentials = Credentials.from_service_account_file(
                str(settings.service_account_path),
                scopes=SCOPES,
            )
        self.client = gspread.authorize(credentials)
        self.sheet = self.client.open(settings.google_sheet_name)

    def append_entry(self, entry: ParsedEntry, source: str) -> None:
        worksheet = self.get_or_create_month_worksheet(entry)
        review_value = entry.review_notes if entry.review_notes else ("Needs review" if entry.needs_review else "")
        worksheet.insert_row(
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

    def get_or_create_month_worksheet(self, entry: ParsedEntry):
        worksheet_name = month_name[entry.date.month].upper()
        try:
            return self.sheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            return self.create_month_worksheet(worksheet_name)

    def create_month_worksheet(self, worksheet_name: str):
        template_name = (
            self.settings.google_worksheet_template_name
            or self.settings.google_worksheet_name
        )
        template = self.sheet.worksheet(template_name)
        response = self.sheet.batch_update(
            {
                "requests": [
                    {
                        "duplicateSheet": {
                            "sourceSheetId": template.id,
                            "newSheetName": worksheet_name,
                        }
                    }
                ]
            }
        )
        new_sheet_id = response["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
        worksheet = self.sheet.get_worksheet_by_id(new_sheet_id)
        worksheet.batch_clear([f"A2:{column_index_to_letter(worksheet.col_count)}{worksheet.row_count}"])
        return worksheet


def column_index_to_letter(column_index: int) -> str:
    letters = []
    current = column_index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(65 + remainder))
    return "".join(reversed(letters))


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


def append_entry_to_sheets(entry: ParsedEntry, source: str, settings: Settings) -> None:
    GoogleSheetsClient(settings).append_entry(entry, source)


def flush_queue(settings: Settings) -> Dict[str, int]:
    with STATE_LOCK:
        queue = load_queue()
        if not queue:
            return {"flushed": 0, "remaining": 0}

        remaining = []
        flushed = 0
        for item in queue:
            try:
                append_entry_to_sheets(
                    ParsedEntry.from_dict(item["entry"]),
                    item["source"],
                    settings,
                )
                flushed += 1
            except (gspread.GSpreadException, OSError):
                remaining.append(item)

        save_queue(remaining)
        return {"flushed": flushed, "remaining": len(remaining)}


def start_queue_worker() -> None:
    global BACKGROUND_THREAD
    settings = get_settings()

    if BACKGROUND_THREAD and BACKGROUND_THREAD.is_alive():
        return

    STOP_EVENT.clear()

    def worker() -> None:
        while not STOP_EVENT.is_set():
            try:
                flush_queue(settings)
            except Exception:
                pass
            STOP_EVENT.wait(settings.queue_poll_seconds)

    BACKGROUND_THREAD = threading.Thread(
        target=worker,
        name="timelogger-queue-worker",
        daemon=True,
    )
    BACKGROUND_THREAD.start()


app = FastAPI(
    title="timelogger",
    description="Lightweight backend for logging work time to Google Sheets.",
    version="1.0.0",
)


@app.on_event("startup")
def startup() -> None:
    start_queue_worker()
    try:
        flush_queue(get_settings())
    except HTTPException:
        pass


@app.on_event("shutdown")
def shutdown() -> None:
    STOP_EVENT.set()


@app.get("/health")
def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> Dict[str, str]:
    return {
        "name": "timelogger",
        "status": "ok",
        "health": "/health",
        "log": "/log",
    }


@app.post("/log", dependencies=[Depends(verify_auth)])
def create_log(
    payload: LogRequest,
    settings: Settings = Depends(get_settings),
    config: Dict[str, Any] = Depends(get_config),
) -> Dict[str, Any]:
    try:
        entry = parse_entry(payload.text, config)

        promotion_message = None
        with STATE_LOCK:
            promotion = track_unknown_project(
                entry,
                CONFIG_PATH,
                settings.auto_promote_threshold,
            )
            if promotion:
                get_config.cache_clear()
                promotion_message = promotion["message"]

        append_entry_to_sheets(entry, payload.source, settings)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except gspread.GSpreadException as exc:
        with STATE_LOCK:
            enqueue_delivery(entry, payload.source, str(exc))
            queued_total = queue_size()
        return {
            "ok": True,
            "queued": True,
            "message": (
                "Saved locally and will retry automatically when Google Sheets is available."
            ),
            "entry": entry.model_dump(),
            "queue_size": queued_total,
        }
    except OSError as exc:
        with STATE_LOCK:
            enqueue_delivery(entry, payload.source, str(exc))
            queued_total = queue_size()
        return {
            "ok": True,
            "queued": True,
            "message": "Saved locally and will retry automatically when the Mac is back online.",
            "entry": entry.model_dump(),
            "queue_size": queued_total,
        }

    with STATE_LOCK:
        queued_total = queue_size()

    response = {
        "ok": True,
        "message": (
            f"Logged {entry.duration_hours:g}h"
            f"{' to ' + entry.project if entry.project else ''}"
            f" [{entry.category}] on {entry.date.isoformat()}"
        ),
        "entry": entry.model_dump(),
        "queue_size": queued_total,
    }
    if promotion_message:
        response["promotion_message"] = promotion_message
    return response


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


@app.get("/queue-status", dependencies=[Depends(verify_auth)])
def queue_status() -> Dict[str, Any]:
    with STATE_LOCK:
        return {"queued": queue_size()}


@app.post("/flush", dependencies=[Depends(verify_auth)])
def flush_now(settings: Settings = Depends(get_settings)) -> Dict[str, int]:
    return flush_queue(settings)

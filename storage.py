import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from parser import ParsedEntry, normalize_phrase


DATA_DIR = Path(__file__).resolve().parent / ".timelogger"
QUEUE_PATH = DATA_DIR / "queue.json"
PROJECT_COUNTS_PATH = DATA_DIR / "project_counts.json"

UNKNOWN_PROJECT_RE = re.compile(
    r"(?:Inferred unknown project|Explicit project override for unknown project) '([^']+)'\."
)


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: Path, payload: Any) -> None:
    ensure_data_dir()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    temp_path.replace(path)


def enqueue_delivery(entry: ParsedEntry, source: str, reason: str) -> None:
    queue = load_queue()
    queue.append(
        {
            "entry": entry.model_dump(),
            "source": source,
            "reason": reason,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_json_file(QUEUE_PATH, queue)


def load_queue() -> List[Dict[str, Any]]:
    ensure_data_dir()
    return read_json_file(QUEUE_PATH, [])


def save_queue(queue: List[Dict[str, Any]]) -> None:
    write_json_file(QUEUE_PATH, queue)


def queue_size() -> int:
    return len(load_queue())


def track_unknown_project(
    entry: ParsedEntry,
    config_path: Path,
    threshold: int,
) -> Optional[Dict[str, str]]:
    if threshold <= 0:
        return None

    unknown_project = extract_unknown_project_name(entry.review_notes)
    if not unknown_project:
        return None

    counts = read_json_file(PROJECT_COUNTS_PATH, {})
    key = normalize_phrase(unknown_project)
    record = counts.get(key, {"count": 0, "project": unknown_project})
    record["count"] += 1
    record["project"] = entry.project or unknown_project
    if entry.client:
        record["client"] = entry.client
    if entry.via:
        record["via"] = entry.via
    if entry.category and entry.category != "General":
        record["default_category"] = entry.category

    if record["count"] < threshold:
        counts[key] = record
        write_json_file(PROJECT_COUNTS_PATH, counts)
        return None

    config = read_json_file(config_path, {})
    projects = config.setdefault("projects", [])
    if any(normalize_phrase(project["name"]) == key for project in projects):
        counts.pop(key, None)
        write_json_file(PROJECT_COUNTS_PATH, counts)
        return None

    aliases = [record["project"].lower()]
    compact_alias = record["project"].replace(" ", "").lower()
    if compact_alias != aliases[0]:
        aliases.append(compact_alias)

    promoted_project: Dict[str, Any] = {
        "name": record["project"],
        "aliases": aliases,
    }
    if record.get("client"):
        promoted_project["client"] = record["client"]
    if record.get("via"):
        promoted_project["via"] = record["via"]
    if record.get("default_category"):
        promoted_project["default_category"] = record["default_category"]

    projects.append(promoted_project)
    projects.sort(key=lambda item: normalize_phrase(item["name"]))
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")

    counts.pop(key, None)
    write_json_file(PROJECT_COUNTS_PATH, counts)

    return {
        "project": record["project"],
        "message": (
            f"Auto-promoted recurring project '{record['project']}' into aliases.json "
            f"after {threshold} uses."
        ),
    }


def extract_unknown_project_name(review_notes: str) -> Optional[str]:
    match = UNKNOWN_PROJECT_RE.search(review_notes)
    if not match:
        return None
    return match.group(1)

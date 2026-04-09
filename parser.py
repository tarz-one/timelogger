import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class ParseError(ValueError):
    pass


DATE_PATTERNS = [
    re.compile(r"\b(?P<iso>\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(?P<mdy>\d{1,2}/\d{1,2}/\d{2,4})\b"),
    re.compile(r"\b(?P<md>\d{1,2}/\d{1,2})\b"),
    re.compile(r"\b(?P<mdy_dash>\d{1,2}-\d{1,2}-\d{2,4})\b"),
    re.compile(r"\b(?P<md_dash>\d{1,2}-\d{1,2})\b"),
    re.compile(
        r"\b(?P<month_name>"
        r"(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|"
        r"jul|july|aug|august|sep|sept|september|oct|october|nov|november|"
        r"dec|december)"
        r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?"
        r")\b"
    ),
    re.compile(
        r"\b(?P<month_compact>"
        r"(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|"
        r"jul|july|aug|august|sep|sept|september|oct|october|nov|november|"
        r"dec|december)"
        r"\d{1,2}(?:st|nd|rd|th)?"
        r")\b"
    ),
]

TASK_CONNECTOR_WORDS = {
    "with",
    "for",
    "and",
    "to",
    "about",
    "re",
    "on",
    "at",
}

NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


@dataclass
class ParsedEntry:
    raw_input: str
    task: str
    project: str
    client: str
    via: str
    duration_minutes: int
    duration_hours: float
    category: str
    date: date
    needs_review: bool
    review_notes: str

    def model_dump(self) -> Dict[str, Any]:
        return {
            "raw_input": self.raw_input,
            "task": self.task,
            "project": self.project,
            "client": self.client,
            "via": self.via,
            "duration_minutes": self.duration_minutes,
            "duration_hours": self.duration_hours,
            "category": self.category,
            "date": self.date.isoformat(),
            "needs_review": self.needs_review,
            "review_notes": self.review_notes,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ParsedEntry":
        return cls(
            raw_input=payload["raw_input"],
            task=payload["task"],
            project=payload["project"],
            client=payload["client"],
            via=payload["via"],
            duration_minutes=int(payload["duration_minutes"]),
            duration_hours=float(payload["duration_hours"]),
            category=payload["category"],
            date=date.fromisoformat(payload["date"]),
            needs_review=bool(payload["needs_review"]),
            review_notes=payload["review_notes"],
        )


@dataclass
class Match:
    name: str
    start: int
    end: int
    config: Dict[str, Any]


@dataclass
class ConfigIndex:
    clients: Dict[str, Dict[str, Any]]
    projects: Dict[str, Dict[str, Any]]
    categories: Dict[str, Dict[str, Any]]
    people: Dict[str, Dict[str, Any]]


def parse_entry(text: str, config: Dict[str, Any]) -> ParsedEntry:
    normalized = normalize_text(text)
    if not normalized:
        raise ParseError("Input text is empty.")

    parsed_date, working_text = extract_date(normalized)
    duration_minutes, working_text = extract_duration(working_text)
    tokens = tokenize(working_text)

    if not tokens:
        raise ParseError("Could not find enough detail after parsing duration and date.")

    index = build_index(config)

    project_match = find_best_match(tokens, index.projects)
    client_match = find_best_match(tokens, index.clients)
    category_match = find_best_match(tokens, index.categories)
    person_match = find_best_match(tokens, index.people)

    removed_indexes: Set[int] = set()
    review_notes: List[str] = []
    needs_review = False

    project = ""
    client = ""
    via = ""
    category = ""

    if project_match:
        project = project_match.name
        removed_indexes.update(range(project_match.start, project_match.end))
        project_defaults = project_match.config
        client = project_defaults.get("client", "")
        via = project_defaults.get("via", "")
        category = project_defaults.get("default_category", "")

    if client_match:
        if not project_match:
            removed_indexes.update(range(client_match.start, client_match.end))
        if client and client != client_match.name:
            needs_review = True
            review_notes.append(
                f"Project default client '{client}' conflicts with explicit client '{client_match.name}'."
            )
        else:
            client = client_match.name

    if category_match:
        category = category_match.name

    if person_match:
        person_defaults = person_match.config
        if not project and person_match.start == 0:
            removed_indexes.update(range(person_match.start, person_match.end))
        if not client and person_defaults.get("client"):
            client = person_defaults["client"]
        if not via and person_defaults.get("via"):
            via = person_defaults["via"]
        if not category and person_defaults.get("default_category"):
            category = person_defaults["default_category"]
        if not project and person_defaults.get("project"):
            project = person_defaults["project"]

    if not project:
        inferred_project, inferred_indexes = infer_unknown_project(
            tokens=tokens,
            category_match=category_match,
            client_match=client_match,
            person_match=person_match,
        )
        if inferred_project:
            project = inferred_project
            removed_indexes.update(inferred_indexes)
            needs_review = True
            review_notes.append(
                f"Inferred unknown project '{project}'. Review before promoting it into config."
            )

    task_tokens = [token for index_value, token in enumerate(tokens) if index_value not in removed_indexes]
    task = clean_task(" ".join(task_tokens))

    if not task and category:
        task = category
    if not task:
        raise ParseError("Could not determine the task description.")

    if not category:
        category = "General"
        needs_review = True
        review_notes.append("No category matched. Defaulted to 'General'.")

    review_notes_text = " ".join(review_notes).strip()

    return ParsedEntry(
        raw_input=text.strip(),
        task=task,
        project=project,
        client=client,
        via=via,
        duration_minutes=duration_minutes,
        duration_hours=round(duration_minutes / 60, 2),
        category=category,
        date=parsed_date,
        needs_review=needs_review,
        review_notes=review_notes_text,
    )


def normalize_text(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(
        r"\b("
        r"jan|january|feb|february|mar|march|apr|april|may|jun|june|"
        r"jul|july|aug|august|sep|sept|september|oct|october|nov|november|"
        r"dec|december"
        r")(\d{1,2}(?:st|nd|rd|th)?)\b",
        r"\1 \2",
        lowered,
    )
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = re.sub(r"(?<=\d)[-\s]*(hours|hour|hrs|hr)\b", "h", lowered)
    lowered = re.sub(r"(?<=\d)[-\s]*(minutes|minute|mins|min)\b", "m", lowered)
    lowered = normalize_spoken_durations(lowered)
    return lowered


def normalize_spoken_durations(text: str) -> str:
    def replacer(match: re.Match[str]) -> str:
        number_words = match.group("number")
        suffix = match.group("unit")
        value = words_to_number(number_words)
        if value is None:
            return match.group(0)
        unit = "h" if suffix.startswith(("hour", "hr")) else "m"
        return f"{value}{unit}"

    pattern = re.compile(
        r"\b(?P<number>(?:half|(?:"
        r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
        r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
        r")(?:[-\s](?:one|two|three|four|five|six|seven|eight|nine))?))"
        r"[-\s]+(?P<unit>hours|hour|hrs|hr|minutes|minute|mins|min)\b"
    )
    return pattern.sub(replacer, text)


def words_to_number(words: str) -> Optional[float]:
    normalized = words.replace("-", " ").strip()
    if normalized == "half":
        return 0.5
    parts = [part for part in normalized.split() if part]
    if not parts:
        return None
    total = 0
    for part in parts:
        if part not in NUMBER_WORDS:
            return None
        total += NUMBER_WORDS[part]
    return float(total)


def extract_date(text: str) -> Tuple[date, str]:
    today = date.today()

    if " yesterday" in f" {text} ":
        cleaned = re.sub(r"\byesterday\b", "", text).strip()
        return today - timedelta(days=1), cleaned

    if " today" in f" {text} ":
        cleaned = re.sub(r"\btoday\b", "", text).strip()
        return today, cleaned

    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        value = match.group(0)
        parsed = parse_date_string(value, today.year)
        cleaned = (text[: match.start()] + " " + text[match.end() :]).strip()
        return parsed, re.sub(r"\s+", " ", cleaned)

    return today, text


def parse_date_string(value: str, default_year: int) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.strptime(value, "%Y-%m-%d").date()

    if re.fullmatch(r"\d{1,2}-\d{1,2}-\d{2,4}", value):
        for fmt in ("%m-%d-%Y", "%m-%d-%y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue

    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", value):
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue

    if re.fullmatch(r"\d{1,2}-\d{1,2}", value):
        return datetime.strptime(f"{value}-{default_year}", "%m-%d-%Y").date()

    if re.fullmatch(r"\d{1,2}/\d{1,2}", value):
        return datetime.strptime(f"{value}/{default_year}", "%m/%d/%Y").date()

    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value)
    cleaned = re.sub(r"\s+", " ", cleaned.replace(",", " ")).strip()
    compact_month = re.fullmatch(r"([a-zA-Z]+)(\d{1,2})", cleaned)
    if compact_month:
        cleaned = f"{compact_month.group(1)} {compact_month.group(2)}"
    for fmt in ("%B %d %Y", "%b %d %Y", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            if "%Y" not in fmt:
                return parsed.replace(year=default_year).date()
            return parsed.date()
        except ValueError:
            continue

    raise ParseError(f"Unsupported date format: {value}")


def extract_duration(text: str) -> Tuple[int, str]:
    patterns = [
        re.compile(r"\b(?P<value>\d+(?:\.\d+)?)\s*h\b"),
        re.compile(r"\b(?P<value>\d+)\s*m\b"),
    ]

    matches = []
    for pattern in patterns:
        matches.extend(pattern.finditer(text))

    if not matches:
        raise ParseError("Could not find a duration like 2h, 1.5h, or 45m.")

    match = sorted(matches, key=lambda item: item.start())[0]
    value = float(match.group("value"))
    unit = text[match.end() - 1]

    minutes = int(round(value * 60)) if unit == "h" else int(round(value))
    rounded_minutes = round_to_nearest_quarter_hour(minutes)
    cleaned = (text[: match.start()] + " " + text[match.end() :]).strip()
    return rounded_minutes, re.sub(r"\s+", " ", cleaned)


def round_to_nearest_quarter_hour(minutes: int) -> int:
    if minutes <= 0:
        raise ParseError("Duration must be greater than zero.")
    return max(15, int(math.floor((minutes + 7.5) / 15) * 15))


def tokenize(text: str) -> List[str]:
    return [token for token in re.split(r"\s+", text.strip()) if token]


def build_index(config: Dict[str, Any]) -> ConfigIndex:
    return ConfigIndex(
        clients=build_alias_lookup(config.get("clients", [])),
        projects=build_alias_lookup(config.get("projects", [])),
        categories=build_alias_lookup(config.get("categories", [])),
        people=build_alias_lookup(config.get("people", [])),
    )


def build_alias_lookup(entries: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        canonical = entry["name"]
        aliases = {normalize_phrase(canonical)}
        aliases.update(normalize_phrase(alias) for alias in entry.get("aliases", []))
        enriched = dict(entry)
        enriched["name"] = canonical
        for alias in aliases:
            lookup[alias] = enriched
    return lookup


def normalize_phrase(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def find_best_match(tokens: Sequence[str], lookup: Dict[str, Dict[str, Any]]) -> Optional[Match]:
    best_match: Optional[Match] = None
    token_count = len(tokens)

    for start in range(token_count):
        for end in range(token_count, start, -1):
            candidate = " ".join(tokens[start:end])
            if candidate not in lookup:
                continue

            current = Match(
                name=lookup[candidate]["name"],
                start=start,
                end=end,
                config=lookup[candidate],
            )
            if not best_match or (end - start) > (best_match.end - best_match.start):
                best_match = current

    return best_match


def infer_unknown_project(
    tokens: Sequence[str],
    category_match: Optional[Match],
    client_match: Optional[Match],
    person_match: Optional[Match],
) -> Tuple[str, Set[int]]:
    if not category_match or category_match.start < 1:
        return "", set()

    candidate_indexes = list(range(category_match.start))
    if client_match and client_match.start == 0:
        candidate_indexes = [
            index_value for index_value in candidate_indexes if index_value >= client_match.end
        ]
    if person_match and person_match.start == 0:
        candidate_indexes = [
            index_value for index_value in candidate_indexes if index_value >= person_match.end
        ]

    candidate_tokens = [tokens[index_value] for index_value in candidate_indexes]
    if len(candidate_tokens) < 1:
        return "", set()

    if any(token in TASK_CONNECTOR_WORDS for token in candidate_tokens):
        return "", set()

    return clean_task(" ".join(candidate_tokens)), set(candidate_indexes)


def clean_task(task: str) -> str:
    task = task.strip(" -,:")
    task = re.sub(r"\s+", " ", task)
    tokens = task.split()
    while tokens and tokens[0] in TASK_CONNECTOR_WORDS:
        tokens.pop(0)
    while tokens and tokens[-1] in TASK_CONNECTOR_WORDS:
        tokens.pop()
    return " ".join(tokens).title()

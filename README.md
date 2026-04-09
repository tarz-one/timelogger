# timelogger

A lightweight FastAPI backend for logging natural-language work entries into Google Sheets from:

- a Mac terminal command
- an iPhone Shortcut
- a simple HTTP webhook

The v1 design is intentionally dumb and reliable: one backend, one parser, one append-to-sheet flow, and one editable JSON config file.

## File layout

- `app.py`: FastAPI app, config loading, API auth, Google Sheets append logic
- `parser.py`: Rule-based text parser with separate client, project, category, and person defaults
- `aliases.json`: Editable JSON config for clients, projects, categories, and people
- `.env.example`: Environment variables for local and deployed use
- `requirements.txt`: Python dependencies

## What it parses

Examples supported:

- `adhoc 2h meeting with mason`
- `3fish 45m website updates`
- `heman design 5hrs`
- `mason call 30m`

Fields produced:

- `task`
- `project`
- `client`
- `via`
- `duration_minutes`
- `duration_hours`
- `category`
- `date`
- `needs_review`
- `review_notes`

V2 additions:

- local offline queue with automatic retry
- auto-promotion of recurring inferred projects into `aliases.json`
- optional macOS launch agent for always-on local logging

Sheet columns written by `POST /log`:

- `Date`
- `Client`
- `Via`
- `Project`
- `Category`
- `Task`
- `Hours`
- `Source`
- `Review`

Rules:

- If no date is given, it defaults to today.
- Durations like `2h`, `1.5h`, and `45m` are supported.
- Durations are rounded to the nearest 15 minutes.
- Explicit project matches win over generic words.
- Category words fill `category`, not `project`.
- Known projects can supply saved defaults for `client`, `via`, and default category.
- Person defaults are fallback behavior only.
- Unknown projects can still be logged and are marked as needing review.
- The architecture leaves room for a future AI fallback, but v1 is rule-based only.

## Data model

- `project`: specific work item, such as `Heman`, `Lost Lands`, or `Angelyne`
- `category`: type of work, such as `Drone Design`, `Operations`, `Meeting`, `Socials`, `Admin`, or `Revisions`
- `client`: top-level client or company
- `via`: partner or intermediary when relevant

This structure supports metrics like:

- total hours by project
- total hours by category
- total hours by project plus category

because those values are stored in separate columns.

## Review column behavior

The `Review` column is intentionally compact for daily use:

- blank when the parser is confident
- `Needs review` when something looks incomplete
- a short note when the parser inferred a new unknown project or found a conflict

If the same inferred unknown project appears `AUTO_PROMOTE_THRESHOLD` times, the app automatically adds it into [`aliases.json`](/Users/tarz/timelogger/aliases.json) as a saved project.

## Config structure

[`aliases.json`](/Users/tarz/timelogger/aliases.json) is now organized into:

- `clients`
- `projects`
- `categories`
- `people`

Starter example:

```json
{
  "clients": [
    {
      "name": "AdHoc",
      "aliases": ["adhoc", "ad hoc", "ad-hoc"]
    }
  ],
  "projects": [
    {
      "name": "Heman",
      "aliases": ["heman"],
      "client": "Heads In the Sky",
      "via": "AdHoc",
      "default_category": "Drone Design"
    }
  ],
  "categories": [
    {
      "name": "Drone Design",
      "aliases": ["design", "drone design", "dronedesign"]
    }
  ],
  "people": [
    {
      "name": "Mason",
      "aliases": ["mason"],
      "client": "AdHoc",
      "default_category": "Meeting"
    }
  ]
}
```

Editing tips:

- Add new reusable clients under `clients`.
- Add known recurring work items under `projects`.
- Add new work types under `categories`.
- Use `people` only for fallback defaults, not as the main source of project matching.

## Parsing behavior

- Explicit known project aliases are matched anywhere in the phrase and take priority.
- Explicit category aliases set the category but stay in the task text when helpful.
- If no known project is found, the parser may infer a new leading multi-word project like `Lost Lands` when the phrase also contains a known category.
- Inferred new projects are logged immediately with `needs_review=true` and a `review_notes` message.
- Unknown projects are not silently added to config. You review them manually and add them to [`aliases.json`](/Users/tarz/timelogger/aliases.json) later if they should become permanent.

## Category mapping

- `design`, `drone design` -> `Drone Design`
- `ops`, `operations`, `setup`, `onsite` -> `Operations`
- `meeting`, `call`, `discussion`, `review` -> `Meeting`
- `socials`, `social`, `posting`, `caption`, `content` -> `Socials`
- `admin`, `invoice`, `email` -> `Admin`
- `revisions`, `revision`, `edit`, `edits` -> `Revisions`

## Local setup

### 1. Create and activate a virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your env file

```bash
cp .env.example .env
```

Then edit `.env` and set:

- `GOOGLE_SERVICE_ACCOUNT_FILE`
- `GOOGLE_SHEET_NAME`
- `GOOGLE_WORKSHEET_NAME`
- `GOOGLE_WORKSHEET_TEMPLATE_NAME` if you want a separate worksheet template source
- `API_BEARER_TOKEN` if you want to protect the webhook
- `QUEUE_POLL_SECONDS` for queued retry frequency
- `AUTO_PROMOTE_THRESHOLD` for recurring project promotion

## Google Sheets setup

### 1. Create a Google Cloud project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project or pick an existing one.
3. Search for `Google Sheets API`.
4. Enable the API for that project.
5. Search for `Google Drive API`.
6. Enable that API too.

### 2. Create a service account

1. In Google Cloud Console, go to `IAM & Admin` -> `Service Accounts`.
2. Click `Create Service Account`.
3. Give it a name like `timelogger`.
4. Finish the service account creation.

### 3. Generate a JSON key

1. Open the new service account.
2. Go to the `Keys` tab.
3. Click `Add Key` -> `Create new key`.
4. Choose `JSON`.
5. Download the JSON file.
6. Place it in this project folder, for example as `service-account.json`.

Then set:

```env
GOOGLE_SERVICE_ACCOUNT_FILE=./service-account.json
```

### 4. Create and share the sheet

1. Create a Google Sheet with the spreadsheet name you want to use.
2. Add one worksheet tab to use as your template, for example `APRIL`.
3. In the first row, add headers like:

```text
Date | Client | Via | Project | Category | Task | Hours | Source | Review
```

4. Click `Share` on the Google Sheet.
5. Copy the service account email from the JSON file's `client_email` value.
6. Share the sheet with that service account email as an editor.

If you skip the sharing step, the API will authenticate successfully but fail to open the sheet.

Monthly worksheet behavior:

- logs are written to a worksheet based on the entry date, such as `APRIL`, `MAY`, or `JUNE`
- if that month tab does not exist yet, the app creates it automatically
- the new month tab copies the header row from your template worksheet
- by default, `GOOGLE_WORKSHEET_NAME` is used as the template worksheet name
- if you want a separate dedicated template tab later, set `GOOGLE_WORKSHEET_TEMPLATE_NAME`

Why Drive API is needed:

- this app opens the spreadsheet by name, not by hard-coded spreadsheet ID
- Google Drive API lets the service account find that spreadsheet before Sheets API writes to it

## Run locally

```bash
uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Parse without writing to Google Sheets:

```bash
curl -X POST http://127.0.0.1:8000/parse \
  -H "Content-Type: application/json" \
  -d '{"text":"adhoc 2h meeting with mason","source":"manual"}'
```

Example log request:

```bash
curl -X POST http://127.0.0.1:8000/log \
  -H "Content-Type: application/json" \
  -d '{"text":"adhoc 2h meeting with mason","source":"manual"}'
```

If you set `API_BEARER_TOKEN`, include:

```bash
-H "Authorization: Bearer YOUR_TOKEN"
```

## Terminal command on macOS

Add this zsh function to your `~/.zshrc`:

```zsh
timelog() {
  emulate -L zsh
  setopt localoptions noxtrace noverbose typesetsilent

  local url="${TIMELOGGER_URL:-http://127.0.0.1:8000/log}"
  local health_url="${url%/log}/health"

  if [[ $# -eq 0 ]]; then
    echo "Usage: timelog \"adhoc 2h meeting with mason\""
    echo "   or: timelog \"heman design 3hrs\" \"socials meeting with mason 4hrs\""
    return 1
  fi

  if [[ "$url" == http://127.0.0.1:8000/log || "$url" == http://localhost:8000/log ]]; then
    if ! curl -fsS "$health_url" >/dev/null 2>&1; then
      launchctl kickstart -k "gui/$UID/com.tarz.timelogger" >/dev/null 2>&1 || true
      sleep 1
    fi
  fi

  local auth_args=()
  if [[ -n "$TIMELOGGER_TOKEN" ]]; then
    auth_args=(-H "Authorization: Bearer $TIMELOGGER_TOKEN")
  fi

  local input
  for input in "$@"; do
    local json
    json=$(python3 -c 'import json, sys; print(json.dumps({"text": sys.argv[1], "source": "terminal"}))' "$input") || return 1

    local body_file
    body_file=$(mktemp) || return 1

    local http_code
    http_code=$(curl -sS -o "$body_file" -w "%{http_code}" -X POST "$url" \
      -H "Content-Type: application/json" \
      "${auth_args[@]}" \
      -d "$json") || {
        rm -f "$body_file"
        return 1
      }

    python3 - <<'PY' "$http_code" "$body_file"
import json
from pathlib import Path
import sys

http_code = int(sys.argv[1])
payload = json.loads(Path(sys.argv[2]).read_text())

if 200 <= http_code < 300 and payload.get("ok"):
    entry = payload["entry"]
    prefix = "OK"
    if payload.get("queued"):
        prefix = "Queued"
    bits = [
        prefix,
        entry["date"],
        f"{entry['duration_hours']}h",
    ]
    if entry.get("project"):
        bits.append(entry["project"])
    elif entry.get("client"):
        bits.append(entry["client"])
    bits.append(f"[{entry['category']}]")
    bits.append(entry["task"])
    print("  ".join(bits))
    if payload.get("queue_size", 0) > 0:
        print(f"Logs unsent: {payload['queue_size']}")
    if entry.get("needs_review"):
        note = entry.get("review_notes") or "Needs review"
        print(f"Review: {note}")
    if payload.get("promotion_message"):
        print(payload["promotion_message"])
else:
    detail = payload.get("detail") or payload
    print(f"Error ({http_code}): {detail}")
PY

    rm -f "$body_file"
  done
}
```

Then reload your shell:

```bash
source ~/.zshrc
```

Optional shell env vars:

```bash
export TIMELOGGER_URL="https://your-app-url/log"
export TIMELOGGER_TOKEN="your-token-if-used"
```

The zsh function only depends on `python3` and `curl`, prints cleaner errors, shows review notes when the parser flags something, accepts multiple quoted entries in one command, and tries to wake the macOS background service automatically when you use the local URL.

## macOS background service

Make the helper scripts executable:

```bash
cd /Users/tarz/timelogger
chmod +x scripts/run_server.sh scripts/install_launch_agent.sh scripts/uninstall_launch_agent.sh
```

Install the launch agent:

```bash
./scripts/install_launch_agent.sh
```

Useful commands:

```bash
launchctl kickstart -k "gui/$UID/com.tarz.timelogger"
launchctl print "gui/$UID/com.tarz.timelogger"
tail -f ~/Library/Logs/timelogger/stdout.log
tail -f ~/Library/Logs/timelogger/stderr.log
```

Remove it later:

```bash
./scripts/uninstall_launch_agent.sh
```

If you want a visual cue for reviewable entries, update the print line to include `needs_review` from the API response.

## iPhone Shortcut setup

Create a Shortcut with these exact actions:

### 1. Dictate Text

- Add the `Dictate Text` action.
- Prompt example: `What did you work on?`
- This captures a phrase like `adhoc 2 hours meeting with mason`.

### 2. Get Contents of URL

- Add `Get Contents of URL`.
- URL: `https://your-public-app-url/log`
- Method: `POST`
- Request Body: `JSON`

Add JSON fields:

- `text` -> `Dictated Text`
- `source` -> `shortcut`

If using auth, add a header:

- Header name: `Authorization`
- Header value: `Bearer YOUR_TOKEN`

Also add this header:

- `Content-Type` -> `application/json`

### 3. Show Result

- Add `Show Result`.
- Set it to display the response body or `message` field.

Recommended Shortcut test phrase:

- `heman design 5 hours`
- `mason call 30m`
- `lost lands design 2 hours`

Good parser smoke tests:

- `heman design 5hrs`
- `angelyne design 2h`
- `mason call 30m`
- `lost lands design 2h`
- `nebula meeting 1.5h`

## Deploying so the Shortcut works anywhere

### Railway

1. Create a new Railway project from this folder or Git repo.
2. Set the same environment variables from `.env.example`.
3. Upload your service account JSON somewhere Railway can access, or store it securely and write it to disk during deploy.
4. Start command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

### Render

1. Create a new `Web Service`.
2. Point it at this project.
3. Build command:

```bash
pip install -r requirements.txt
```

4. Start command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

5. Add the environment variables from `.env.example`.

For either platform, set `TIMELOGGER_URL` in your shell and use that same public URL in the iPhone Shortcut.

## Review workflow for new projects

Example:

- `lost lands design 2h`

The parser can log that entry even if `Lost Lands` is not in config yet. It will:

- set `project` to `Lost Lands`
- set `category` to `Drone Design`
- mark `needs_review` as `true`
- include a note in `review_notes`

If you decide it is a recurring project, add it later to [`aliases.json`](/Users/tarz/timelogger/aliases.json).

## Notes for later

Good future additions:

- optional AI parse fallback when rule parsing fails
- better project inference from more varied sentence patterns
- duplicate entry protection
- request logging and lightweight audit trail

## Exact local run steps on macOS

1. Open Terminal and move into the project:

```bash
cd /Users/tarz/timelogger
```

2. Create a virtual environment:

```bash
python3 -m venv .venv
```

3. Activate it:

```bash
source .venv/bin/activate
```

4. Install the Python packages:

```bash
pip install -r requirements.txt
```

5. Create your local env file:

```bash
cp .env.example .env
```

6. Put your Google service account JSON in this folder, for example:

```text
/Users/tarz/timelogger/service-account.json
```

7. Edit `.env` so it matches your sheet setup. A typical local file looks like:

```env
GOOGLE_SERVICE_ACCOUNT_FILE=./service-account.json
GOOGLE_SHEET_NAME=Time Logs
GOOGLE_WORKSHEET_NAME=Entries
API_BEARER_TOKEN=
HOST=0.0.0.0
PORT=8000
```

8. Start the server:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

9. In a second Terminal tab, test that the server is up:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

10. Test parsing only first, so you can confirm the parser before writing to Google Sheets:

```bash
curl -X POST http://127.0.0.1:8000/parse \
  -H "Content-Type: application/json" \
  -d '{"text":"3fish 45m website updates","source":"manual"}'
```

11. Then send a real log entry:

```bash
curl -X POST http://127.0.0.1:8000/log \
  -H "Content-Type: application/json" \
  -d '{"text":"adhoc 2h meeting with mason","source":"manual"}'
```

12. Open your Google Sheet and confirm a new row was appended.

13. If that works, add the `timelog` zsh function from above to `~/.zshrc`, then reload your shell:

```bash
source ~/.zshrc
```

14. Use it from Terminal:

```bash
timelog "heman design 5hrs"
```

If you want your iPhone Shortcut to work outside your home network, deploy the same app to Railway or Render and replace the local URL with your public URL.

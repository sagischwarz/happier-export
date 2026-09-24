# happier-export

Export your full meditation history from [Ten Percent Happier](https://www.tenpercent.com/) to JSON and CSV.

## What you get

Each session includes:

| Field | Example |
|---|---|
| `date` | `2026-09-24T10:33:14.500Z` |
| `date_end` | `2026-09-24T10:53:34.107Z` |
| `title` | `Meditation` |
| `source` | `Plum Village` |
| `duration_minutes` | `20.3` |
| `type` | `meditation` |
| `uuid` | `7df2002b-fb52-…` |

Output files:

- `meditation_history.json` — structured JSON with all sessions
- `meditation_history.csv` — flat CSV, easy to open in Excel/Google Sheets

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A Ten Percent Happier account (email + password)

## Usage

```bash
uv run happier-export
```

On first run (or when the session has expired) the script will prompt for your email and password. The session cookie is saved to `~/.happier_session_cookies` so you don't need to log in every time.

### Options

| Flag | Description |
|---|---|
| `--email EMAIL` | Account email (prompted if omitted) |
| `--password PASSWORD` | Account password (prompted if omitted) |
| `-o`, `--output PREFIX` | Output filename prefix (default: `meditation_history`) |
| `--min-duration MIN` | Minimum session duration in minutes (default: `1.0`, use `0` to include all) |
| `--base-url URL` | Override the server URL |
| `--logout` | Clear saved session and exit |
| `-h`, `--help` | Show help and exit |

### Examples

```bash
# Include very short sessions too
uv run happier-export --min-duration 0

# Custom output filename prefix
uv run happier-export -o my_meditation_data

# Pass credentials directly (interactive prompt is safer for passwords)
uv run happier-export --email you@example.com --password 'yourpassword'

# Clear saved session
uv run happier-export --logout
```

## How it works

Ten Percent Happier is a [Hotwire/Turbo](https://hotwired.dev/) web app — there is no public JSON API. This tool:

1. Logs in via the standard web form at `https://my.meditatehappier.com/v2/sign_in`
2. Fetches the history page at `/v2/history` and parses session data from the HTML
3. Follows the cursor-based infinite-scroll pagination until all sessions are collected
4. Writes the results to JSON and CSV

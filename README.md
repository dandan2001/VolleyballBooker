# VolleyballBooker

Books volleyball courts on ActiveCarrot (site 487, group 1848) from the terminal.

## Setup

1. `cp .env.example .env` and fill in real details (accounts + card).
2. Requires Google Chrome (`/usr/bin/google-chrome`); Selenium Manager fetches a matching chromedriver automatically.

## Usage

```bash
uv run main.py --date 2026-07-10 --time 6:00pm --duration 2
```

- `--duration 1..4` hours. Durations over 2h are split into two bookings
  (primary + backup account) because the site caps a booking at 2 hours.
- `--dry-run` shows the plan without booking.
- `--yes` skips the final confirmation prompt.

Run from the repo root — failures/ and receipts/ paths are relative to the working directory.

Failures save a screenshot + HTML to `failures/`; successful receipts to `receipts/`.

## Known limitation

First checkout per email creates an ActiveCarrot account. Later bookings with the
same email hit a login redirect the tool does not implement yet
(`LoginCheckoutFlow` stub). Capture the logged-in payment page HTML and extend.

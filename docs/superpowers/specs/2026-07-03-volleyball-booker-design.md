# Volleyball Court Booking Tool — Design

Date: 2026-07-03
Status: Approved

## Purpose

CLI tool that books a volleyball court on ActiveCarrot (site 487, group 1848 — council courts, NSW) for a given date, start time, and duration (1–4 hours). Finds the first court with the requested window free and completes checkout with details from `.env`. Durations over 2 hours require two consecutive bookings (site caps a single booking at 2 hours), made with a primary and a backup account.

## CLI

```
uv run main.py --date YYYY-MM-DD --time H:MMam/pm --duration N [--dry-run] [--yes]
```

- `--duration`: integer 1–4 (hours)
- `--dry-run`: scan and print the booking plan; make no bookings
- `--yes`: skip the final "Proceed?" confirmation
- Input formats are trusted (per requirements); only basic parse-or-exit handling
- Bookings can only be made up to ~1 week ahead (site rule)

## Site Model (reverse-engineered, verified live 2026-07-03)

- Browse page `GET /public/facility/browse/487/1848/{date}` embeds iframe
  `GET /public/facility/iframe/487/1848/{date}` containing, per court (10 courts),
  a fullCalendar config with `facilityId`, `facilityName`, and "Unavailable" event
  blocks. Free time = gaps between Unavailable blocks (day window 05:00–23:00).
- Duration options: `POST /public/facility/duration_dropdown_ajax/487` with
  `facility_id`, `datetime` → JSON array of allowed minutes (e.g. `[60,120]`).
- Booking: `POST /public/facility/book_ajax/487/` with `site_facility_id`,
  `event_duration` (minutes), `event_from_date` (YYYY-MM-DD), `event_from_hour`,
  `event_from_min` → `{error: false}` and pending booking held in session cookie;
  then browser navigates to `/public/facility/payment/487`.
- Payment page (captured in `1.html` / `Sports Halls_ Online Services.html`):
  guest form. Fields: `first_name`, `last_name`, `email` → blur triggers
  `POST /public/facility/check_user_ajax/487/`:
  - `has_person: true` → site redirects to `/login?site=487` (account exists)
  - otherwise reveals `.person_extra`: `password`, `gender`, `dob_day/month/year`,
    `street`, `suburb`, `state`, `postcode`, `home_phone`, `mobile_phone`,
    `work_phone`, terms checkbox, card fields (`name_on_card`,
    `credit_card_number`, `expiry_month`, `expiry_year`, `cvv_number`)
- Submit ("Confirm and pay") is gated by invisible reCAPTCHA v3
  (sitekey `6LexcHsmAAAAALuHLbzf33yBmGev3hHnIqUmlk6L`) and a synchronous
  `POST /public/facility/validate_ajax/487/` field validation.
- First successful guest checkout creates an account for that email; subsequent
  bookings with the same email will hit the login redirect.
- No cancellations or refunds after confirmation (site terms).

## Architecture — Approach A (hybrid)

Requests for availability scanning (fast, reliable); Selenium headless Chromium
for checkout (reCAPTCHA v3 needs a real browser). Chromium + chromedriver v149
already installed via snap.

### Files

- `main.py` — CLI parsing, orchestration, split-booking prompts, final output
- `booker.py` — `AvailabilityScanner`, `CheckoutSession`, checkout flows, data classes
- `.env` — account + card details (gitignored); `.env.example` committed

### .env contents

Primary account: first/last name, email, password, gender, DOB, street, suburb,
state, postcode, phone. Backup account: same field set. Shared card:
name on card, number, expiry month/year, CVV.

### AvailabilityScanner (requests)

1. GET iframe URL for date; regex-parse per-court facility id, name, Unavailable blocks
2. Compute free windows per court; select courts where requested window fully free
3. Court order 1→10; first match wins
4. For durations >2h, plan two segments: 2h + remainder
5. Before checkout, verify each segment via `duration_dropdown_ajax` (server
   confirms the duration is bookable at that start time)

### CheckoutSession (Selenium)

One session per booking segment:

1. Load browse page (establishes session cookies and captcha context)
2. Execute `book_ajax` POST via in-browser JS (same call the site's own JS makes);
   assert `error == false`, else fail with server-provided error strings
3. Navigate to payment page; **verify summary text matches requested court, date,
   time, and duration — abort on mismatch** (wrong-booking guard)
4. **Flow split point** — detect page state and dispatch to a `CheckoutFlow`
   implementation (`complete(details) -> BookingResult`):
   - `GuestCheckoutFlow` (built now): fill name/email, trigger blur →
     `check_user_ajax`, wait for `.person_extra`; fill personal fields, tick terms,
     fill card; click "Confirm and pay" (reCAPTCHA v3 executes natively); wait for
     confirmation page; parse and return booking details
   - `LoginCheckoutFlow` (stub): raised when login redirect or `has_person`
     detected — `NotImplementedError("account exists — login flow not yet built")`.
     To be implemented once a logged-in payment page capture exists.
5. On any failure: capture page screenshot + error text to `./failures/`, return
   failed `BookingResult` with stage and reason

### Split-booking orchestration (>2h)

- Same court has full window free → segment 1 on primary account, segment 2 on
  backup account. Both browser sessions pre-warmed in parallel to payment-ready
  state; submits fired back-to-back.
- No single court has the full window → prompt `Split across courts? [y/N]`
  - yes → cross-court plan (segment courts may differ)
  - no → prompt `Book first 2h only? [y/N]` → yes: single booking; no: abort
- Segment 1 succeeds but segment 2 fails → report **partial success** loudly
  (no refunds possible): print booked segment details + failure stage/reason

### Anti-detection (modest — small vendor)

- Chromium `headless=new`, standard user agent
- `excludeSwitches=["enable-automation"]`, CDP patch for `navigator.webdriver`
- Human-ish jitter (0.5–1.5 s) between field fills
- Requests session mirrors the browser user agent
- No heavy stealth stack; reCAPTCHA v3 in a real browser is the main defense

### Verification & output

Explicit expected-state check at every step; fail fast with stage + reason.
Final output per segment: `SUCCESS` (court, date, time, duration, amount,
account used) or `FAILED` (stage, reason). Unless `--yes`, print the plan and
prompt `Proceed? [y/N]` before any booking is made.

## Out of scope

- Test suite (user decision — small project, site unlikely to change)
- Login checkout flow implementation (stubbed until page capture available)
- Cancellation handling (site offers none)

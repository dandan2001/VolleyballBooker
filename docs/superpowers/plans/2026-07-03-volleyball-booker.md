# Volleyball Booker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLI tool that books ActiveCarrot volleyball courts (site 487, group 1848) for a date/time/duration, splitting >2h bookings across primary/backup accounts.

**Architecture:** Hybrid — `requests` scans the availability iframe HTML; Selenium headless Chromium performs checkout (booking AJAX fired in-browser, guest payment form, native reCAPTCHA v3). `main.py` = CLI + orchestration, `booker.py` = scanner + checkout classes.

**Tech Stack:** Python 3.13, uv, requests, selenium, python-dotenv. Snap Chromium `/snap/bin/chromium` + chromedriver `/snap/bin/chromium.chromedriver` (v149, already installed).

## Global Constraints

- No test suite (user decision). Each task verifies via a live command instead.
- Only dependencies: `requests`, `selenium`, `python-dotenv`.
- Site constants: SITE_ID=487, GROUP_ID=1848, base `https://secure.activecarrot.com`.
- Court day window: 05:00–23:00. Single booking max 120 minutes.
- Guest checkout only; login flow is a stub raising `NotImplementedError` (spec: built later after page capture).
- Verification commands may hit the live site read-only (GET iframe, duration AJAX). NEVER run `book_ajax` or payment submit during verification — creates real pending bookings.
- All user-facing behavior per spec `docs/superpowers/specs/2026-07-03-volleyball-booker-design.md`.

---

### Task 1: Project setup

**Files:**
- Modify: `pyproject.toml` (via `uv add`)
- Create: `.gitignore`, `.env.example`

**Interfaces:**
- Produces: `.env` variable names consumed by `Account.from_env` / `Card.from_env` in Task 4.

- [ ] **Step 1: Add dependencies**

Run: `uv add requests selenium python-dotenv`
Expected: resolves and writes `pyproject.toml` + `uv.lock`, exit 0.

- [ ] **Step 2: Create `.gitignore`**

```gitignore
.venv/
__pycache__/
.env
failures/
receipts/
1.html
1_files/
Sports Halls_ Online Services.html
Sports Halls_ Online Services_files/
```

- [ ] **Step 3: Create `.env.example`**

```bash
# Primary account (used for first/only booking segment)
PRIMARY_FIRST_NAME=John
PRIMARY_LAST_NAME=Smith
PRIMARY_EMAIL=john@example.com
PRIMARY_PASSWORD=ChooseAPassword1
PRIMARY_GENDER=m
PRIMARY_DOB_DAY=1
PRIMARY_DOB_MONTH=1
PRIMARY_DOB_YEAR=1990
PRIMARY_STREET=1 Example St
PRIMARY_SUBURB=Sydney
PRIMARY_STATE=NSW
PRIMARY_POSTCODE=2000
PRIMARY_PHONE=0400 000 000

# Backup account (used for second segment of >2h bookings)
BACKUP_FIRST_NAME=Jane
BACKUP_LAST_NAME=Smith
BACKUP_EMAIL=jane@example.com
BACKUP_PASSWORD=ChooseAPassword2
BACKUP_GENDER=f
BACKUP_DOB_DAY=1
BACKUP_DOB_MONTH=1
BACKUP_DOB_YEAR=1990
BACKUP_STREET=1 Example St
BACKUP_SUBURB=Sydney
BACKUP_STATE=NSW
BACKUP_POSTCODE=2000
BACKUP_PHONE=0400 000 001

# Card (shared by both accounts)
CARD_NAME=John Smith
CARD_NUMBER=4111111111111111
CARD_EXPIRY_MONTH=01
CARD_EXPIRY_YEAR=2030
CARD_CVV=123
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock .gitignore .env.example .python-version README.md main.py
git commit -m "chore: project setup, deps, env template"
```

---

### Task 2: Availability parsing (`booker.py` core)

**Files:**
- Create: `booker.py`

**Interfaces:**
- Produces:
  - `parse_time(s: str) -> int` (minutes since midnight; accepts `6:00pm`)
  - `fmt_time(mins: int, space: bool = False) -> str` (`6:00pm` / `6:00 pm`)
  - `split_segments(hours: int) -> list[int]` (`3 -> [120, 60]`)
  - `@dataclass Court(facility_id: int, name: str, busy: list[tuple[int, int]])` with `free(start: int, end: int) -> bool`
  - `parse_iframe(html: str, date: datetime.date) -> list[Court]`
  - `class BookingError(Exception)` with `.stage: str`, `.reason: str`
  - Constants: `SITE_ID`, `GROUP_ID`, `BASE_URL`, `BROWSE_URL`, `IFRAME_URL`, `DURATION_URL`, `BOOK_AJAX_PATH`, `PAYMENT_URL`, `OPEN_MIN`, `CLOSE_MIN`, `USER_AGENT`, `CHROMIUM_BINARY`, `CHROMEDRIVER`

- [ ] **Step 1: Create `booker.py` with constants, errors, time utils, parsing**

```python
"""ActiveCarrot volleyball court booking: availability scan + Selenium checkout."""
from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

SITE_ID = 487
GROUP_ID = 1848
BASE_URL = "https://secure.activecarrot.com"
BROWSE_URL = f"{BASE_URL}/public/facility/browse/{SITE_ID}/{GROUP_ID}/{{date}}"
IFRAME_URL = f"{BASE_URL}/public/facility/iframe/{SITE_ID}/{GROUP_ID}/{{date}}"
DURATION_URL = f"{BASE_URL}/public/facility/duration_dropdown_ajax/{SITE_ID}"
BOOK_AJAX_PATH = f"/public/facility/book_ajax/{SITE_ID}/"
PAYMENT_URL = f"{BASE_URL}/public/facility/payment/{SITE_ID}"

OPEN_MIN = 5 * 60
CLOSE_MIN = 23 * 60

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
CHROMIUM_BINARY = "/snap/bin/chromium"
CHROMEDRIVER = "/snap/bin/chromium.chromedriver"


class BookingError(Exception):
    def __init__(self, stage: str, reason: str):
        self.stage = stage
        self.reason = reason
        super().__init__(f"[{stage}] {reason}")


def parse_time(s: str) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(am|pm)", s.strip().lower())
    if not m:
        raise BookingError("input", f"bad time {s!r}, expected H:MMam/pm")
    h, mi, ap = int(m[1]), int(m[2]), m[3]
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + mi


def fmt_time(mins: int, space: bool = False) -> str:
    h, mi = divmod(mins, 60)
    ap = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{mi:02d}{' ' if space else ''}{ap}"


def split_segments(hours: int) -> list[int]:
    total = hours * 60
    return [total] if total <= 120 else [120, total - 120]


@dataclass
class Court:
    facility_id: int
    name: str
    busy: list[tuple[int, int]]

    def free(self, start: int, end: int) -> bool:
        if start < OPEN_MIN or end > CLOSE_MIN or start >= end:
            return False
        return all(not (bs < end and be > start) for bs, be in self.busy)


_CAL_RE = re.compile(r"facilityId = (\d+);\s*var facilityName = '([^']+)';")
_EVT_RE = re.compile(
    r"start: new Date\((\d+), (\d+), (\d+), (\d+), (\d+)\),\s*"
    r"end: new Date\((\d+), (\d+), (\d+), (\d+), (\d+)\)"
)


def parse_iframe(html: str, date: date_type) -> list[Court]:
    matches = list(_CAL_RE.finditer(html))
    if not matches:
        raise BookingError("scan", "no courts found in iframe HTML - structure changed?")
    courts = []
    for i, m in enumerate(matches):
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        chunk = html[m.end():end_pos]
        busy = []
        for e in _EVT_RE.finditer(chunk):
            sy, sm, sd, sh, smin, ey, em, ed, eh, emin = map(int, e.groups())
            if (sy, sm + 1, sd) != (date.year, date.month, date.day):
                raise BookingError(
                    "scan", f"event date {sy}-{sm + 1}-{sd} != requested {date} - aborting"
                )
            end = CLOSE_MIN if (eh, emin) == (23, 59) else eh * 60 + emin
            busy.append((sh * 60 + smin, end))
        name = re.sub(r"\s+", " ", m[2]).strip()
        courts.append(Court(int(m[1]), name, busy))
    return courts
```

- [ ] **Step 2: Verify against live site**

Run:
```bash
uv run python -c "
import datetime, requests, booker
d = datetime.date.today() + datetime.timedelta(days=3)
html = requests.get(booker.IFRAME_URL.format(date=d.isoformat()), headers={'User-Agent': booker.USER_AGENT}).text
courts = booker.parse_iframe(html, d)
print(len(courts), 'courts')
for c in courts:
    print(c.facility_id, c.name, c.busy)
print('16:00-17:00 free on:', [c.name for c in courts if c.free(16*60, 17*60)])
"
```
Expected: `10 courts`, each with facility id (11482, 11567, 11568, 11637, 11638, 11639, 11484, 11397, 11396, 11398), name `Volleyball Crt N` (single spaces), busy tuples like `(300, 960)` style ranges, and a plausible free-court list.

- [ ] **Step 3: Commit**

```bash
git add booker.py
git commit -m "feat: iframe availability parsing and time utils"
```

---

### Task 3: AvailabilityScanner + booking plan search (`booker.py`)

**Files:**
- Modify: `booker.py` (append)

**Interfaces:**
- Consumes: `Court`, `parse_iframe`, `split_segments`, constants (Task 2)
- Produces:
  - `@dataclass PlannedSegment(court: Court, date_str: str, start_min: int, minutes: int, account_prefix: str)`
  - `class AvailabilityScanner` with:
    - `fetch_courts(date_str: str) -> list[Court]`
    - `duration_allowed(facility_id: int, date_str: str, start_min: int, minutes: int) -> bool`
    - `plan_same_court(courts, date_str, start_min, seg_minutes) -> list[PlannedSegment] | None`
    - `plan_cross_court(courts, date_str, start_min, seg_minutes) -> list[PlannedSegment] | None`
    - `verify_plan(plan: list[PlannedSegment]) -> None` (raises `BookingError`)

- [ ] **Step 1: Append scanner code to `booker.py`**

```python
@dataclass
class PlannedSegment:
    court: Court
    date_str: str
    start_min: int
    minutes: int
    account_prefix: str

    def describe(self) -> str:
        return (
            f"{self.court.name}: {fmt_time(self.start_min)} for {self.minutes} min "
            f"on {self.date_str} (account: {self.account_prefix})"
        )


_ACCOUNT_ORDER = ["PRIMARY", "BACKUP"]


class AvailabilityScanner:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def fetch_courts(self, date_str: str) -> list[Court]:
        r = self.session.get(IFRAME_URL.format(date=date_str), timeout=30)
        if r.status_code != 200:
            raise BookingError("scan", f"iframe fetch returned HTTP {r.status_code}")
        return parse_iframe(r.text, datetime.strptime(date_str, "%Y-%m-%d").date())

    def duration_allowed(
        self, facility_id: int, date_str: str, start_min: int, minutes: int
    ) -> bool:
        h, mi = divmod(start_min, 60)
        r = self.session.post(
            DURATION_URL,
            data={"facility_id": facility_id, "datetime": f"{date_str} {h:02d}:{mi:02d}:00"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        if r.status_code != 200:
            raise BookingError("scan", f"duration check returned HTTP {r.status_code}")
        allowed = r.json()
        if not isinstance(allowed, list):
            return False
        return minutes in [abs(int(x)) for x in allowed]

    def plan_same_court(self, courts, date_str, start_min, seg_minutes):
        total = sum(seg_minutes)
        for court in courts:
            if court.free(start_min, start_min + total):
                plan, offset = [], 0
                for i, minutes in enumerate(seg_minutes):
                    plan.append(
                        PlannedSegment(
                            court, date_str, start_min + offset, minutes, _ACCOUNT_ORDER[i]
                        )
                    )
                    offset += minutes
                return plan
        return None

    def plan_cross_court(self, courts, date_str, start_min, seg_minutes):
        plan, offset = [], 0
        for i, minutes in enumerate(seg_minutes):
            seg_start = start_min + offset
            court = next(
                (c for c in courts if c.free(seg_start, seg_start + minutes)), None
            )
            if court is None:
                return None
            plan.append(PlannedSegment(court, date_str, seg_start, minutes, _ACCOUNT_ORDER[i]))
            offset += minutes
        return plan

    def verify_plan(self, plan: list[PlannedSegment]) -> None:
        for seg in plan:
            if not self.duration_allowed(
                seg.court.facility_id, seg.date_str, seg.start_min, seg.minutes
            ):
                raise BookingError(
                    "verify",
                    f"server rejects {seg.minutes} min at {fmt_time(seg.start_min)} "
                    f"on {seg.court.name}",
                )
```

- [ ] **Step 2: Verify against live site**

Run:
```bash
uv run python -c "
import datetime, booker
d = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
s = booker.AvailabilityScanner()
courts = s.fetch_courts(d)
plan = s.plan_same_court(courts, d, 16*60, booker.split_segments(1))
print('plan:', [p.describe() for p in plan] if plan else None)
if plan:
    s.verify_plan(plan)
    print('server verified OK')
"
```
Expected: one-segment plan on first free court at 4:00pm (or `None` if that day fully booked — then retry with a different start time visible in Task 2 output), and `server verified OK`.

- [ ] **Step 3: Commit**

```bash
git add booker.py
git commit -m "feat: availability scanner and booking plan search"
```

---

### Task 4: Selenium checkout (`booker.py`)

**Files:**
- Modify: `booker.py` (append; add selenium imports below existing imports)

**Interfaces:**
- Consumes: `PlannedSegment`, `BookingError`, `fmt_time`, constants (Tasks 2-3)
- Produces:
  - `@dataclass Account(...)` with `Account.from_env(prefix: str) -> Account`
  - `@dataclass Card(...)` with `Card.from_env() -> Card`
  - `@dataclass BookingResult(success: bool, stage: str, detail: str, segment: PlannedSegment | None, account_email: str, amount: str, artifact: str)`
  - `class CheckoutSession(account: Account, card: Card, label: str)` with `start()`, `prepare(seg: PlannedSegment)`, `submit() -> BookingResult`, `close()`

- [ ] **Step 1: Add selenium imports to the import block of `booker.py`**

```python
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
```

- [ ] **Step 2: Append account/card/result dataclasses**

```python
def _env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise BookingError("config", f"missing {key} in .env")
    return v


@dataclass
class Account:
    first_name: str
    last_name: str
    email: str
    password: str
    gender: str
    dob_day: str
    dob_month: str
    dob_year: str
    street: str
    suburb: str
    state: str
    postcode: str
    phone: str

    @classmethod
    def from_env(cls, prefix: str) -> "Account":
        return cls(
            first_name=_env(f"{prefix}_FIRST_NAME"),
            last_name=_env(f"{prefix}_LAST_NAME"),
            email=_env(f"{prefix}_EMAIL"),
            password=_env(f"{prefix}_PASSWORD"),
            gender=_env(f"{prefix}_GENDER"),
            dob_day=_env(f"{prefix}_DOB_DAY"),
            dob_month=_env(f"{prefix}_DOB_MONTH"),
            dob_year=_env(f"{prefix}_DOB_YEAR"),
            street=_env(f"{prefix}_STREET"),
            suburb=_env(f"{prefix}_SUBURB"),
            state=_env(f"{prefix}_STATE"),
            postcode=_env(f"{prefix}_POSTCODE"),
            phone=_env(f"{prefix}_PHONE"),
        )


@dataclass
class Card:
    name: str
    number: str
    expiry_month: str
    expiry_year: str
    cvv: str

    @classmethod
    def from_env(cls) -> "Card":
        return cls(
            name=_env("CARD_NAME"),
            number=_env("CARD_NUMBER"),
            expiry_month=_env("CARD_EXPIRY_MONTH"),
            expiry_year=_env("CARD_EXPIRY_YEAR"),
            cvv=_env("CARD_CVV"),
        )


@dataclass
class BookingResult:
    success: bool
    stage: str
    detail: str
    segment: PlannedSegment | None
    account_email: str = ""
    amount: str = ""
    artifact: str = ""
```

- [ ] **Step 3: Append `CheckoutSession`**

```python
_BOOK_JS = (
    "const body = arguments[0];"
    "const done = arguments[arguments.length - 1];"
    f"fetch('{BOOK_AJAX_PATH}', {{"
    "  method: 'POST',"
    "  headers: {'Content-Type': 'application/x-www-form-urlencoded',"
    "             'X-Requested-With': 'XMLHttpRequest'},"
    "  body: body, credentials: 'same-origin'"
    "}).then(r => r.json()).then(done)"
    ".catch(e => done({error: true, errors: {exception: String(e)}}));"
)


def _jitter():
    time.sleep(random.uniform(0.5, 1.5))


class LoginCheckoutFlow:
    """Placeholder: account already exists on ActiveCarrot for this email."""

    def complete(self):
        raise NotImplementedError(
            "account exists - login flow not yet built; capture the logged-in "
            "payment page HTML and extend the tool"
        )


class CheckoutSession:
    def __init__(self, account: Account, card: Card, label: str):
        self.account = account
        self.card = card
        self.label = label
        self.driver = None
        self.segment: PlannedSegment | None = None

    def start(self):
        opts = webdriver.ChromeOptions()
        opts.binary_location = CHROMIUM_BINARY
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1366,900")
        opts.add_argument(f"--user-agent={USER_AGENT}")
        profile = Path.home() / ".cache" / "vbbooker" / self.label
        profile.mkdir(parents=True, exist_ok=True)
        opts.add_argument(f"--user-data-dir={profile}")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.set_capability("unhandledPromptBehavior", "accept")
        self.driver = webdriver.Chrome(service=Service(CHROMEDRIVER), options=opts)
        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
        )
        self.driver.set_page_load_timeout(60)

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def _capture(self, stage: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        Path("failures").mkdir(exist_ok=True)
        base = f"failures/{ts}_{self.label}_{stage}"
        try:
            self.driver.save_screenshot(base + ".png")
            Path(base + ".html").write_text(self.driver.page_source)
        except Exception:
            pass
        return base

    def _fill(self, el_id: str, value: str):
        el = self.driver.find_element(By.ID, el_id)
        el.clear()
        el.send_keys(value)
        _jitter()

    def _select(self, el_id: str, value: str):
        Select(self.driver.find_element(By.ID, el_id)).select_by_value(value)
        _jitter()

    def prepare(self, seg: PlannedSegment):
        """Create pending booking + fill payment form. Does NOT pay."""
        self.segment = seg
        d = self.driver

        d.get(BROWSE_URL.format(date=seg.date_str))
        WebDriverWait(d, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        _jitter()

        h, mi = divmod(seg.start_min, 60)
        body = urlencode(
            {
                "site_facility_id": seg.court.facility_id,
                "event_duration": seg.minutes,
                "event_from_date": seg.date_str,
                "event_from_hour": h,
                "event_from_min": f"{mi:02d}",
            }
        )
        d.set_script_timeout(30)
        result = d.execute_async_script(_BOOK_JS, body)
        if not result or result.get("error") is not False:
            errs = "; ".join(str(v) for v in (result or {}).get("errors", {}).values())
            raise BookingError("book_ajax", errs or f"unexpected response: {result!r}")

        d.get(PAYMENT_URL)
        if "/login" in d.current_url:
            LoginCheckoutFlow().complete()

        page = re.sub(r"\s+", " ", d.find_element(By.TAG_NAME, "body").text)
        dt = datetime.strptime(seg.date_str, "%Y-%m-%d")
        expected_start = f"{fmt_time(seg.start_min, space=True)} {dt:%A %d %B}"
        expected_dur = f"for {seg.minutes} Minutes"
        for needle in (seg.court.name, expected_start, expected_dur):
            if needle not in page:
                art = self._capture("summary_mismatch")
                raise BookingError(
                    "verify_summary",
                    f"payment page missing {needle!r} - wrong booking? ({art}.html)",
                )

        a = self.account
        self._fill("first_name", a.first_name)
        self._fill("last_name", a.last_name)
        self._fill("email", a.email)
        d.execute_script("$('#email').change();")

        def guest_or_login(drv):
            if "/login" in drv.current_url:
                return "login"
            extra = drv.find_elements(By.CSS_SELECTOR, ".person_extra")
            return "guest" if extra and extra[0].is_displayed() else False

        try:
            mode = WebDriverWait(d, 20).until(guest_or_login)
        except TimeoutException:
            art = self._capture("check_user_timeout")
            raise BookingError(
                "check_user", f"no response after email entry ({art}.html)"
            )
        if mode == "login":
            LoginCheckoutFlow().complete()

        pw = d.find_element(By.ID, "password")
        if pw.is_displayed():
            self._fill("password", a.password)
        self._select("gender", a.gender)
        self._select("dob_day", a.dob_day)
        self._select("dob_month", a.dob_month)
        self._select("dob_year", a.dob_year)
        self._fill("street", a.street)
        self._fill("suburb", a.suburb)
        self._select("state", a.state)
        self._fill("postcode", a.postcode)
        self._fill("home_phone", a.phone)

        terms = d.find_element(By.ID, "terms")
        if not terms.is_selected():
            terms.click()
        _jitter()

        c = self.card
        self._fill("name_on_card", c.name)
        self._fill("credit_card_number", c.number)
        self._select("expiry_month", c.expiry_month)
        self._select("expiry_year", c.expiry_year)
        self._fill("cvv_number", c.cvv)

    def submit(self) -> BookingResult:
        """Click Confirm and pay. Real money moves here."""
        d = self.driver
        seg = self.segment
        amount = ""
        els = d.find_elements(By.CSS_SELECTOR, ".booking_total")
        if els:
            amount = els[0].text.strip()

        form = d.find_element(By.ID, "personal_trainer_payment_form")
        d.find_element(By.ID, "confirm_submit").click()

        try:
            WebDriverWait(d, 90).until(EC.staleness_of(form))
        except TimeoutException:
            errors = [
                e.text.strip()
                for e in d.find_elements(By.CSS_SELECTOR, "span.error")
                if e.text.strip() and e.text.strip() != "\xa0"
            ]
            art = self._capture("validate")
            return BookingResult(
                False, "validate",
                "; ".join(errors) or f"no navigation after submit ({art}.html)",
                seg, self.account.email, amount, art,
            )

        WebDriverWait(d, 30).until(
            lambda drv: drv.execute_script("return document.readyState") == "complete"
        )
        page = d.page_source
        text = re.sub(r"\s+", " ", d.find_element(By.TAG_NAME, "body").text)
        if 'id="personal_trainer_payment_form"' in page:
            errors = [
                e.text.strip()
                for e in d.find_elements(By.CSS_SELECTOR, "span.error")
                if e.text.strip() and e.text.strip() != "\xa0"
            ]
            art = self._capture("payment_rejected")
            return BookingResult(
                False, "payment",
                "; ".join(errors) or f"payment page re-shown ({art}.html)",
                seg, self.account.email, amount, art,
            )

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        Path("receipts").mkdir(exist_ok=True)
        receipt = f"receipts/{ts}_{self.label}.html"
        Path(receipt).write_text(page)
        return BookingResult(
            True, "done", text[:600], seg, self.account.email, amount, receipt
        )
```

- [ ] **Step 4: Verify driver boots and browse page loads (read-only — no book_ajax)**

Run:
```bash
uv run python -c "
import datetime, booker

class FakeAcct: pass
s = booker.CheckoutSession.__new__(booker.CheckoutSession)
s.label = 'smoke'
booker.CheckoutSession.start(s)
d = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
s.driver.get(booker.BROWSE_URL.format(date=d))
print('title:', s.driver.title)
print('webdriver flag:', s.driver.execute_script('return navigator.webdriver'))
booker.CheckoutSession.close(s)
"
```
Expected: `title: Sports Halls:  Online Services` (or similar site title), `webdriver flag: None`.

- [ ] **Step 5: Commit**

```bash
git add booker.py
git commit -m "feat: selenium checkout session with guest flow and login stub"
```

---

### Task 5: CLI + orchestration (`main.py`)

**Files:**
- Modify: `main.py` (replace contents)

**Interfaces:**
- Consumes: everything produced by Tasks 2-4 (exact names above)

- [ ] **Step 1: Replace `main.py`**

```python
"""CLI: book a volleyball court. uv run main.py --date YYYY-MM-DD --time H:MMam/pm --duration N"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from dotenv import load_dotenv

from booker import (
    Account,
    AvailabilityScanner,
    BookingError,
    Card,
    CheckoutSession,
    fmt_time,
    parse_time,
    split_segments,
)


def yesno(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower().startswith("y")


def parse_args():
    p = argparse.ArgumentParser(description="Book a volleyball court on ActiveCarrot")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--time", required=True, help="H:MMam/pm e.g. 6:00pm")
    p.add_argument("--duration", required=True, type=int, choices=[1, 2, 3, 4],
                   help="hours (1-4)")
    p.add_argument("--dry-run", action="store_true", help="plan only, book nothing")
    p.add_argument("--yes", action="store_true", help="skip final confirmation")
    return p.parse_args()


def build_plan(scanner, args, start_min, seg_minutes):
    courts = scanner.fetch_courts(args.date)
    plan = scanner.plan_same_court(courts, args.date, start_min, seg_minutes)
    if plan:
        return plan
    if len(seg_minutes) == 1:
        print("FAILED: no court free for the requested window.")
        sys.exit(1)
    print("No single court has the full window free.")
    if yesno("Split across courts?"):
        plan = scanner.plan_cross_court(courts, args.date, start_min, seg_minutes)
        if plan:
            return plan
        print("FAILED: no cross-court combination free either.")
        sys.exit(1)
    if yesno("Book first 2 hour session only?"):
        plan = scanner.plan_same_court(courts, args.date, start_min, [seg_minutes[0]])
        if plan:
            return plan
        print("FAILED: first 2h window not free on any court.")
        sys.exit(1)
    print("Aborted by user.")
    sys.exit(0)


def report(result):
    seg = result.segment
    where = seg.describe() if seg else "?"
    if result.success:
        print(f"SUCCESS: {where}")
        print(f"  account: {result.account_email}  paid: ${result.amount}")
        print(f"  receipt: {result.artifact}")
        print(f"  page: {result.detail[:300]}")
    else:
        print(f"FAILED at stage '{result.stage}': {where}")
        print(f"  account: {result.account_email}")
        print(f"  reason: {result.detail}")


def main():
    load_dotenv()
    args = parse_args()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
        start_min = parse_time(args.time)
    except (ValueError, BookingError) as e:
        sys.exit(f"Bad input: {e}")

    seg_minutes = split_segments(args.duration)
    scanner = AvailabilityScanner()
    try:
        plan = build_plan(scanner, args, start_min, seg_minutes)
        scanner.verify_plan(plan)
    except BookingError as e:
        sys.exit(f"FAILED: {e}")

    print("\nBooking plan:")
    for seg in plan:
        print(f"  - {seg.describe()}")
    if args.dry_run:
        print("Dry run - nothing booked.")
        return
    if not args.yes and not yesno("Proceed with booking (real payment)?"):
        print("Aborted by user.")
        return

    card = Card.from_env()
    sessions = [
        CheckoutSession(Account.from_env(seg.account_prefix), card, seg.account_prefix.lower())
        for seg in plan
    ]

    results = []
    try:
        def prep(pair):
            sess, seg = pair
            sess.start()
            sess.prepare(seg)

        try:
            with ThreadPoolExecutor(max_workers=len(sessions)) as ex:
                list(ex.map(prep, zip(sessions, plan)))
        except (BookingError, NotImplementedError) as e:
            sys.exit(f"FAILED before any payment (nothing charged): {e}")

        for i, sess in enumerate(sessions):
            res = sess.submit()
            results.append(res)
            report(res)
            if not res.success and i + 1 < len(sessions):
                print("Skipping remaining segment - first payment failed, nothing "
                      "further will be charged.")
                break
    finally:
        for sess in sessions:
            sess.close()

    booked = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    if booked and failed:
        print("\n*** PARTIAL SUCCESS - one segment booked, one failed. ***")
        print("*** Site allows no cancellations/refunds. Booked segment stands: ***")
        for r in booked:
            print(f"***   {r.segment.describe()} ***")
    sys.exit(0 if booked and not failed else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify dry run against live site**

Run: `uv run main.py --date <today+3 in YYYY-MM-DD> --time 4:00pm --duration 1 --dry-run`
Expected: prints one-segment booking plan naming a real court, then `Dry run - nothing booked.` exit 0. Also try `--duration 3 --dry-run` — expect two segments (PRIMARY 120 min + BACKUP 60 min) or interactive split prompts if no single court free.

- [ ] **Step 3: Verify bad input handling**

Run: `uv run main.py --date 2026-07-10 --time 25:00xx --duration 1 --dry-run`
Expected: exits with `Bad input: ...` message, nonzero exit code.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: CLI orchestration with split-booking prompts"
```

---

### Task 6: README + final review

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Write README**

```markdown
# VolleyballBooker

Books volleyball courts on ActiveCarrot (site 487, group 1848) from the terminal.

## Setup

1. `cp .env.example .env` and fill in real details (accounts + card).
2. Requires snap Chromium + chromedriver (`/snap/bin/chromium`, `/snap/bin/chromium.chromedriver`).

## Usage

```bash
uv run main.py --date 2026-07-10 --time 6:00pm --duration 2
```

- `--duration 1..4` hours. Durations over 2h are split into two bookings
  (primary + backup account) because the site caps a booking at 2 hours.
- `--dry-run` shows the plan without booking.
- `--yes` skips the final confirmation prompt.

Failures save a screenshot + HTML to `failures/`; successful receipts to `receipts/`.

## Known limitation

First checkout per email creates an ActiveCarrot account. Later bookings with the
same email hit a login redirect the tool does not implement yet
(`LoginCheckoutFlow` stub). Capture the logged-in payment page HTML and extend.
```

- [ ] **Step 2: Full dry-run sanity check**

Run: `uv run main.py --date <today+3> --time 4:00pm --duration 4 --dry-run` and answer prompts if shown.
Expected: sensible plan or clean failure messages; no tracebacks.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: usage README"
```

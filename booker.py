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
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

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
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
CHROME_BINARY = "/usr/bin/google-chrome"


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
        opts.binary_location = CHROME_BINARY
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
        self.driver = webdriver.Chrome(options=opts)
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

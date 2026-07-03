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

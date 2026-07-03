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

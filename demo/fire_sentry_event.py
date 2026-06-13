#!/usr/bin/env python3
"""Fire test events into Sentry to trigger the triage harness webhook.

Setup once:
    pip install sentry-sdk

Run:
    python demo/fire_sentry_event.py
    python demo/fire_sentry_event.py --kind db
    python demo/fire_sentry_event.py --kind ads
"""
from __future__ import annotations

import argparse
import sys

import sentry_sdk

DSN = "https://122c72f5be399f756d9bf5fa02ad55e1@o4511559224459264.ingest.us.sentry.io/4511559237238784"


def fire_null_pointer():
    sentry_sdk.set_tag("service", "users-api")
    sentry_sdk.set_tag("python", "true")
    sentry_sdk.set_tag("attribute_error", "true")
    try:
        x = None
        x.name  # noqa
    except Exception:
        sentry_sdk.capture_exception()


def fire_db_timeout():
    sentry_sdk.set_tag("service", "checkout-svc")
    sentry_sdk.set_tag("database", "true")
    sentry_sdk.set_tag("postgres", "true")
    sentry_sdk.set_tag("timeout", "true")
    sentry_sdk.set_level("fatal")
    try:
        raise TimeoutError("OperationalError: timeout expired (postgres)")
    except Exception:
        sentry_sdk.capture_exception()


def fire_ads_error():
    sentry_sdk.set_tag("service", "ads-tracker")
    sentry_sdk.set_tag("ads", "true")
    sentry_sdk.set_tag("campaign", "cpc")
    try:
        raise ValueError("invalid campaign_id format")
    except Exception:
        sentry_sdk.capture_exception()


KINDS = {
    "null": fire_null_pointer,
    "db": fire_db_timeout,
    "ads": fire_ads_error,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="null", choices=list(KINDS), help="event flavor")
    args = ap.parse_args()

    sentry_sdk.init(dsn=DSN, send_default_pii=True, traces_sample_rate=0.0)
    KINDS[args.kind]()
    sentry_sdk.flush(timeout=5)
    print(f"sent: {args.kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

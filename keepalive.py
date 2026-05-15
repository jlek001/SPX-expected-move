#!/usr/bin/env python3
"""IBKR Client Portal session keepalive.

The CP Gateway logs you out after ~10 minutes of inactivity. Run this in a
separate terminal (or in the background) to keep the session alive while you
work with spx_expected_move.py.

    python keepalive.py                     # tickle every 55 s
    python keepalive.py --interval 30       # faster
    python keepalive.py --base-url https://localhost:5001/v1/api
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from ibkr_client import IBKRClient, IBKRError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Keep an IBKR Client Portal session alive",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url", default="https://localhost:5000/v1/api",
        help="Client Portal Gateway base URL",
    )
    parser.add_argument(
        "--interval", type=int, default=55,
        help="Seconds between tickles (keep below 60 to prevent timeout)",
    )
    args = parser.parse_args()

    client = IBKRClient(base_url=args.base_url)
    print(
        f"Keepalive running against {args.base_url} "
        f"(interval={args.interval}s)  Ctrl-C to stop."
    )

    consecutive_failures = 0
    try:
        while True:
            ts = dt.datetime.now().strftime("%H:%M:%S")
            try:
                client.tickle()
                status = client.auth_status()
                authenticated = status.get("authenticated", False)
                connected = status.get("connected", False)
                competing = status.get("competing", False)
                flag = "OK" if authenticated else "NOT AUTHENTICATED"
                print(
                    f"[{ts}] {flag}  "
                    f"authenticated={authenticated}  "
                    f"connected={connected}  "
                    f"competing={competing}"
                )
                if not authenticated:
                    print(
                        f"         ↳ Open https://localhost:5000 in a browser "
                        "and log in to restore the session."
                    )
                consecutive_failures = 0
            except IBKRError as e:
                consecutive_failures += 1
                print(f"[{ts}] ERROR ({consecutive_failures}): {e}")
                if consecutive_failures >= 5:
                    print(
                        "5 consecutive failures — is the gateway running?",
                        file=sys.stderr,
                    )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

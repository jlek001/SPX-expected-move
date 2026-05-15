#!/usr/bin/env python3
"""SPX 0DTE Expected Move workflow using the IBKR Client Portal API.

Steps:
  1. Auth-check the local Client Portal gateway.
  2. Resolve the SPX index conid via symbol search.
  3. Pull a snapshot of SPX to get spot.
  4. Discover the nearest expiration on or after today (0DTE if available).
  5. Pull the strike chain for that month and pick N strikes either side of spot.
  6. Resolve each (strike, right) into an option conid for the chosen expiration.
  7. Snapshot every option to populate bid/ask/last/volume/open-interest.
  8. Print a per-contract table and the tastylive 60/30/10 weighted expected move.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import sys
from dataclasses import dataclass
from typing import Optional

from tabulate import tabulate

from ibkr_client import IBKRClient, IBKRError

LOG = logging.getLogger("spx_em")

# IBKR Client Portal snapshot field codes.
# These IDs are documented at https://interactivebrokers.github.io/cpwebapi/.
# OI in particular is gateway-version dependent — override with --oi-field
# if your gateway returns it under a different ID.
FIELD_LAST = "31"
FIELD_BID = "84"
FIELD_ASK = "86"
FIELD_VOLUME = "87"
FIELD_OI_DEFAULT = "7762"
SNAPSHOT_FIELDS_IDX = [FIELD_LAST, FIELD_BID, FIELD_ASK]

# Tastylive expected-move weights.
WEIGHT_ATM = 0.60
WEIGHT_OTM1 = 0.30
WEIGHT_OTM2 = 0.10

# ATM straddle premium ≈ S·σ·√(2T/π); inverting gives the 1-sigma move
# in dollar terms as straddle × √(π/2). Many traders use the straddle
# itself as the 1σ proxy; we expose both via the per-row column.
STRADDLE_TO_SIGMA = math.sqrt(math.pi / 2.0)  # ≈ 1.2533


@dataclass
class Option:
    conid: int
    symbol: str
    strike: float
    right: str  # 'C' or 'P'
    maturity: dt.date
    trading_class: str = "SPXW"
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if (
            self.bid is not None
            and self.ask is not None
            and self.bid >= 0
            and self.ask > 0
        ):
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def display_symbol(self) -> str:
        return (
            f"{self.trading_class} "
            f"{self.maturity.strftime('%y%m%d')}"
            f"{self.right}"
            f"{int(round(self.strike * 1000)):08d}"
        )


# --- IBKR response parsing -------------------------------------------------

def parse_ibkr_number(v) -> Optional[float]:
    """IBKR snapshot prices may arrive as numbers or strings prefixed with
    status indicators (e.g. 'C5800.50' for a closed market). Strip those."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    while s and s[0].isalpha() and s[0] not in {"e", "E"}:
        s = s[1:]
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_ibkr_count(v) -> Optional[float]:
    """Volume/OI fields can come back as '12.3K', '1.5M', or plain ints."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    multiplier = 1.0
    if s and s[-1] in "KMB":
        suffix = s[-1]
        s = s[:-1]
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9}[suffix]
    try:
        return float(s) * multiplier
    except ValueError:
        return None


# --- Chain discovery --------------------------------------------------------

def find_spx_conid(client: IBKRClient) -> tuple[int, str]:
    results = client.search_symbol("SPX", sec_type="IND")
    for r in results:
        if r.get("symbol") == "SPX":
            return int(r["conid"]), r.get("companyHeader") or r.get("companyName") or ""
    raise IBKRError("SPX index not found via /iserver/secdef/search")


def get_index_spot(client: IBKRClient, conid: int) -> Optional[float]:
    rows = client.snapshot([conid], SNAPSHOT_FIELDS_IDX)
    if not rows:
        return None
    row = rows[0]
    last = parse_ibkr_number(row.get(FIELD_LAST))
    if last is not None and last > 0:
        return last
    bid = parse_ibkr_number(row.get(FIELD_BID))
    ask = parse_ibkr_number(row.get(FIELD_ASK))
    if bid and ask:
        return (bid + ask) / 2.0
    return bid or ask


def resolve_expiration(
    client: IBKRClient,
    spx_conid: int,
    target: dt.date,
    exchange: str,
) -> dt.date:
    """Find the earliest SPX option expiration on or after `target`.

    Looks in the target month first and then the next month if necessary
    (handles end-of-month and post-weekend cases).
    """
    seen: list[dt.date] = []
    for month_offset in (0, 1):
        probe = target + dt.timedelta(days=month_offset * 32)
        month_tag = probe.strftime("%b%y").upper()
        try:
            strikes_info = client.strikes(spx_conid, month=month_tag, exchange=exchange)
        except IBKRError as e:
            LOG.debug("strikes lookup for %s failed: %s", month_tag, e)
            continue
        call_strikes = strikes_info.get("call") or []
        if not call_strikes:
            continue
        sample = call_strikes[len(call_strikes) // 2]
        try:
            contracts = client.contract_info(
                conid=spx_conid,
                sectype="OPT",
                month=month_tag,
                strike=sample,
                right="C",
                exchange=exchange,
            )
        except IBKRError as e:
            LOG.debug("contract_info probe failed for %s: %s", month_tag, e)
            continue
        if not isinstance(contracts, list):
            contracts = [contracts] if contracts else []
        for c in contracts:
            md = c.get("maturityDate")
            if not md:
                continue
            try:
                d = dt.datetime.strptime(md, "%Y%m%d").date()
            except ValueError:
                continue
            if d >= target:
                seen.append(d)
    if not seen:
        raise IBKRError("No SPX expirations on or after target date")
    return min(seen)


def select_strikes(strikes: list[float], spot: float, n_each_side: int) -> list[float]:
    if not strikes:
        return []
    sorted_strikes = sorted(strikes)
    atm_idx = min(
        range(len(sorted_strikes)), key=lambda i: abs(sorted_strikes[i] - spot)
    )
    lo = max(0, atm_idx - n_each_side)
    hi = min(len(sorted_strikes), atm_idx + n_each_side + 1)
    return sorted_strikes[lo:hi]


def resolve_contracts(
    client: IBKRClient,
    spx_conid: int,
    target_date: dt.date,
    strikes: list[float],
    exchange: str,
    prefer_class: str = "SPXW",
) -> dict[float, dict[str, Option]]:
    """For each strike, find the call+put contracts expiring on `target_date`."""
    month_tag = target_date.strftime("%b%y").upper()
    yyyymmdd = target_date.strftime("%Y%m%d")
    out: dict[float, dict[str, Option]] = {}
    for strike in strikes:
        for right in ("C", "P"):
            try:
                contracts = client.contract_info(
                    conid=spx_conid,
                    sectype="OPT",
                    month=month_tag,
                    strike=strike,
                    right=right,
                    exchange=exchange,
                )
            except IBKRError as e:
                LOG.warning("contract_info %s %s %s failed: %s", month_tag, strike, right, e)
                continue
            if not isinstance(contracts, list):
                contracts = [contracts] if contracts else []
            match = None
            for c in contracts:
                if c.get("maturityDate") != yyyymmdd:
                    continue
                if c.get("tradingClass") == prefer_class:
                    match = c
                    break
                if match is None:
                    match = c
            if not match:
                continue
            out.setdefault(strike, {})[right] = Option(
                conid=int(match["conid"]),
                symbol=match.get("symbol") or "SPX",
                strike=float(match.get("strike", strike)),
                right=right,
                maturity=target_date,
                trading_class=match.get("tradingClass") or prefer_class,
            )
    return out


def fill_snapshots(
    client: IBKRClient,
    contracts: dict[float, dict[str, Option]],
    oi_field: str,
) -> None:
    fields = [FIELD_LAST, FIELD_BID, FIELD_ASK, FIELD_VOLUME, oi_field]
    flat = [o for cp in contracts.values() for o in cp.values()]
    if not flat:
        return
    rows = client.snapshot([o.conid for o in flat], fields)
    by_conid: dict[int, dict] = {}
    for r in rows:
        cid = r.get("conid") or r.get("conidEx")
        if cid is None:
            continue
        try:
            by_conid[int(cid)] = r
        except (TypeError, ValueError):
            continue
    for opt in flat:
        row = by_conid.get(opt.conid)
        if not row:
            continue
        opt.bid = parse_ibkr_number(row.get(FIELD_BID))
        opt.ask = parse_ibkr_number(row.get(FIELD_ASK))
        opt.last = parse_ibkr_number(row.get(FIELD_LAST))
        opt.volume = parse_ibkr_count(row.get(FIELD_VOLUME))
        opt.open_interest = parse_ibkr_count(row.get(oi_field))


# --- Expected-move math -----------------------------------------------------

def straddle_mid(c: Option, p: Option) -> Optional[float]:
    if c.mid is None or p.mid is None:
        return None
    return c.mid + p.mid


def tastylive_expected_move(
    contracts: dict[float, dict[str, Option]], spot: float
) -> Optional[dict]:
    """tastylive 60/30/10 weighted expected move.

        EM = 0.60·ATM_straddle + 0.30·OTM1_strangle + 0.10·OTM2_strangle

    where the OTM strangles use the call N strikes above ATM and the put N
    strikes below. If a strangle is missing (e.g. you only requested a thin
    band around spot) its weight is dropped and the remaining weights are
    rescaled so the result still represents a 1-sigma move.
    """
    strikes = sorted(contracts.keys())
    if not strikes:
        return None
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    atm_strike = strikes[atm_idx]

    def at(idx: int, right: str) -> Optional[Option]:
        if 0 <= idx < len(strikes):
            return contracts.get(strikes[idx], {}).get(right)
        return None

    def pair_mid(call: Optional[Option], put: Optional[Option]) -> Optional[float]:
        if not call or not put or call.mid is None or put.mid is None:
            return None
        return call.mid + put.mid

    atm = pair_mid(at(atm_idx, "C"), at(atm_idx, "P"))
    otm1 = pair_mid(at(atm_idx + 1, "C"), at(atm_idx - 1, "P"))
    otm2 = pair_mid(at(atm_idx + 2, "C"), at(atm_idx - 2, "P"))

    if atm is None:
        return None

    weighted = WEIGHT_ATM * atm
    used = WEIGHT_ATM
    if otm1 is not None:
        weighted += WEIGHT_OTM1 * otm1
        used += WEIGHT_OTM1
    if otm2 is not None:
        weighted += WEIGHT_OTM2 * otm2
        used += WEIGHT_OTM2
    # Rescale so the result is still on the same basis as the full formula.
    em = weighted / used

    return {
        "expected_move": em,
        "atm_strike": atm_strike,
        "atm_straddle": atm,
        "otm1_strangle": otm1,
        "otm2_strangle": otm2,
        "weight_used": used,
    }


# --- Presentation -----------------------------------------------------------

def fmt_money(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.2f}"


def fmt_count(v: Optional[float]) -> str:
    return "-" if v is None else f"{int(round(v)):,}"


def build_rows(
    contracts: dict[float, dict[str, Option]], atm_strike: Optional[float]
) -> list[list]:
    rows: list[list] = []
    for strike in sorted(contracts.keys()):
        cp = contracts[strike]
        call = cp.get("C")
        put = cp.get("P")
        straddle = straddle_mid(call, put) if call and put else None
        sigma = straddle * STRADDLE_TO_SIGMA if straddle is not None else None
        marker = " *" if atm_strike is not None and math.isclose(strike, atm_strike) else ""
        for right in ("C", "P"):
            opt = cp.get(right)
            if not opt:
                continue
            rows.append(
                [
                    opt.display_symbol + marker,
                    f"{opt.strike:.2f}",
                    fmt_money(opt.bid),
                    fmt_money(opt.ask),
                    fmt_money(opt.mid),
                    fmt_count(opt.open_interest),
                    fmt_count(opt.volume),
                    fmt_money(sigma),
                ]
            )
    return rows


# --- CLI --------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="SPX 0DTE Expected Move via IBKR Client Portal API",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default="https://localhost:5000/v1/api",
        help="IBKR Client Portal Gateway base URL",
    )
    parser.add_argument(
        "--num-strikes",
        type=int,
        default=5,
        help="Strikes to pull on each side of ATM",
    )
    parser.add_argument("--exchange", default="SMART")
    parser.add_argument(
        "--expiration",
        help="YYYY-MM-DD override for the target expiration (default = today / 0DTE)",
    )
    parser.add_argument(
        "--trading-class",
        default="SPXW",
        help="Preferred option trading class (SPXW for weeklies/dailies)",
    )
    parser.add_argument(
        "--oi-field",
        default=FIELD_OI_DEFAULT,
        help="Snapshot field ID for open interest (varies by gateway version)",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Verify the gateway's TLS certificate (off by default for self-signed)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = IBKRClient(base_url=args.base_url, verify_ssl=args.verify_ssl)

    try:
        return run(client, args)
    except IBKRError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def run(client: IBKRClient, args: argparse.Namespace) -> int:
    print("== Auth check ==")
    try:
        client.tickle()
    except IBKRError as e:
        LOG.debug("tickle failed (non-fatal): %s", e)
    status = client.auth_status()
    if not status.get("authenticated"):
        print(
            "Gateway is not authenticated. Log in at the gateway URL first.\n"
            f"  status: {status}",
            file=sys.stderr,
        )
        return 2
    print(
        f"  authenticated={status.get('authenticated')} "
        f"connected={status.get('connected')} "
        f"competing={status.get('competing')}"
    )
    try:
        client.init_brokerage_session()
    except IBKRError as e:
        LOG.debug("init_brokerage_session failed (non-fatal): %s", e)

    print("\n== Resolving SPX ==")
    spx_conid, header = find_spx_conid(client)
    print(f"  conid={spx_conid}  ({header})")

    print("\n== SPX spot ==")
    spot = get_index_spot(client, spx_conid)
    if spot is None:
        print("Could not fetch SPX spot price.", file=sys.stderr)
        return 3
    print(f"  spot={spot:.2f}")

    target = (
        dt.date.fromisoformat(args.expiration)
        if args.expiration
        else dt.date.today()
    )
    print(f"\n== Expiration on/after {target.isoformat()} ==")
    expiry = resolve_expiration(client, spx_conid, target, args.exchange)
    dte = (expiry - dt.date.today()).days
    print(f"  selected={expiry.isoformat()}  DTE={dte}")

    print("\n== Strike chain ==")
    month_tag = expiry.strftime("%b%y").upper()
    strikes_resp = client.strikes(spx_conid, month=month_tag, exchange=args.exchange)
    all_strikes = sorted(
        set((strikes_resp.get("call") or []) + (strikes_resp.get("put") or []))
    )
    print(f"  {len(all_strikes)} total strikes in {month_tag}")
    chosen = select_strikes(all_strikes, spot, args.num_strikes)
    if not chosen:
        print("No strikes selected.", file=sys.stderr)
        return 4
    print(
        f"  using {len(chosen)} strikes: "
        f"{chosen[0]:.0f} .. {chosen[-1]:.0f}"
    )

    print("\n== Resolving option contracts ==")
    contracts = resolve_contracts(
        client, spx_conid, expiry, chosen, args.exchange,
        prefer_class=args.trading_class,
    )
    resolved = sum(len(cp) for cp in contracts.values())
    print(f"  resolved {resolved} contracts across {len(contracts)} strikes")
    if not contracts:
        print("No option contracts resolved.", file=sys.stderr)
        return 5

    print("\n== Snapshots ==")
    fill_snapshots(client, contracts, oi_field=args.oi_field)
    print("  populated bid/ask/last/volume/open-interest")

    # ATM strike (used for the marker in the table and the 60/30/10 formula)
    atm_strike = min(contracts.keys(), key=lambda k: abs(k - spot))

    headers = [
        "Contract",
        "Strike",
        "Bid",
        "Ask",
        "Mid",
        "OI",
        "Volume",
        "1σ Move",
    ]
    rows = build_rows(contracts, atm_strike)
    print()
    print(tabulate(rows, headers=headers, tablefmt="github", stralign="right"))
    print("  (* = ATM strike. 1σ Move = per-strike straddle mid × √(π/2))")

    print("\n== tastylive 60/30/10 Expected Move ==")
    em = tastylive_expected_move(contracts, spot)
    if em is None:
        print(
            "  Insufficient quotes around the ATM strike to compute the "
            "weighted expected move.",
            file=sys.stderr,
        )
        return 6
    move = em["expected_move"]
    print(f"  Spot                = {spot:.2f}")
    print(f"  ATM strike          = {em['atm_strike']:.2f}")
    print(f"  ATM straddle (mid)  = {fmt_money(em['atm_straddle'])}")
    print(f"  1st OTM strangle    = {fmt_money(em['otm1_strangle'])}")
    print(f"  2nd OTM strangle    = {fmt_money(em['otm2_strangle'])}")
    print(f"  Weights applied     = {em['weight_used']:.2f}")
    print(f"  Expected move (1σ)  = ±{move:.2f}")
    print(f"  Upper bound         = {spot + move:.2f}")
    print(f"  Lower bound         = {spot - move:.2f}")
    pct = move / spot * 100.0 if spot else float("nan")
    print(f"  As % of spot        = ±{pct:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())

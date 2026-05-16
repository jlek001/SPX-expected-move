#!/usr/bin/env python3
"""FastAPI server exposing the SPX expected-move workflow as a mobile-friendly web app.

Run it on the same machine as the IBKR Client Portal Gateway:

    python app.py                       # binds 0.0.0.0:8000
    python app.py --port 8080
    python app.py --host 127.0.0.1      # local-only

Then open http://<your-desktop-ip>:8000 from any browser on the same network
(or tunnel it via Tailscale/ngrok for remote access).
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import spx_expected_move as core
from ibkr_client import IBKRClient, IBKRError

LOG = logging.getLogger("spx_em.app")

STATIC_DIR = Path(__file__).parent / "static"

# Global state. The CP Gateway runs locally, so a single shared session is
# fine — and contract conids are stable for a given expiration, so we resolve
# the chain once and re-use it across requests.
_state: dict[str, Any] = {
    "client": None,
    "spx_conid": None,
    "expiry": None,
    "contracts": None,
    "chain_key": None,          # (num_strikes, expiration override) — invalidates the cache
    "gateway_base_url": "https://localhost:5000/v1/api",
    "oi_field": "7762",
    "exchange": "SMART",
    "trading_class": "SPXW",
}
_lock = threading.Lock()


def _get_client() -> IBKRClient:
    if _state["client"] is None:
        _state["client"] = IBKRClient(base_url=_state["gateway_base_url"])
    return _state["client"]


def _resolve_chain(client: IBKRClient, num_strikes: int, expiration: Optional[str]) -> tuple[int, dt.date, dict]:
    """Resolve SPX → expiry → strike chain → option conids. Cached per (num_strikes, expiration)."""
    key = (num_strikes, expiration)
    if (
        _state["contracts"] is not None
        and _state["chain_key"] == key
        and _state["expiry"] is not None
        and _state["expiry"] >= dt.date.today()
    ):
        return _state["spx_conid"], _state["expiry"], _state["contracts"]

    client.init_brokerage_session()
    spx_conid, _ = core.find_spx_conid(client)
    target = dt.date.fromisoformat(expiration) if expiration else dt.date.today()
    expiry = core.resolve_expiration(client, spx_conid, target, _state["exchange"])
    month_tag = expiry.strftime("%b%y").upper()
    strikes_resp = client.strikes(spx_conid, month=month_tag, exchange=_state["exchange"])
    all_strikes = sorted(
        set((strikes_resp.get("call") or []) + (strikes_resp.get("put") or []))
    )
    spot = core.get_index_spot(client, spx_conid) or 0.0
    chosen = core.select_strikes(all_strikes, spot, num_strikes)
    contracts = core.resolve_contracts(
        client, spx_conid, expiry, chosen, _state["exchange"],
        prefer_class=_state["trading_class"],
    )
    _state.update(
        spx_conid=spx_conid, expiry=expiry, contracts=contracts, chain_key=key,
    )
    return spx_conid, expiry, contracts


def _serialize(
    spot: float,
    expiry: dt.date,
    contracts: dict,
    atm_strike: float,
    em: Optional[dict],
) -> dict:
    chain = []
    for strike in sorted(contracts.keys()):
        cp = contracts[strike]
        call, put = cp.get("C"), cp.get("P")
        straddle = core.straddle_mid(call, put) if call and put else None
        sigma = straddle * core.STRADDLE_TO_SIGMA if straddle is not None else None
        row: dict[str, Any] = {
            "strike": strike,
            "is_atm": strike == atm_strike,
            "sigma_move": sigma,
            "straddle_mid": straddle,
        }
        for right_key, opt in (("c", call), ("p", put)):
            if opt:
                row[right_key] = {
                    "symbol": opt.display_symbol,
                    "bid": opt.bid,
                    "ask": opt.ask,
                    "mid": opt.mid,
                    "volume": opt.volume,
                    "open_interest": opt.open_interest,
                }
            else:
                row[right_key] = None
        chain.append(row)

    out = {
        "ok": True,
        "spot": spot,
        "expiry": expiry.isoformat(),
        "dte": (expiry - dt.date.today()).days,
        "atm_strike": atm_strike,
        "chain": chain,
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if em is not None:
        move = em["expected_move"]
        out.update(
            expected_move=move,
            atm_straddle=em["atm_straddle"],
            otm1_strangle=em["otm1_strangle"],
            otm2_strangle=em["otm2_strangle"],
            weight_used=em["weight_used"],
            upper_bound=spot + move,
            lower_bound=spot - move,
            pct_move=move / spot * 100.0 if spot else None,
        )
    else:
        out.update(
            expected_move=None, atm_straddle=None, otm1_strangle=None,
            otm2_strangle=None, weight_used=None, upper_bound=None,
            lower_bound=None, pct_move=None,
        )
    return out


# --- FastAPI app ------------------------------------------------------------

app = FastAPI(title="SPX Expected Move", docs_url="/api/docs", redoc_url=None)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def api_status() -> dict:
    try:
        client = _get_client()
        try:
            client.tickle()
        except IBKRError:
            pass
        status = client.auth_status()
        return {
            "ok": True,
            "authenticated": bool(status.get("authenticated")),
            "connected": bool(status.get("connected")),
            "competing": bool(status.get("competing")),
            "gateway_url": _state["gateway_base_url"],
        }
    except IBKRError as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "gateway_url": _state["gateway_base_url"]},
            status_code=502,
        )


@app.get("/api/snapshot")
def api_snapshot(
    num_strikes: int = Query(5, ge=1, le=25),
    expiration: Optional[str] = Query(None, description="YYYY-MM-DD override"),
    refresh_chain: bool = Query(False, description="Force re-resolution of option conids"),
) -> dict:
    with _lock:
        try:
            client = _get_client()
            try:
                client.tickle()
            except IBKRError:
                pass
            status = client.auth_status()
            if not status.get("authenticated"):
                raise HTTPException(
                    status_code=401,
                    detail=(
                        f"Gateway not authenticated. Open {_state['gateway_base_url'].rsplit('/v1/api',1)[0]} "
                        "in a browser and log in."
                    ),
                )

            if refresh_chain:
                _state["contracts"] = None
                _state["chain_key"] = None

            spx_conid, expiry, contracts = _resolve_chain(client, num_strikes, expiration)
            spot = core.get_index_spot(client, spx_conid)
            if spot is None:
                raise HTTPException(502, "Could not fetch SPX spot price.")

            core.fill_snapshots(client, contracts, oi_field=_state["oi_field"])
            atm_strike = min(contracts.keys(), key=lambda k: abs(k - spot))
            em = core.tastylive_expected_move(contracts, spot)
            return _serialize(spot, expiry, contracts, atm_strike, em)
        except IBKRError as e:
            raise HTTPException(502, str(e)) from e


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPX Expected Move web/mobile server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (0.0.0.0 exposes on LAN)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--gateway-url", default="https://localhost:5000/v1/api",
        help="IBKR Client Portal Gateway base URL",
    )
    parser.add_argument("--oi-field", default="7762", help="Snapshot field ID for open interest")
    parser.add_argument("--exchange", default="SMART")
    parser.add_argument("--trading-class", default="SPXW")
    args = parser.parse_args()

    _state["gateway_base_url"] = args.gateway_url
    _state["oi_field"] = args.oi_field
    _state["exchange"] = args.exchange
    _state["trading_class"] = args.trading_class

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import uvicorn

    print(f"SPX Expected Move server: http://{args.host}:{args.port}")
    print(f"  → talking to gateway at {args.gateway_url}")
    print(f"  → from mobile, browse to http://<your-LAN-ip>:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

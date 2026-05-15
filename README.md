# SPX 0DTE Expected Move

A small Python workflow that pulls a live SPX 0DTE options snapshot from the
Interactive Brokers **Client Portal API** gateway, resolves the nearest
expiration and a range of strikes around spot, and computes a 1-sigma
expected move using the tastylive 60/30/10 weighting.

## What it does

1. Verifies the local Client Portal gateway is authenticated.
2. Resolves the SPX index conid via `/iserver/secdef/search`.
3. Pulls a snapshot of SPX to get spot.
4. Finds the nearest expiration on or after today (0DTE when available).
5. Picks the ATM strike and N strikes either side of it.
6. Resolves each (strike, right) pair into an option conid for that
   expiration, preferring the `SPXW` trading class for dailies/weeklies.
7. Snapshots every option to populate bid / ask / last / volume / OI.
8. Prints a per-contract table plus the tastylive expected move.

## tastylive 60/30/10 formula

```
EM(1σ) = 0.60 · ATM_straddle
       + 0.30 · 1st_OTM_strangle
       + 0.10 · 2nd_OTM_strangle
```

- **ATM straddle** = call mid + put mid at the strike nearest spot.
- **1st OTM strangle** = call one strike above ATM + put one strike below.
- **2nd OTM strangle** = call two strikes above + put two strikes below.

If the OTM strangles are unavailable (e.g. you requested only a thin band of
strikes), their weights are dropped and the remaining weights are rescaled so
the result still represents a 1-sigma move.

## Prerequisites

- IBKR Client Portal Gateway (or IBeam) running locally; default URL
  `https://localhost:5000/v1/api`.
- An authenticated session: visit the gateway URL in a browser and complete
  the IBKR login flow before running this script.
- A market-data subscription that covers SPX index options. Without it the
  snapshot endpoint will return delayed or empty fields.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python spx_expected_move.py
# Or with options
python spx_expected_move.py --num-strikes 7 --base-url https://localhost:5000/v1/api
```

## CLI options

| Flag | Default | Description |
|---|---|---|
| `--base-url` | `https://localhost:5000/v1/api` | Client Portal gateway base URL |
| `--num-strikes` | `5` | Strikes to pull on each side of ATM |
| `--exchange` | `SMART` | Exchange routing for the chain lookup |
| `--expiration` | today | Override target expiration (`YYYY-MM-DD`) |
| `--trading-class` | `SPXW` | Preferred trading class (use `SPX` for monthlies) |
| `--oi-field` | `7762` | Snapshot field ID for OI (varies by gateway) |
| `--verify-ssl` | off | Verify the gateway's TLS cert (off by default for self-signed) |
| `--verbose` | off | Debug logging |

## Example output

```
== Auth check ==
  authenticated=True connected=True competing=False

== Resolving SPX ==
  conid=416904  (S&P 500 - CBOE)

== SPX spot ==
  spot=5821.40

== Expiration on/after 2026-05-15 ==
  selected=2026-05-15  DTE=0

== Strike chain ==
  256 total strikes in MAY26
  using 11 strikes: 5795 .. 5845

== Resolving option contracts ==
  resolved 22 contracts across 11 strikes

== Snapshots ==
  populated bid/ask/last/volume/open-interest

| Contract                       | Strike  | Bid  | Ask  | Mid  |     OI |  Volume | 1σ Move |
|--------------------------------|---------|------|------|------|--------|---------|---------|
| SPXW 260515C05795000           | 5795.00 | 31.4 | 32.1 | 31.8 |  1,234 |   8,910 |   62.93 |
| SPXW 260515P05795000           | 5795.00 |  5.5 |  6.0 |  5.8 |  2,201 |   7,344 |   62.93 |
| ...                                                                                          |
| SPXW 260515C05820000 *         | 5820.00 | 15.0 | 15.4 | 15.2 |  3,109 |  12,455 |   38.66 |
| SPXW 260515P05820000 *         | 5820.00 | 15.6 | 16.1 | 15.85| 4,002  |  11,201 |   38.66 |
| ...                                                                                          |
  (* = ATM strike. 1σ Move = per-strike straddle mid × √(π/2))

== tastylive 60/30/10 Expected Move ==
  Spot                = 5821.40
  ATM strike          = 5820.00
  ATM straddle (mid)  = 31.05
  1st OTM strangle    = 27.40
  2nd OTM strangle    = 22.10
  Weights applied     = 1.00
  Expected move (1σ)  = ±29.04
  Upper bound         = 5850.44
  Lower bound         = 5792.36
  As % of spot        = ±0.50%
```

## Notes & caveats

- **0DTE availability**: outside RTH, SPX 0DTE may not be quoting. On a
  weekend the script falls forward to the next available expiration.
- **Field IDs**: the Client Portal snapshot field for open interest has
  varied across gateway versions. Override with `--oi-field` if your gateway
  exposes it under a different code; `OI` will simply render as `-` when the
  field is unavailable.
- **Snapshot priming**: IBKR's snapshot endpoint streams data lazily — the
  client polls a few times to allow fields to populate. If you still see
  empty fields, market data subscriptions or post-hours conditions are the
  usual culprits.
- **No order routing**: this script is read-only. It performs no trades and
  does not place any orders.

# SPX 0DTE Expected Move — IBKR Client Portal

Pulls a live SPX 0DTE options snapshot from a running Interactive Brokers
Client Portal Gateway, resolves the nearest daily expiration and a band of
strikes around spot, and prints:

- A per-contract table (symbol, strike, bid, ask, mid, OI, volume, 1σ move).
- The tastylive **60/30/10** weighted expected move with upper/lower bounds.

---

## 1. Local setup — IBKR Client Portal Gateway

### 1a. Requirements

- **IBKR account** with market-data subscriptions for SPX index options.
- **Java 11 or later** (`java -version` to check; install via your package
  manager or https://adoptium.net if missing).

### 1b. Download the gateway

Log in to the IBKR website, navigate to **Technology → APIs → Client Portal
API**, and download the latest **Client Portal API** zip. Unzip it somewhere
convenient, e.g.:

```bash
unzip clientportal.gw.zip -d ~/ibkr-gateway
cd ~/ibkr-gateway
```

### 1c. Verify the bundled config

The zip includes `root/conf.yaml`. The defaults work out of the box for a
local session; the relevant fields are:

```yaml
listenPort: 5000
listenSsl: true
```

No edits needed unless you want a different port.

### 1d. Start the gateway

```bash
# macOS / Linux
cd ~/ibkr-gateway
bin/run.sh root/conf.yaml

# Windows
cd %USERPROFILE%\ibkr-gateway
bin\run.bat root\conf.yaml
```

The gateway prints startup messages and then waits for a browser login.

### 1e. Authenticate

Open `https://localhost:5000` in a browser. Accept the self-signed
certificate warning (it's expected — the gateway uses a local cert). Log in
with your IBKR username and password, and complete any two-factor prompt.

Once the browser shows "Client login succeeds", the gateway is ready.

---

## 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

---

## 3. Run

### Single snapshot

```bash
python spx_expected_move.py
```

### Intraday watch mode (auto-refreshes, contract conids cached after first run)

```bash
python spx_expected_move.py --watch 60   # refresh every 60 seconds
```

### Keep the gateway session alive in a separate terminal

The gateway logs you out after ~10 minutes of inactivity. Run this alongside
the main script:

```bash
python keepalive.py                  # tickle every 55 s
python keepalive.py --interval 30    # faster
```

---

## 4. CLI reference

### spx_expected_move.py

| Flag | Default | Description |
|------|---------|-------------|
| `--base-url` | `https://localhost:5000/v1/api` | Gateway URL |
| `--num-strikes` | `5` | Strikes each side of ATM |
| `--exchange` | `SMART` | Chain lookup routing |
| `--expiration` | today | Override target date (`YYYY-MM-DD`) |
| `--trading-class` | `SPXW` | `SPXW` for dailies/weeklies, `SPX` for monthlies |
| `--oi-field` | `7762` | Snapshot field ID for open interest |
| `--watch` | off | Refresh every N seconds (Ctrl-C to stop) |
| `--verify-ssl` | off | Verify gateway TLS cert |
| `--verbose` | off | Debug logging |

### keepalive.py

| Flag | Default | Description |
|------|---------|-------------|
| `--base-url` | `https://localhost:5000/v1/api` | Gateway URL |
| `--interval` | `55` | Seconds between tickles |

---

## 5. tastylive 60/30/10 formula

```
EM(1σ) = 0.60 × ATM_straddle
       + 0.30 × 1st_OTM_strangle   (call +1 strike, put −1 strike)
       + 0.10 × 2nd_OTM_strangle   (call +2 strikes, put −2 strikes)
```

All legs use mid prices. If an OTM strangle is unavailable its weight is
dropped and the remaining weights are rescaled. The result is a dollar-value
1-sigma expected move for the chosen expiration.

The per-row **1σ Move** column in the table shows `straddle_mid × √(π/2)`
— the theoretical 1-sigma from that strike's own call+put pair — and lets
you see how the implied move varies across the chain.

---

## 6. Example output

```
SPX  spot=5821.40  expiry=2026-05-15 (0DTE)  [10:32:11]

|               Contract | Strike |   Bid |   Ask |   Mid |    OI |  Volume | 1σ Move |
|------------------------|--------|-------|-------|-------|-------|---------|---------|
|   SPXW 260515C05795000 |   5795 | 31.40 | 31.80 | 31.60 | 1,203 |   8,410 |   46.29 |
|   SPXW 260515P05795000 |   5795 |  5.20 |  5.60 |  5.40 | 2,101 |   6,840 |   46.29 |
|           ...                                                                         |
| SPXW 260515C05820000 * |   5820 |  9.10 |  9.50 |  9.30 | 3,009 |  11,455 |   21.43 |
| SPXW 260515P05820000 * |   5820 |  7.70 |  8.10 |  7.90 | 3,802 |  10,201 |   21.43 |
|           ...                                                                         |

  (* = ATM strike  |  1σ Move = per-strike straddle mid × √(π/2))

  tastylive 60/30/10  EM = ±16.20 (0.28%)   [5805.20 … 5837.60]
    ATM straddle = 17.20  |  OTM1 = 15.10  |  OTM2 = 13.90  |  weights = 1.00
```

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Connection refused` on startup | Gateway not running | Start `bin/run.sh root/conf.yaml` |
| `Gateway is not authenticated` | Session expired or never logged in | Open `https://localhost:5000` in a browser and log in |
| All bid/ask fields show `-` | Delayed or missing market-data subscription | Verify the SPX option data subscription in Account Management |
| OI shows `-` | Field `7762` not populated by this gateway version | Try `--oi-field 7085` or `--oi-field 7635`; check your gateway's API reference |
| `competing=True` in auth status | Another session is active (e.g. TWS) | Either close the competing session or enable "Allow competing connection" in TWS |
| Session logs out quickly | No keepalive | Run `python keepalive.py` in a second terminal |

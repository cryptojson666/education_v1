# TradingView Webhook → Bitunix Auto Trade - AI Agent Development Guide

## Project Overview
A FastAPI webhook receiver for TradingView alerts that validates incoming signals, calculates position sizing, and places orders on Bitunix futures exchange.

**Stack**: Python 3.11+, FastAPI, Uvicorn, Pydantic v2, python-dotenv, requests
**Port**: 8000
**Endpoint**: `POST /webhook`
**Bitunix API**: Official Futures API v1 (https://fapi.bitunix.com)

---

## 1. Repository Structure
```
tradingview_webhook/
├── app.py                 # Main FastAPI application
├── test_local.py          # Local test script with proper timestamps
├── requirements.txt       # Dependencies
├── .env                   # Local secrets (gitignored)
├── .env.example           # Template for secrets
└── AGENTS.md              # This file
```

---

## 2. Core Architecture

### 2.1 Flow
```
TradingView Alert → Webhook Validation → Position Calculation → Bitunix place_order
```

### 2.2 Key Components
- **Settings Class**: Centralized config with validation
- **BitunixClient**: Official API v1 compatible client
- **Webhook Validation**: Body token + API key with HMAC compare
- **Position Calculator**: Fixed USDT or equity % sizing
- **BitunixClient**: Official API v1 client with proper signatures
- **DRY_RUN Mode**: Safe testing without real orders

---

## 3. Critical Implementation Details

### 3.1 Webhook Validation (Body Token)
**Why**: TradingView removed `Secret` field from UI. HMAC signature (`X-Signature` header) no longer works reliably.
**Solution**: Shared secret in JSON body `token` field + `api_key`.

**Server Validation**:
```python
# app.py authenticate()
if b"{{" in raw_body:  # Reject unrendered TV placeholders
    raise HTTPException(400, "Payload contains unrendered placeholders")

if not hmac.compare_digest(token, settings.webhook_secret):
    raise HTTPException(403, "Token 驗證失敗")
if not hmac.compare_digest(api_key, settings.webhook_api_key):
    raise HTTPException(403, detail="API Key 驗證失敗")
```

**TradingView Message JSON** (use quoted timestamps!):
```json
{
  "token": "tv_sig_secret_xxx",
  "api_key": "tv_api_key_xxx",
  "timestamp": "{{timenow}}",
  "payload": {
    "ticker": "{{ticker}}",
    "action": "buy",
    "price": {{close}},
    "time": "{{time}}"
  }
}
```

### 3.2 TradingView JSON Quirks (Critical)
**Problem**: TV test webhook sends `{{timenow}}` and `{{time}}` as unquoted ISO strings (invalid JSON).
**Fix**: Force quotes in TV Message template:
```json
"timestamp": "{{timenow}}",   // Quoted!
"time": "{{time}}",           // Quoted!
```
Numbers (`{{close}}`, `{{high}}`) remain unquoted.

### 3.3 Bitunix API Client (Official Demo Aligned)
**Base URL**: `https://fapi.bitunix.com`
**Auth Headers**: `api-key`, `sign`, `nonce`, `timestamp`, `language: en-US`
**No Passphrase** (Bitunix doesn't use it)

**Signature Algorithm** (Double SHA256):
```
digest = SHA256(nonce + timestamp + api_key + query_params + body)
sign   = SHA256(digest + secret_key)
```
- GET params sorted: `k1v1k2v2...`
- Body: compact JSON (`separators=(',', ':')`)

**Endpoints**:
- Account: `GET /api/v1/futures/account?marginCoin=USDT`
- Tickers: `GET /api/v1/futures/market/tickers?symbols=BTCUSDT`
- Contracts: `GET /api/v1/futures/market/trading_pairs`
- Place Order: `POST /api/v1/futures/trade/place_order`
- Leverage: `POST /api/v1/futures/account/change_leverage`
- Margin Mode: `POST /api/v1/futures/account/change_margin_mode`

### 3.4 Order Placement (Official Demo Params)
```json
{
  "symbol": "BTCUSDT",
  "side": "BUY",
  "orderType": "MARKET",
  "qty": "0.001",
  "tradeSide": "OPEN",
  "effect": "GTC",
  "reduceOnly": false
}
```
- `side`: "BUY" / "SELL"
- `tradeSide`: "OPEN" (only in hedge mode)
- `qty`: string, quantized to contract precision
- `marginCoin`: not in body (account level)

### 3.5 Position Sizing
```python
# Fixed USDT mode (default)
margin = TRADE_FIXED_USDT  # 1.0 USDT
notional = margin * leverage  # 10 USDT @ 10x
qty = notional / entry_price
qty = quantize_qty(symbol, qty)  # Decimal ROUND_DOWN
```

### 3.6 Safety Features
| Feature | Implementation |
|---------|----------------|
| **DRY_RUN** | `TRADE_DRY_RUN=true` prints body, no real order |
| **ClientId Idempotency** | SHA256(payload) → `clientId`, Bitunix returns 30042 on duplicate |
| **Placeholder Rejection** | Rejects any `{{` in raw body |
| **Placeholder Rejection** | Any `{{` in body → 400 rejection |
| **TP/SL Auto-Send** | `tpPrice`, `slPrice` with `LAST_PRICE` stop type |
| **Precision Quantization** | Decimal ROUND_DOWN, respects `min_qty` |
| **run_in_threadpool** | Non-blocking sync HTTP calls |

---

## 4. Environment Variables (`.env`)

```env
# Webhook
WEBHOOK_API_KEY=tv_api_key_xxx
WEBHOOK_SIGNATURE_SECRET=tv_sig_secret_xxx

# Bitunix
BITUNIX_API_KEY=xxx
BITUNIX_SECRET_KEY=xxx
BITUNIX_BASE_URL=https://fapi.bitunix.com

# Trading
TRADE_DRY_RUN=true              # true=只印不下單，首跑必開
TRADE_USE_FIXED_USDT=true       # true=固定保證金, false=權益百分比
TRADE_FIXED_USDT=1.0            # 固定 1 USDT
TRADE_EQUITY_PERCENT=0.01       # 1% 權益 (TRADE_USE_FIXED_USDT=false 時)
TRADE_LEVERAGE=10               # 10x 槓桿
TRADE_MARGIN_MODE=ISOLATION     # ISOLATION / CROSS
TRADE_POSITION_MODE=oneway      # oneway / hedge
TRADE_SL_PERCENT=0.05           # 5% 停損
TRADE_TP_PERCENT=0.05           # 5% 停利 (1:1)
TRADE_MARGIN_COIN=USDT
TRADE_SYMBOL_WHITELIST=         # 逗號分隔，留空=不限制
TRADE_SYMBOL_WHITELIST=BTCUSDT,ETHUSDT  # 例
```

**Required**: `WEBHOOK_API_KEY`, `WEBHOOK_SIGNATURE_SECRET`, `BITUNIX_API_KEY`, `BITUNIX_SECRET_KEY`

---

## 5. Local Development Workflow

### 5.1 Start Server
```bash
cd tradingview_webhook
py -m venv venv
source venv/Scripts/activate
py -m pip install -r requirements.txt
py app.py
```

### 5.2 Verify Endpoints
```bash
# Health check
curl http://localhost:8000/health

# Contract precision cache
curl http://localhost:8000/test/contracts?symbol=STXUSDT

# Balance check (verifies auth)
curl http://localhost:8000/test/balance

# DRY RUN order test
curl -X POST "http://localhost:8000/test/trade?symbol=STXUSDT&side=BUY&qty=200"
```

### 5.3 Expose via Tunnel
```bash
# ngrok (free, but IP changes)
ngrok http --host-header=localhost 0.0.0.0:8000

# Cloudflare Tunnel (free, fixed URL)
cloudflared tunnel --url http://localhost:8000
```

### 5.4 TradingView Alert Setup
1. **Webhook URL**: `https://your-ngrok-url.ngrok-free.app/webhook`
2. **Secret**: Leave empty
3. **Message** (use quoted timestamps!):
```json
{
  "token": "tv_sig_secret_xxx",
  "api_key": "tv_api_key_xxx",
  "timestamp": "{{timenow}}",
  "payload": {
    "ticker": "{{ticker}}",
    "exchange": "{{exchange}}",
    "price": {{close}},
    "time": "{{time}}",
    "action": "{{strategy.order.action}}",
    "comment": "{{strategy.order.comment}}"
  }
}
```

---

## 6. Common Pitfalls & Solutions

| Symptom | Cause | Solution |
|---------|-------|----------|
| `400 Bad Request` / `Expecting ',' delimiter` | TV test sends unquoted ISO timestamps | Quote `{{timenow}}` and `{{time}}` in TV Message |
| `403 Invalid Token` | `token` in JSON ≠ `.env` `WEBHOOK_SIGNATURE_SECRET` | Ensure TV Message `token` matches `.env` `WEBHOOK_SIGNATURE_SECRET` |
| `403 Invalid API Key` | `api_key` mismatch | Match TV Message `api_key` to `.env` `WEBHOOK_API_KEY` |
| `10004 Invalid IP` | Current IP not in Bitunix API Key whitelist | Add current IP to Bitunix API Key whitelist |
| `10007 Signature Error` | Signature mismatch | Check system time sync, API Key/Secret correctness |
| `10002 Parameter Error` | Missing required fields | Check `marginCoin`, `leverage`, `marginMode` in order |
| `30016 Min Qty` | Order qty below min_qty | Check `min_qty` in `/test/contracts` |
| `30042 Duplicate Client ID` | TV resent same alert | Handled automatically (clientId idempotency) |
| `ngrok` no Forwarding URL | Local server not on 8000 | Verify `curl localhost:8000/health` |
| `ngrok` ERR_NGROK_8013 | Used `ngrok tcp` | Use `ngrok http --host-header=localhost 0.0.0.0:8000` |
| TV test shows `action: "{{strategy.order.action}}"` | TV test sends literal template | Normal. Real alerts send actual values (`buy`/`sell`) |
| `ModuleNotFoundError` / `py not found` | Python not in PATH | Install Python 3.11+ with "Add to PATH". Use `py` launcher on Windows |

---

## 7. Production Deployment Checklist
- [ ] Replace ngrok with **Cloudflare Tunnel** (free, fixed URL) or **VPS + Cloudflare Tunnel**
- [ ] Set `TRADE_DRY_RUN=false` after verification
- [ ] Set `WEBHOOK_API_KEY`, `WEBHOOK_SIGNATURE_SECRET`, `BITUNIX_API_KEY`, `BITUNIX_SECRET_KEY` as **strong random secrets** (32+ chars)
- [ ] Disable `reload=True` in `uvicorn.run()`
- [ ] Add rate limiting (e.g., `slowapi`)
- [ ] Add persistent logging (file/ELK) and monitoring
- [ ] Store secrets in platform secret manager (not `.env`)
- [ ] VPS with fixed IP for stable Bitunix whitelist

---

## 8. Quick Reference Commands
```bash
# Start server
cd tradingview_webhook
source venv/Scripts/activate
py app.py

# Test endpoints
curl http://localhost:8000/health
curl http://localhost:8000/test/contracts?symbol=STXUSDT
curl http://localhost:8000/test/balance
curl -X POST "http://localhost:8000/test/trade?symbol=STXUSDT&side=BUY&qty=200"

# ngrok
ngrok http --host-header=localhost 0.0.0.0:8000

# Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8000

# Health check
curl http://localhost:8000/health
```

---

## 9. File Templates for Instant Reproduction

### `requirements.txt`
```
fastapi
uvicorn[standard]
pydantic
python-dotenv
requests
```

### `.env.example`
```env
# Webhook
WEBHOOK_API_KEY=tv_api_key_xxx
WEBHOOK_SIGNATURE_SECRET=tv_sig_secret_xxx

# Bitunix
BITUNIX_API_KEY=your_bitunix_api_key
BITUNIX_SECRET_KEY=your_bitunix_secret_key
BITUNIX_BASE_URL=https://fapi.bitunix.com

# Trading
TRADE_DRY_RUN=true
TRADE_USE_FIXED_USDT=true
TRADE_FIXED_USDT=1.0
TRADE_EQUITY_PERCENT=0.01
TRADE_LEVERAGE=10
TRADE_MARGIN_MODE=ISOLATION
TRADE_POSITION_MODE=oneway
TRADE_SL_PERCENT=0.05
TRADE_TP_PERCENT=0.05
TRADE_MARGIN_COIN=USDT
TRADE_SYMBOL_WHITELIST=
```

### `test_local.py`
```python
import json, time, requests
from dotenv import load_dotenv
import os
from datetime import datetime, timezone

load_dotenv()
TOKEN = os.getenv("WEBHOOK_SIGNATURE_SECRET")
API_KEY = os.getenv("WEBHOOK_API_KEY")
URL = "http://localhost:8000/webhook"

now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
now_ts = str(int(time.time()))

payload_dict = {
    "token": TOKEN,
    "api_key": API_KEY,
    "timestamp": now_ts,
    "payload": {
        "ticker": "BTCUSDT",
        "exchange": "BINANCE",
        "price": 65000.0,
        "time": now_iso,
        "action": "buy",
        "comment": "test"
    }
}

body_bytes = json.dumps(payload_dict, separators=(',', ':'), sort_keys=True).encode('utf-8')
headers = {"Content-Type": "application/json"}

r = requests.post(URL, data=body_bytes, headers=headers, timeout=(3, 10))
print(f"Status: {r.status_code}")
print(f"Response: {r.json()}")
```

---

## 10. AI Agent Instructions for Next Time
> "Create a FastAPI TradingView webhook receiver at `./tradingview_webhook`. Use Body Token validation (`token` in JSON). Handle TV's unquoted timestamp quirk by documenting quoted template. Implement Bitunix official API v1 client with double SHA256 signatures. Use DRY_RUN mode, clientId idempotency, placeholder rejection, Decimal ROUND_DOWN quantization, run_in_threadpool. Provide test endpoints for balance/contracts/trade. Write AGENTS.md with pitfalls."

---

## 11. Verification Checklist Before Live Trading
- [ ] `TRADE_DRY_RUN=true` → `curl /test/balance` → `{"status":"success","equity":...}`
- [ ] `curl /test/contracts?symbol=STXUSDT` → shows correct `min_qty` (200 for STXUSDT)
- [ ] `curl /test/trade` DRY RUN → check logged body has correct `qty`, `tpPrice`, `slPrice`
- [ ] `TRADE_DRY_RUN=false` → `curl /test/trade` → real order placed
- [ ] Bitunix web UI shows order with correct SL/TP
- [ ] TradingView test alert → webhook receives → order placed → 200 OK

---

*Updated: 2026-08-15 | Version 3.0.0*
# TradingView Webhook Receiver - AI Agent Development Guide

## Project Overview
A FastAPI webhook receiver for TradingView alerts with HMAC/Token validation, structured logging, and ngrok/Cloudflare tunnel support.

**Stack**: Python 3.11+, FastAPI, Uvicorn, Pydantic, python-dotenv, requests
**Port**: 8000
**Endpoint**: `POST /webhook`

---

## 1. Repository Structure
```
tradingview_webhook/
├── app.py                 # Main FastAPI application
├── test_local.py          # Local test script with HMAC/Token signing
├── requirements.txt       # Dependencies
├── .env                   # Local secrets (gitignored)
├── .env.example           # Template for secrets
└── AGENTS.md              # This file
```

---

## 2. Core Requirements
1. **POST /webhook** endpoint
2. **Validation**: Shared secret token in JSON body (`token` field) + optional `api_key`
3. **Logging**: Full request/response/error logs to console
4. **Response**: Always return 200 OK to prevent TradingView retries
5. **Tunneling**: ngrok (free) or Cloudflare Tunnel for public HTTPS URL
6. **TradingView Compatibility**: Handle TV's JSON quirks (unquoted timestamps in test mode)

---

## 3. Critical Implementation Details

### 3.1 Validation Strategy (Body Token)
**Why**: TradingView removed `Secret` field from UI. HMAC signature (`X-Signature` header) no longer works reliably.
**Solution**: Shared secret in JSON body `token` field.

**Server (`app.py`)**:
```python
EXPECTED_TOKEN = os.getenv("WEBHOOK_SIGNATURE_SECRET")
if data.get("token") != EXPECTED_TOKEN:
    raise HTTPException(403, "Invalid Token in Body")
```

**TradingView Message JSON**:
```json
{
  "token": "tv_sig_secret_aBcD1234EfGh5678",
  "api_key": "tv_api_key_7x3fGh9JkLq2",
  "timestamp": "{{timenow}}",
  "payload": { ... }
}
```

### 3.2 TradingView JSON Quirks (Major Pitfall)
**Problem**: TV test webhook sends `{{timenow}}` and `{{time}}` as **unquoted ISO strings** (invalid JSON).
```
"timestamp": 2026-08-14T17:20:11Z   // Invalid! Missing quotes
```
**Fix**: Force quotes in TV Message template:
```json
"timestamp": "{{timenow}}",   // Quoted!
"time": "{{time}}",           // Quoted!
```
Numbers (`{{close}}`, `{{high}}`) remain unquoted.

### 3.3 Content-Type Handling
TV sends `Content-Type: text/plain; charset=utf-8` even for JSON.
**Server**: Use `await request.body()` + `json.loads()` instead of `await request.json()`.

### 3.3 Logging Middleware (Essential for Debugging)
```python
@app.middleware("http")
async def log_requests(request: Request, call_next):
    body = await request.body()
    request.state.raw_body = body
    print(f"Headers: {dict(request.headers)}")
    print(f"Body: {body.decode('utf-8')}")
    # ... error handling ...
```

### 3.4 Always Return 200
```python
@app.post("/webhook", status_code=200, dependencies=[Depends(authenticate_webhook)])
async def receive_webhook(...):
    return {"status": "success"}
```
TradingView retries on non-2xx. Validation errors return 403 but **TV still sees 200** if exception handled? Actually FastAPI returns 403 status. To prevent retries, validation must happen *before* response status set? No, TV retries on non-200. So validation must not raise HTTPException? But we need to reject invalid. Compromise: Log error, return 200 with error message? But spec says reject. We'll keep 403 for security, accept TV retries.

---

## 4. Environment Variables (`.env`)
```env
WEBHOOK_API_KEY=tv_api_key_7x3fGh9JkLq2
WEBHOOK_SIGNATURE_SECRET=tv_sig_secret_aBcD1234EfGh5678
WEBHOOK_TIMESTAMP_TOLERANCE=300
```

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

### 5.2 Run Local Test
```bash
# Separate terminal
source venv/Scripts/activate
python test_local.py
```

### 5.3 Expose via Tunnel (ngrok)
```bash
ngrok http --host-header=localhost 0.0.0.0:8000
# Copy https://xxx.ngrok-free.app
```

### 5.4 TradingView Alert Setup
1. **Webhook URL**: `https://your-ngrok-url.ngrok-free.app/webhook`
2. **Secret**: Leave empty (TV UI removed/hid it)
3. **Message**: Use quoted timestamp template (see 3.2)

---

## 6. Common Pitfalls & Solutions

| Symptom | Cause | Solution |
|---------|-------|----------|
| `400 Bad Request` / `Expecting ',' delimiter` | TV test sends unquoted ISO timestamps | Quote `{{timenow}}` and `{{time}}` in TV Message |
| `403 Invalid Token` | `token` in JSON ≠ `.env` `WEBHOOK_SIGNATURE_SECRET` | Ensure TV Message `token` matches `.env` `WEBHOOK_SIGNATURE_SECRET` |
| `403 Invalid API Key` | `api_key` mismatch | Match TV Message `api_key` to `.env` `WEBHOOK_API_KEY` |
| `ngrok` shows no Forwarding URL | Local server not running on 8000 | Verify `curl http://localhost:8000/health` returns `{"status":"ok"}` |
| `ngrok` ERR_NGROK_8013 | Used `ngrok tcp` (requires credit card) | Use `ngrok http --host-header=localhost 0.0.0.0:8000` |
| `ngrok` no UI / no URL | Tunnel not established / firewall | Try `ngrok http --host-header=localhost 0.0.0.0:8000` or switch to Cloudflare Tunnel |
| TV test shows `action: "{{strategy.order.action}}"` | TV test sends literal template string | Normal. Real alerts send actual values (`buy`/`sell`) |
| `ModuleNotFoundError` / `py not found` | Python not in PATH / not installed | Install Python 3.11+ with "Add to PATH" checked. Use `py` launcher on Windows |

---

## 7. Production Deployment Checklist
- [ ] Replace ngrok with **Cloudflare Tunnel** (free, fixed URL, no card) or **Render/Railway** (auto HTTPS, Git push deploy)
- [ ] Set `WEBHOOK_API_KEY` and `WEBHOOK_SIGNATURE_SECRET` as **strong random secrets** (32+ chars)
- [ ] Disable `reload=True` in `uvicorn.run()`
- [ ] Add rate limiting (e.g., `slowapi`)
- [ ] Add persistent logging (file/ELK) and monitoring
- [ ] Store secrets in platform secret manager (not `.env`)

---

## 8. Quick Reference Commands
```bash
# Start server
py app.py

# Test locally
python test_local.py

# ngrok (HTTP only!)
ngrok http --host-header=localhost 0.0.0.0:8000

# Cloudflare Tunnel (fallback)
/c/cloudflared/cloudflared.exe tunnel --url http://localhost:8000

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
```
WEBHOOK_API_KEY=your_api_key_here
WEBHOOK_SIGNATURE_SECRET=your_super_secret_token_here
WEBHOOK_TIMESTAMP_TOLERANCE=300
```

### `test_local.py` (Body Token Version)
```python
import json, time, requests
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv("WEBHOOK_SIGNATURE_SECRET")
API_KEY = os.getenv("WEBHOOK_API_KEY")
URL = "http://localhost:8000/webhook"

payload_dict = {
    "token": TOKEN,
    "api_key": API_KEY,
    "timestamp": int(time.time()),
    "payload": {"ticker": "BTCUSDT", "price": 65000, "action": "buy"}
}
body_bytes = json.dumps(payload_dict, separators=(',', ':'), sort_keys=True).encode('utf-8')
headers = {"Content-Type": "application/json"}

r = requests.post(URL, data=body_bytes, headers=headers, timeout=(3, 10))
print(f"Status: {r.status_code}, Response: {r.json()}")
```

---

## 10. AI Agent Instructions for Next Time
> "Create a FastAPI TradingView webhook receiver at `./tradingview_webhook`. Use Body Token validation (`token` in JSON). Handle TV's unquoted timestamp quirk by documenting quoted template. Implement request logging middleware. Provide `test_local.py` for local testing. Use ngrok/Cloudflare for tunneling. Return 200 always. Write `AGENTS.md` with pitfalls."

---

*Generated from session: 2026-08-15*
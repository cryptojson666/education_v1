import json
import time
import hmac
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os

# 載入 .env
load_dotenv()

# 從環境變數讀取設定
EXPECTED_API_KEY = os.getenv("WEBHOOK_API_KEY")
EXPECTED_SIGNATURE_SECRET = os.getenv("WEBHOOK_SIGNATURE_SECRET")
TIMESTAMP_TOLERANCE = int(os.getenv("WEBHOOK_TIMESTAMP_TOLERANCE", "300"))

app = FastAPI(
    title="TradingView Webhook Receiver",
    description="接收 TradingView Alert Webhook，驗證 HMAC 簽名並列印內容",
    version="1.0.0"
)

# --- Middleware: 完整請求/錯誤日誌 ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    # 讀取 body 並保存以便後續使用
    body = await request.body()
    request.state.raw_body = body
    
    # 印出請求資訊
    print("\n" + "="*60)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 📥 Incoming Request")
    print(f"Method: {request.method} | Path: {request.url.path}")
    print(f"Headers: {dict(request.headers)}")
    try:
        print(f"Body: {body.decode('utf-8')}")
    except:
        print(f"Body (raw): {body}")

    try:
        response = await call_next(request)
        print(f"Response Status: {response.status_code}")
        print("="*60 + "\n")
        return response
    except Exception as e:
        print(f"❌ UNHANDLED EXCEPTION: {e}")
        print("="*60 + "\n")
        raise

# --- Pydantic Models (兼容 TradingView 任意結構) ---
class TradingViewPayload(BaseModel):
    class Config:
        extra = "allow"  # 允許 TV 送任何欄位

class WebhookRequest(BaseModel):
    api_key: str
    timestamp: int
    payload: TradingViewPayload

# --- 驗證邏輯 ---
async def authenticate_webhook(request: Request):
    # 從 middleware 取得 raw body
    body = request.state.raw_body
    
    # 1. 解析 JSON
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        print(f"❌ JSON Decode Error: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON Body")

    # 2. 驗證 Body 內的 token (對應 TV Message 裡的 "token")
    EXPECTED_TOKEN = os.getenv("WEBHOOK_SIGNATURE_SECRET")
    if data.get("token") != EXPECTED_TOKEN:
        print(f"❌ Token Mismatch. Expected: {EXPECTED_TOKEN}, Got: {data.get('token')}")
        raise HTTPException(status_code=403, detail="Invalid Token in Body")

    # 選擇性：驗證 api_key (若有帶)
    if data.get("api_key") != os.getenv("WEBHOOK_API_KEY"):
        print(f"❌ API Key Mismatch. Got: {data.get('api_key')}")
        raise HTTPException(status_code=403, detail="Invalid API Key")

    # 存入 state
    request.state.webhook_data = data
    return data

# --- API 端點 ---
@app.post(
    "/webhook",
    summary="接收 TradingView Webhook (返回 200 防止重送)",
    status_code=200,
    dependencies=[Depends(authenticate_webhook)]
)
async def receive_webhook(request: Request):
    data = request.state.webhook_data
    
    # 列印到 Console (美化輸出)
    print("\n" + "="*60)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ TV Webhook Verified")
    print("="*60)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("="*60 + "\n")

    return {"status": "success", "message": "Webhook processed"}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
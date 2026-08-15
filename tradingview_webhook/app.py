import json
import time
import hmac
import hashlib
import base64
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os
import requests

# 載入 .env
load_dotenv()

# 從環境變數讀取設定
WEBHOOK_API_KEY = os.getenv("WEBHOOK_API_KEY")
WEBHOOK_SIGNATURE_SECRET = os.getenv("WEBHOOK_SIGNATURE_SECRET")
WEBHOOK_TIMESTAMP_TOLERANCE = int(os.getenv("WEBHOOK_TIMESTAMP_TOLERANCE", "300"))

# Bitunix 設定
BITUNIX_API_KEY = os.getenv("BITUNIX_API_KEY")
BITUNIX_SECRET_KEY = os.getenv("BITUNIX_SECRET_KEY")
BITUNIX_PASSPHRASE = os.getenv("BITUNIX_PASSPHRASE", "")
BITUNIX_BASE_URL = os.getenv("BITUNIX_BASE_URL", "https://fapi.bitunix.com")

# 交易參數
TRADE_EQUITY_PERCENT = float(os.getenv("TRADE_EQUITY_PERCENT", "0.01"))
TRADE_LEVERAGE = int(os.getenv("TRADE_LEVERAGE", "10"))
TRADE_SL_PERCENT = float(os.getenv("TRADE_SL_PERCENT", "0.05"))
TRADE_TP_PERCENT = float(os.getenv("TRADE_TP_PERCENT", "0.05"))
TRADE_MARGIN_MODE = os.getenv("TRADE_MARGIN_MODE", "isolated")

app = FastAPI(
    title="TradingView Webhook -> Bitunix Auto Trade",
    description="TV Alert -> 驗證 -> Bitunix 自動下單 (1% Equity, 10x, SL/TP 5%)",
    version="2.1.0"
)

# ============================================================
# Bitunix API Client (依官方 Futures API v1 文檔)
# 參考: https://www.bitunix.com/api-docs/futures/common/introduction.html
# ============================================================
class BitunixClient:
    def __init__(self):
        self.api_key = BITUNIX_API_KEY
        self.secret_key = BITUNIX_SECRET_KEY
        self.passphrase = BITUNIX_PASSPHRASE
        self.base_url = BITUNIX_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Bitunix 簽名: HMAC SHA256 Base64(timestamp + method + requestPath + body)"""
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        mac = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method: str, request_path: str, body: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        sign = self._sign(timestamp, method, request_path, body)
        return {
            "Content-Type": "application/json",
            "API-KEY": self.api_key,
            "PASSPHRASE": self.passphrase,
            "TIMESTAMP": timestamp,
            "SIGN": sign,
        }

    def _request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        url = f"{self.base_url}{endpoint}"
        body = json.dumps(data) if data else ""
        headers = self._headers(method, endpoint, body)
        
        try:
            resp = self.session.request(method, url, headers=headers, params=params, data=body, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") != 0:
                raise Exception(f"Bitunix API Error: {result.get('msg')} (code: {result.get('code')})")
            return result.get("data", result)
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network Error: {e}")

    # ---------- Public API (依官方文檔) ----------
    def get_account_balance(self, margin_coin: str = "USDT") -> float:
        """獲取 USDT 可用餘額 (equity) - 嘗試多個可能端點"""
        endpoints = [
            "/api/v1/account/assets",
            "/api/v1/account/asset",
            "/api/v1/account/balance",
            "/api/v1/user/assets",
            "/api/v1/user/balance",
            "/api/v2/account/assets",
        ]
        
        for endpoint in endpoints:
            try:
                print(f"🔄 Trying balance endpoint: {endpoint}")
                data = self._request("GET", endpoint)
                # 解析回傳格式可能不同
                assets = data.get("assets") or data.get("list") or data.get("data") or data
                if isinstance(assets, dict):
                    assets = [assets]
                for asset in assets:
                    if asset.get("marginCoin") == margin_coin or asset.get("coin") == margin_coin or asset.get("currency") == margin_coin:
                        equity = asset.get("equity") or asset.get("available") or asset.get("balance") or asset.get("total")
                        if equity:
                            return float(equity)
            except Exception as e:
                print(f"⚠️ Balance endpoint {endpoint} failed: {e}")
                continue
        raise Exception("All balance endpoints failed")

    def get_ticker_price(self, symbol: str) -> float:
        """獲取最新標記價格 - GET /api/v1/market/ticker"""
        data = self._request("GET", "/api/v1/market/ticker", params={"symbol": symbol})
        tick = data.get("ticker") or data
        # Bitunix 回傳欄位通常為 lastPr (最新成交價) 或 markPrice
        return float(tick.get("lastPr") or tick.get("markPrice") or tick.get("lastPrice") or 0)

    def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "isolated", hold_side: str = "both") -> Dict:
        """設定槓桿 - POST /api/v1/account/set-leverage"""
        data = {
            "symbol": symbol,
            "leverage": str(leverage),
            "marginMode": margin_mode,
            "holdSide": hold_side  # "long", "short", "both" (雙向持倉用 both)
        }
        return self._request("POST", "/api/v1/account/set-leverage", data=data)

    def place_order(
        self,
        symbol: str,
        side: str,              # "open_long", "open_short", "close_long", "close_short"
        size: float,            # 合約張數
        leverage: int,
        tp_price: float = None,
        sl_price: float = None,
        margin_mode: str = "isolated",
        order_type: str = "market"
    ) -> Dict:
        """下單 - POST /api/v1/trade/order (官方文檔標準端點)"""
        data = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "size": str(size),
            "side": side,
            "orderType": order_type,
            "leverage": str(leverage),
            "marginMode": margin_mode,
        }
        if order_type == "limit":
            # 限價單需 price，這裡假設市價單
            pass
        
        if tp_price:
            data["presetTakeProfitPrice"] = str(tp_price)
        if sl_price:
            data["presetStopLossPrice"] = str(sl_price)
        
        return self._request("POST", "/api/v1/trade/order", data=data)

    def get_positions(self, symbol: str = None) -> List[Dict]:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/api/v1/position/current-positions", params=params)

    def get_contract_info(self, symbol: str) -> Dict:
        """查詢合約資訊 (面值、精度) - GET /api/v1/market/contracts"""
        data = self._request("GET", "/api/v1/market/contracts", params={"symbol": symbol})
        for c in data.get("contracts", []):
            if c.get("symbol") == symbol:
                return c
        return {}


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI(
    title="TradingView Webhook -> Bitunix Auto Trade",
    description="TV Alert -> 驗證 -> Bitunix 自動下單 (1% Equity, 10x, SL/TP 5%)",
    version="2.1.0"
)

bitunix = BitunixClient()

# --- Middleware: 完整請求/錯誤日誌 ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    body = await request.body()
    request.state.raw_body = body
    
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

# --- Pydantic Models ---
class TradingViewPayload(BaseModel):
    class Config:
        extra = "allow"

# --- 驗證邏輯 ---
async def authenticate_webhook(request: Request):
    body = request.state.raw_body
    
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        print(f"❌ JSON Decode Error: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON Body")

    if data.get("token") != WEBHOOK_SIGNATURE_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Token in Body")
    if data.get("api_key") != WEBHOOK_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

    request.state.webhook_data = data
    return data

# ============================================================
# 交易核心邏輯
# ============================================================
def calculate_order_params(payload: Dict) -> Dict:
    """根據 TV payload 計算下單參數"""
    tv_payload = payload.get("payload", {})
    symbol = tv_payload.get("ticker", "").replace(".P", "")  # 移除 .P 後綴
    action = tv_payload.get("action", "").lower()
    entry_price = float(tv_payload.get("price", 0))
    
    if not symbol or not action:
        raise ValueError("Missing symbol or action in payload")
    
    # 1. 獲取帳戶權益
    equity = bitunix.get_account_balance("USDT")
    if equity <= 0:
        raise Exception("USDT Equity is 0 or failed to fetch")
    
    # 2. 計算保證金 (1% Equity)
    margin = equity * TRADE_EQUITY_PERCENT
    
    # 3. 計算張數
    # 實際應查詢合約面值，這裡先用簡易公式: size = (margin * leverage) / entry_price
    # 實際建議：查詢 /api/v1/market/contracts 獲取 contractSize 與 sizePrecision
    notional = margin * TRADE_LEVERAGE
    size = notional / entry_price if entry_price > 0 else 0
    
    # 精度處理：先統一到 3 位小數，實際需依合約 precision 調整
    size = round(size, 3)
    if size <= 0:
        raise ValueError(f"Calculated size too small: {size}")
    
    # 3. 設定槓桿
    bitunix.set_leverage(symbol, TRADE_LEVERAGE, TRADE_MARGIN_MODE)
    
    # 4. 計算 SL/TP 價格
    if entry_price > 0:
        if action in ["buy", "long", "open_long"]:
            sl_price = round(entry_price * (1 - TRADE_SL_PERCENT), 4)
            tp_price = round(entry_price * (1 + TRADE_TP_PERCENT), 4)
            side = "open_long"
        elif action in ["sell", "short", "open_short"]:
            sl_price = round(entry_price * (1 + TRADE_SL_PERCENT), 4)
            tp_price = round(entry_price * (1 - TRADE_TP_PERCENT), 4)
            side = "open_short"
        else:
            raise ValueError(f"Unknown action: {action}")
    else:
        current_price = bitunix.get_ticker_price(symbol)
        if action in ["buy", "long", "open_long"]:
            sl_price = round(current_price * (1 - TRADE_SL_PERCENT), 4)
            tp_price = round(current_price * (1 + TRADE_TP_PERCENT), 4)
            side = "open_long"
        else:
            sl_price = round(current_price * (1 + TRADE_SL_PERCENT), 4)
            tp_price = round(current_price * (1 - TRADE_TP_PERCENT), 4)
            side = "open_short"
        entry_price = current_price

    return {
        "symbol": symbol,
        "side": side,
        "size": size,
        "leverage": TRADE_LEVERAGE,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "entry_price": entry_price,
        "margin": margin
    }

# --- API 端點 ---
@app.post(
    "/webhook",
    summary="接收 TradingView Webhook -> 自動下單 Bitunix",
    status_code=200,
)
async def receive_webhook(request: Request):
    try:
        data = await authenticate_webhook(request)
    except HTTPException as e:
        raise e

    payload = data
    tv_payload = payload.get("payload", {})
    
    print("\n" + "="*60)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ TV Webhook Verified")
    print("="*60)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("="*60 + "\n")

    # 檢查 action 是否為實際交易指令 (測試時會是 template string)
    action = tv_payload.get("action", "").lower()
    if action in ["{{strategy.order.action}}", "test", ""]:
        print("⚠️ Test/Dummy alert received, skipping trade execution.")
        return {"status": "success", "message": "Test alert received, no trade executed"}

    try:
        # 計算下單參數
        params = calculate_order_params(payload)
        
        print(f"🚀 Placing Order: {params['side']} {params['size']} {params['symbol']} @ {params['entry_price']}")
        print(f"   Leverage: {params['leverage']}x | Margin: {params['margin']} USDT")
        print(f"   SL: {params['sl_price']} | TP: {params['tp_price']}")

        # 執行下單
        result = bitunix.place_order(
            symbol=params["symbol"],
            side=params["side"],
            size=params["size"],
            leverage=params["leverage"],
            tp_price=params["tp_price"],
            sl_price=params["sl_price"],
            margin_mode=TRADE_MARGIN_MODE
        )
        
        print(f"✅ Order Success: {json.dumps(result, ensure_ascii=False)}")
        return {"status": "success", "message": "Order placed", "data": result}

    except Exception as e:
        print(f"❌ Trade Execution Failed: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/health")
async def health():
    return {"status": "ok"}

# ============================================================
# Local Test Helper
# ============================================================
@app.post("/test/trade")
async def test_trade(symbol: str = "BTCUSDT", side: str = "open_long", size: float = 0.001):
    """手動測試下單用"""
    try:
        bitunix.set_leverage(symbol, 10, "isolated")
        result = bitunix.place_order(symbol, side, size, 10, margin_mode="isolated")
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/test/balance")
async def test_balance():
    """測試查餘額"""
    try:
        equity = bitunix.get_account_balance("USDT")
        return {"status": "success", "equity": equity}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/test/debug/endpoints")
async def test_endpoints():
    """測試所有可能的端點"""
    results = {}
    endpoints = [
        "/api/v1/account/assets",
        "/api/v1/account/asset",
        "/api/v1/account/balance",
        "/api/v1/user/assets",
        "/api/v1/user/balance",
        "/api/v2/account/assets",
        "/api/v1/account/assets?marginCoin=USDT",
        "/api/v1/account/asset?marginCoin=USDT",
    ]
    
    for endpoint in endpoints:
        try:
            # 直接用 session 請求看原始回應
            timestamp = str(int(time.time() * 1000))
            sign = bitunix._sign(timestamp, "GET", endpoint, "")
            headers = {
                "Content-Type": "application/json",
                "API-KEY": bitunix.api_key,
                "PASSPHRASE": bitunix.passphrase,
                "TIMESTAMP": timestamp,
                "SIGN": sign,
            }
            url = f"{bitunix.base_url}{endpoint}"
            resp = bitunix.session.get(url, headers=headers, timeout=5)
            results[endpoint] = {"status": resp.status_code, "data": resp.json()}
        except Exception as e:
            results[endpoint] = {"error": str(e)}
    
    return results

@app.get("/test/ticker")
async def test_ticker(symbol: str = "BTCUSDT"):
    """測試查價格"""
    try:
        price = bitunix.get_ticker_price(symbol)
        return {"status": "success", "symbol": symbol, "price": price}
    except Exception as e:
        raise HTTPException(500, str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
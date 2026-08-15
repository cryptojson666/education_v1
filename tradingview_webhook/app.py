import json
import time
import hmac
import hashlib
import uuid
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

# Bitunix 設定 (參考官方 Demo)
BITUNIX_API_KEY = os.getenv("BITUNIX_API_KEY")
BITUNIX_SECRET_KEY = os.getenv("BITUNIX_SECRET_KEY")
BITUNIX_BASE_URL = os.getenv("BITUNIX_BASE_URL", "https://fapi.bitunix.com")

# 交易參數
TRADE_EQUITY_PERCENT = float(os.getenv("TRADE_EQUITY_PERCENT", "0.01"))
TRADE_LEVERAGE = int(os.getenv("TRADE_LEVERAGE", "10"))
TRADE_SL_PERCENT = float(os.getenv("TRADE_SL_PERCENT", "0.05"))
TRADE_TP_PERCENT = float(os.getenv("TRADE_TP_PERCENT", "0.05"))
TRADE_MARGIN_MODE = os.getenv("TRADE_MARGIN_MODE", "isolated")

app = FastAPI(
    title="TradingView Webhook -> Bitunix Auto Trade",
    description="TV Alert -> 驗證 -> Bitunix 自動下單 (官方 Demo 模式)",
    version="2.4.0"
)

# ============================================================
# Bitunix API Client (完全對齊官方 Python Demo)
# 參考: /c/Users/chungweng22/open-api/Demo/Python/
# ============================================================
class BitunixClient:
    def __init__(self):
        self.api_key = BITUNIX_API_KEY
        self.secret_key = BITUNIX_SECRET_KEY
        self.base_url = BITUNIX_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "language": "en-US"
        })

    # ========== 簽名算法 (完全對齊官方 open_api_http_sign.py) ==========
    def _nonce(self) -> str:
        """生成 32 位隨機字串 (uuid 去除連字號)"""
        return str(uuid.uuid4()).replace('-', '')

    def _timestamp(self) -> str:
        """毫秒級時間戳"""
        return str(int(time.time() * 1000))

    def _sort_params(self, params: Dict) -> str:
        """參數排序並拼接: k1v1k2v2... (官方 sort_params)"""
        if not params:
            return ""
        return ''.join(f"{k}{v}" for k, v in sorted(params.items()))

    def _generate_signature(self, method: str, endpoint: str, params: Dict = None, body: str = "") -> tuple:
        """生成簽名 (完全對齊官方 open_api_http_sign.py)
        
        簽名邏輯:
        1. nonce + timestamp + api_key + query_params + body
        2. SHA256 hex -> + secret_key -> SHA256 hex
        
        Args:
            method: HTTP method
            endpoint: API endpoint (不含 query string)
            params: GET 參數字典 (會排序拼接)
            body: JSON 字串 (POST body)
            
        Returns:
            (sign, nonce, timestamp)
        """
        nonce = str(uuid.uuid4()).replace('-', '')
        timestamp = str(int(time.time() * 1000))
        
        # 1. 處理 GET 參數: 排序後拼接 k1v1k2v2...
        query_params_str = self._sort_params(params) if params else ""
        
        # 2. Body 處理: 緊湊 JSON (無空格)
        body_str = body if body else ""
        
        # 3. 簽名輸入: nonce + timestamp + api_key + query_params + body
        digest_input = f"{nonce}{timestamp}{self.api_key}{query_params_str}{body_str}"
        digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
        
        # 4. 雙重 SHA256: digest + secret_key -> SHA256 hex
        sign_input = digest + self.secret_key
        sign = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()
        
        return sign, nonce, timestamp

    def _request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        """發送請求 (自動處理簽名)"""
        # 1. 準備 Body 字串 (緊湊 JSON)
        body_str = json.dumps(data, separators=(',', ':')) if data else ""
        
        # 2. 生成簽名 (自動處理 GET 參數排序)
        sign, nonce, timestamp = self._generate_signature(method, endpoint, params, body_str if method == "POST" else "")
        
        # 2. 請求 Headers
        headers = {
            "Content-Type": "application/json",
            "language": "en-US",
            "api-key": self.api_key,
            "sign": sign,
            "nonce": nonce,
            "timestamp": timestamp,
        }
        
        try:
            resp = self.session.request(
                method, 
                f"{self.base_url}{endpoint}", 
                headers=headers, 
                params=params,
                data=body_str if method == "POST" else None,
                timeout=10
            )
            print(f"📤 Request: {method} {endpoint}")
            print(f"📥 Response Status: {resp.status_code}")
            print(f"📥 Response Body: {resp.text}")
            
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") != 0:
                raise Exception(f"Bitunix API Error: {result.get('msg')} (code: {result.get('code')})")
            return result.get("data", result)
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network Error: {e}")

    # ---------- Public API (對齊官方 Demo) ----------
    def get_account_balance(self, margin_coin: str = "USDT") -> float:
        """獲取帳戶資產 - GET /api/v1/futures/account?marginCoin=USDT"""
        data = self._request("GET", "/api/v1/futures/account", params={"marginCoin": "USDT"})
        # 官方回傳格式: 直接物件 {marginCoin, available, equity, ...}
        if isinstance(data, dict):
            # 直接物件格式: {marginCoin: USDT, available: ..., equity: ...}
            if data.get("marginCoin") == margin_coin:
                equity = data.get("equity") or data.get("available") or data.get("balance")
                if equity:
                    return float(equity)
            # 或是包裹在 data 內
            if "data" in data and isinstance(data["data"], dict):
                return self.get_account_balance_from_dict(data["data"], margin_coin)
        elif isinstance(data, list):
            for asset in data:
                if asset.get("marginCoin") == margin_coin:
                    equity = asset.get("equity") or asset.get("available") or asset.get("balance")
                    if equity:
                        return float(equity)
        raise Exception(f"Unexpected balance response: {data}")
    
    def get_account_balance_from_dict(self, data: dict, margin_coin: str) -> float:
        if data.get("marginCoin") == margin_coin:
            equity = data.get("equity") or data.get("available") or data.get("balance")
            if equity:
                return float(equity)
        raise Exception(f"Balance not found in response")

    def get_ticker_price(self, symbol: str) -> float:
        """獲取最新價格 - GET /api/v1/futures/market/tickers"""
        data = self._request("GET", "/api/v1/futures/market/tickers", params={"symbols": symbol})
        if isinstance(data, dict) and "list" in data:
            for tick in data["list"]:
                if tick.get("symbol") == symbol:
                    return float(tick.get("lastPr") or tick.get("markPrice") or tick.get("lastPrice") or 0)
        return 0.0

    def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "isolated") -> Dict:
        """官方 Demo 無獨立設定槓桿端點，下單時帶 leverage 參數"""
        return {"success": True}

    def place_order(
        self,
        symbol: str,
        side: str,              # "BUY" / "SELL"
        size: float,
        leverage: int,
        tp_price: float = None,
        sl_price: float = None,
        margin_mode: str = "isolated",
        order_type: str = "MARKET"
    ) -> Dict:
        """下單 - POST /api/v1/futures/trade/place_order (官方 Demo 端點)"""
        data = {
            "symbol": symbol,
            "side": side,                    # "BUY" / "SELL"
            "orderType": "MARKET" if order_type == "market" else "LIMIT",
            "qty": str(size),                # 數量為字串
            "tradeSide": "OPEN",             # 開倉
            "effect": "GTC",                 # Good Till Cancelled
            "reduceOnly": False,
            "marginCoin": "USDT",            # 必填
            "leverage": str(leverage),       # 槓桿
            "marginMode": "ISOLATED" if margin_mode == "isolated" else "CROSSED",  # 大寫
        }
        
        return self._request("POST", "/api/v1/futures/trade/place_order", data=data)

    def get_positions(self, symbol: str = None) -> List[Dict]:
        return []

    def get_ticker_price_simple(self, symbol: str) -> float:
        """簡易獲取價格 - 公開端點無需簽名"""
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/futures/market/tickers", params={"symbols": symbol}, timeout=5)
            result = resp.json()
            if result.get("code") == 0 and "list" in result.get("data", {}):
                for tick in result["data"]["list"]:
                    if tick.get("symbol") == symbol:
                        return float(tick.get("lastPr") or tick.get("markPrice") or 0)
        except:
            pass
        return 0.0


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI(
    title="TradingView Webhook -> Bitunix Auto Trade",
    description="TV Alert -> 驗證 -> Bitunix 自動下單 (官方 Demo 模式)",
    version="2.4.0"
)

bitunix = BitunixClient()

# --- Middleware ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    body = await request.body()
    request.state.raw_body = body
    
    print("\n" + "="*60)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 📥 Incoming Request")
    print(f"Method: {request.method} | Path: {request.url.path}")
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
# 交易參數 (從環境變數讀取)
# ============================================================
TRADE_EQUITY_PERCENT = float(os.getenv("TRADE_EQUITY_PERCENT", "0.01"))
TRADE_LEVERAGE = int(os.getenv("TRADE_LEVERAGE", "10"))
TRADE_SL_PERCENT = float(os.getenv("TRADE_SL_PERCENT", "0.05"))
TRADE_TP_PERCENT = float(os.getenv("TRADE_TP_PERCENT", "0.05"))
TRADE_MARGIN_MODE = os.getenv("TRADE_MARGIN_MODE", "isolated")

# ============================================================
# 交易核心邏輯
# ============================================================
def calculate_order_params(payload: Dict) -> Dict:
    tv_payload = payload.get("payload", {})
    symbol = tv_payload.get("ticker", "").replace(".P", "")
    action = tv_payload.get("action", "").lower()
    entry_price = float(tv_payload.get("price", 0))
    
    if not symbol or not action:
        raise ValueError("Missing symbol or action in payload")
    
    equity = bitunix.get_account_balance("USDT")
    if equity <= 0:
        raise Exception("USDT Equity is 0 or failed to fetch")
    
    margin = equity * TRADE_EQUITY_PERCENT
    notional = margin * TRADE_LEVERAGE
    size = notional / entry_price if entry_price > 0 else 0
    size = round(size, 3)
    if size <= 0:
        raise ValueError(f"Calculated size too small: {size}")
    
    if entry_price > 0:
        if action in ["buy", "long", "open_long"]:
            sl_price = round(entry_price * (1 - TRADE_SL_PERCENT), 4)
            tp_price = round(entry_price * (1 + TRADE_TP_PERCENT), 4)
            side = "BUY"
        elif action in ["sell", "short", "open_short"]:
            sl_price = round(entry_price * (1 + TRADE_SL_PERCENT), 4)
            tp_price = round(entry_price * (1 - TRADE_TP_PERCENT), 4)
            side = "SELL"
        else:
            raise ValueError(f"Unknown action: {action}")
    else:
        current_price = bitunix.get_ticker_price(symbol)
        if action in ["buy", "long", "open_long"]:
            sl_price = round(current_price * (1 - TRADE_SL_PERCENT), 4)
            tp_price = round(current_price * (1 + TRADE_TP_PERCENT), 4)
            side = "BUY"
        else:
            sl_price = round(current_price * (1 + TRADE_SL_PERCENT), 4)
            tp_price = round(current_price * (1 - TRADE_TP_PERCENT), 4)
            side = "SELL"
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
@app.post("/webhook", status_code=200)
async def receive_webhook(request: Request):
    try:
        data = await authenticate_webhook(request)
    except HTTPException as e:
        raise e

    payload = data
    tv_payload = payload.get("payload", {})
    
    print("\n" + "="*60)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ TV Webhook Verified")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("="*60 + "\n")

    action = tv_payload.get("action", "").lower()
    if action in ["{{strategy.order.action}}", "test", ""]:
        print("⚠️ Test/Dummy alert received, skipping trade execution.")
        return {"status": "success", "message": "Test alert received, no trade executed"}

    try:
        params = calculate_order_params(payload)
        
        print(f"🚀 Placing Order: {params['side']} {params['size']} {params['symbol']} @ {params['entry_price']}")
        print(f"   Leverage: {params['leverage']}x | Margin: {params['margin']} USDT")
        print(f"   SL: {params['sl_price']} | TP: {params['tp_price']}")

        result = bitunix.place_order(
            symbol=params["symbol"],
            side=params["side"],
            size=params["size"],
            leverage=params["leverage"],
            tp_price=params["tp_price"],
            sl_price=params["sl_price"],
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
# 測試端點
# ============================================================
@app.get("/test/balance")
async def test_balance():
    try:
        equity = bitunix.get_account_balance("USDT")
        return {"status": "success", "equity": equity}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/test/ticker")
async def test_ticker(symbol: str = "BTCUSDT"):
    try:
        price = bitunix.get_ticker_price(symbol)
        return {"status": "success", "symbol": symbol, "price": price}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/test/trade")
async def test_trade(symbol: str = "BTCUSDT", side: str = "BUY", size: float = 0.001):
    try:
        result = bitunix.place_order(symbol, side, size, 10)
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(500, str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
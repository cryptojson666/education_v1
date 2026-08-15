import json
import time
import hmac
import hashlib
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
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
TRADE_FIXED_USDT = float(os.getenv("TRADE_FIXED_USDT", "1.0"))      # 固定 1 USDT
TRADE_LEVERAGE = int(os.getenv("TRADE_LEVERAGE", "10"))
TRADE_SL_PERCENT = float(os.getenv("TRADE_SL_PERCENT", "0.05"))
TRADE_TP_PERCENT = float(os.getenv("TRADE_TP_PERCENT", "0.05"))
TRADE_MARGIN_MODE = os.getenv("TRADE_MARGIN_MODE", "isolated")
TRADE_USE_FIXED_USDT = os.getenv("TRADE_USE_FIXED_USDT", "true").lower() == "true"  # true=固定USDT, false=百分比

app = FastAPI(
    title="TradingView Webhook -> Bitunix Auto Trade",
    description="TV Alert -> 驗證 -> Bitunix 自動下單 (官方 Demo 模式)",
    version="2.5.0"
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
        self._symbol_precision_cache: Dict[str, Dict] = {}

    # ========== 簽名算法 (完全對齊官方 open_api_http_sign.py) ==========
    def _nonce(self) -> str:
        return str(uuid.uuid4()).replace('-', '')

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _sort_params(self, params: Dict) -> str:
        if not params:
            return ""
        return ''.join(f"{k}{v}" for k, v in sorted(params.items()))

    def _generate_signature(self, method: str, endpoint: str, params: Dict = None, body: str = "") -> tuple:
        nonce = str(uuid.uuid4()).replace('-', '')
        timestamp = str(int(time.time() * 1000))
        
        query_params_str = self._sort_params(params) if params else ""
        body_str = body if body else ""
        
        digest_input = f"{nonce}{timestamp}{self.api_key}{query_params_str}{body_str}"
        digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
        
        sign_input = digest + self.secret_key
        sign = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()
        
        return sign, nonce, timestamp

    def _request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        body_str = json.dumps(data, separators=(',', ':')) if data else ""
        
        sign, nonce, timestamp = self._generate_signature(method, endpoint, params, body_str if method == "POST" else "")
        
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

    # ---------- 合約資訊快取 ----------
def _load_contracts_info(self):
        """快取所有合約精度資訊"""
        try:
            # 嘗試多個可能的端點
            endpoints = [
                "/api/v1/futures/market/contracts",
                "/api/v1/futures/market/trading_pairs",
                "/api/v1/futures/market/symbols",
                "/api/v1/market/contracts",
                "/api/v1/futures/market/contracts/list",
            ]
            for endpoint in endpoints:
                try:
                    data = self._request("GET", endpoint)
                    contracts = data.get("list", []) or data.get("contracts", []) or data.get("data", []) or data
                    if isinstance(contracts, list) and contracts:
                        for c in contracts:
                            sym = c.get("symbol")
                            if sym:
                                self._symbol_precision_cache[sym] = {
                                    "size_precision": int(c.get("sizePrecision", 3)),
                                    "price_precision": int(c.get("pricePrecision", 4)),
                                    "min_qty": float(c.get("minQty", 0) or c.get("minQty", 0) or c.get("minOrderQty", 0)),
                                }
                        if self._symbol_precision_cache:
                            print(f"✅ Loaded {len(self._symbol_precision_cache)} contracts from {endpoint}")
                            return
                except Exception as e:
                    continue
        except Exception as e:
            print(f"⚠️ Failed to load contracts info: {e}")
        
        # Fallback: 常見幣種最小量 (若 API 端點不可用)
        self._symbol_precision_cache.update({
            "BTCUSDT": {"size_precision": 3, "price_precision": 1, "min_qty": 0.001},
            "ETHUSDT": {"size_precision": 3, "price_precision": 2, "min_qty": 0.01},
            "STXUSDT": {"size_precision": 0, "price_precision": 4, "min_qty": 200},
            "SOLUSDT": {"size_precision": 2, "price_precision": 3, "min_qty": 0.1},
            "DOGEUSDT": {"size_precision": 0, "price_precision": 5, "min_qty": 100},
            "XRPUSDT": {"size_precision": 1, "price_precision": 4, "min_qty": 10},
            "ADAUSDT": {"size_precision": 1, "price_precision": 4, "min_qty": 10},
            "MATICUSDT": {"size_precision": 1, "price_precision": 4, "min_qty": 10},
            "AVAXUSDT": {"size_precision": 2, "price_precision": 3, "min_qty": 0.1},
            "DOTUSDT": {"size_precision": 1, "price_precision": 3, "min_qty": 1},
            "LINKUSDT": {"size_precision": 2, "price_precision": 3, "min_qty": 0.1},
            "LTCUSDT": {"size_precision": 3, "price_precision": 2, "min_qty": 0.01},
            "BCHUSDT": {"size_precision": 3, "price_precision": 2, "min_qty": 0.01},
            "UNIUSDT": {"size_precision": 2, "price_precision": 3, "min_qty": 0.1},
            "ATOMUSDT": {"size_precision": 2, "price_precision": 3, "min_qty": 0.1},
            "ETCUSDT": {"size_precision": 2, "price_precision": 3, "min_qty": 0.1},
            "FILUSDT": {"size_precision": 2, "price_precision": 3, "min_qty": 0.1},
            "TRXUSDT": {"size_precision": 0, "price_precision": 5, "min_qty": 100},
            "XRPUSDT": {"size_precision": 1, "price_precision": 4, "min_qty": 10},
        })

    def _get_symbol_precision(self, symbol: str) -> Dict:
        if not self._symbol_precision_cache:
            self._load_contracts_info()
        return self._symbol_precision_cache.get(symbol, {"size_precision": 3, "price_precision": 4, "min_qty": 0.001})

def _quantize_size(self, symbol: str, size: float) -> str:
        """根據合約精度量化 size，強制檢查 min_qty"""
        prec = self._get_symbol_precision(symbol)
        step = 10 ** (-prec["size_precision"])
        quantized = round(size / step) * step
        min_qty = prec.get("min_qty", 0)
        if quantized < min_qty:
            raise ValueError(f"Size {quantized} below min_qty {min_qty} for {symbol} (need at least {min_qty})")
        return f"{quantized:.{prec['size_precision']}f}"

    def _clean_symbol(self, symbol: str) -> str:
        """清理 symbol: 移除交易所前綴、.P 後綴"""
        # BITUNIX:BTCUSDT.P -> BTCUSDT
        if ":" in symbol:
            symbol = symbol.split(":")[-1]
        if symbol.endswith(".P"):
            symbol = symbol[:-2]
        return symbol

    # ========== 簽名算法 ==========
    def _nonce(self) -> str:
        return str(uuid.uuid4()).replace('-', '')

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _sort_params(self, params: Dict) -> str:
        if not params:
            return ""
        return ''.join(f"{k}{v}" for k, v in sorted(params.items()))

    def _generate_signature(self, method: str, endpoint: str, params: Dict = None, body: str = "") -> tuple:
        nonce = str(uuid.uuid4()).replace('-', '')
        timestamp = str(int(time.time() * 1000))
        
        query_params_str = self._sort_params(params) if params else ""
        body_str = body if body else ""
        
        digest_input = f"{nonce}{timestamp}{self.api_key}{query_params_str}{body_str}"
        digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
        
        sign_input = digest + self.secret_key
        sign = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()
        
        return sign, nonce, timestamp

    def _request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        body_str = json.dumps(data, separators=(',', ':')) if data else ""
        
        sign, nonce, timestamp = self._generate_signature(method, endpoint, params, body_str if method == "POST" else "")
        
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

    # ---------- Public API ----------
    def get_account_balance(self, margin_coin: str = "USDT") -> float:
        data = self._request("GET", "/api/v1/futures/account", params={"marginCoin": "USDT"})
        if isinstance(data, dict) and "list" in data:
            for asset in data["list"]:
                if asset.get("marginCoin") == "USDT":
                    return float(asset.get("equity", 0) or asset.get("available", 0))
        elif isinstance(data, list):
            for asset in data:
                if asset.get("marginCoin") == "USDT":
                    return float(asset.get("equity", 0) or asset.get("available", 0))
        elif isinstance(data, dict) and data.get("marginCoin") == "USDT":
            return float(data.get("equity", 0) or data.get("available", 0))
        raise Exception(f"Unexpected balance response: {data}")

    def get_ticker_price(self, symbol: str) -> float:
        data = self._request("GET", "/api/v1/futures/market/tickers", params={"symbols": symbol})
        if isinstance(data, dict) and "list" in data:
            for tick in data["list"]:
                if tick.get("symbol") == symbol:
                    return float(tick.get("lastPr") or tick.get("markPrice") or tick.get("lastPrice") or 0)
        return 0.0

    def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "isolated") -> Dict:
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
        # 統一 order_type 大小寫
        order_type = (order_type or "MARKET").upper()
        
        # 量化 size 精度
        symbol = self._clean_symbol(symbol)
        qty_str = self._quantize_size(symbol, size)
        
        data = {
            "symbol": symbol,
            "side": side,                    # "BUY" / "SELL"
            "orderType": order_type,         # MARKET / LIMIT
            "qty": qty_str,                  # 使用量化後的 size
            "tradeSide": "OPEN",             # 開倉
            "reduceOnly": False,
        }
        
        if order_type == "LIMIT":
            # LIMIT 必填 price
            pass  # 由上層邏輯處理
        
        # TP/SL (官方參數名: tpPrice, slPrice, tpStopType, slStopType 等)
        if tp_price:
            data["tpPrice"] = str(round(tp_price, 4))
            data["tpStopType"] = "LAST_PRICE"
            data["tpOrderType"] = "MARKET"
        if sl_price:
            data["slPrice"] = str(round(sl_price, 4))
            data["slStopType"] = "LAST_PRICE"
            data["slOrderType"] = "MARKET"
        
        return self._request("POST", "/api/v1/futures/trade/place_order", data=data)

    def get_ticker_price(self, symbol: str) -> float:
        data = self._request("GET", "/api/v1/futures/market/tickers", params={"symbols": symbol})
        if isinstance(data, dict) and "list" in data:
            for tick in data["list"]:
                if tick.get("symbol") == symbol:
                    return float(tick.get("lastPr") or tick.get("markPrice") or tick.get("lastPrice") or 0)
        return 0.0

    def get_ticker_price_simple(self, symbol: str) -> float:
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
    version="2.6.0"
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
    model_config = ConfigDict(extra="allow")

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
# 交易參數
# ============================================================
TRADE_EQUITY_PERCENT = float(os.getenv("TRADE_EQUITY_PERCENT", "0.01"))
TRADE_FIXED_USDT = float(os.getenv("TRADE_FIXED_USDT", "1.0"))      # 固定 1 USDT
TRADE_LEVERAGE = int(os.getenv("TRADE_LEVERAGE", "10"))
TRADE_SL_PERCENT = float(os.getenv("TRADE_SL_PERCENT", "0.05"))
TRADE_TP_PERCENT = float(os.getenv("TRADE_TP_PERCENT", "0.05"))
TRADE_MARGIN_MODE = os.getenv("TRADE_MARGIN_MODE", "isolated")
TRADE_USE_FIXED_USDT = os.getenv("TRADE_USE_FIXED_USDT", "true").lower() == "true"  # true=固定USDT, false=百分比

# ============================================================
# 交易核心邏輯
# ============================================================
def calculate_order_params(payload: Dict) -> Dict:
    tv_payload = payload.get("payload", {})
    symbol = bitunix._clean_symbol(tv_payload.get("ticker", ""))
    action = tv_payload.get("action", "").lower()
    entry_price = float(tv_payload.get("price", 0))
    
    if not symbol or not action:
        raise ValueError("Missing symbol or action in payload")
    
    # 計算下單張數：固定 USDT 或百分比
    if TRADE_USE_FIXED_USDT:
        margin = TRADE_FIXED_USDT  # 固定 1 USDT
    else:
        equity = bitunix.get_account_balance("USDT")
        if equity <= 0:
            raise Exception("USDT Equity is 0 or failed to fetch")
        margin = equity * TRADE_EQUITY_PERCENT
    
    notional = margin * TRADE_LEVERAGE
    size = notional / entry_price if entry_price > 0 else 0
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
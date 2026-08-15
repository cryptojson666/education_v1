"""
TradingView Webhook -> Bitunix 合約自動下單

流程: TradingView Alert -> 驗證 -> 計算倉位 -> Bitunix place_order

環境變數 (.env):
    # Webhook 驗證
    WEBHOOK_API_KEY=tv_api_key_xxx
    WEBHOOK_SIGNATURE_SECRET=tv_sig_secret_xxx

    # Bitunix API
    BITUNIX_API_KEY=xxx
    BITUNIX_SECRET_KEY=xxx
    BITUNIX_BASE_URL=https://fapi.bitunix.com

    # 交易設定
    TRADE_DRY_RUN=true              # true=只印不下單，第一次跑務必開著
    TRADE_USE_FIXED_USDT=true       # true=固定保證金, false=權益百分比
    TRADE_FIXED_USDT=1.0
    TRADE_EQUITY_PERCENT=0.01
    TRADE_LEVERAGE=10
    TRADE_MARGIN_MODE=ISOLATION     # ISOLATION / CROSS
    TRADE_POSITION_MODE=oneway      # oneway / hedge
    TRADE_SL_PERCENT=0.05
    TRADE_TP_PERCENT=0.05
    TRADE_SYMBOL_WHITELIST=         # 逗號分隔，留空=不限制
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tv2bitunix")


# ============================================================
# 設定
# ============================================================
def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        log.warning("環境變數 %s 不是合法數字，使用預設值 %s", key, default)
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        log.warning("環境變數 %s 不是合法整數，使用預設值 %s", key, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "y")


class Settings:
    # Webhook
    webhook_api_key = _env_str("WEBHOOK_API_KEY")
    webhook_secret = _env_str("WEBHOOK_SIGNATURE_SECRET")

    # Bitunix
    bitunix_api_key = _env_str("BITUNIX_API_KEY")
    bitunix_secret_key = _env_str("BITUNIX_SECRET_KEY")
    bitunix_base_url = _env_str("BITUNIX_BASE_URL", "https://fapi.bitunix.com").rstrip("/")

    # 交易
    dry_run = _env_bool("TRADE_DRY_RUN", True)
    use_fixed_usdt = _env_bool("TRADE_USE_FIXED_USDT", True)
    fixed_usdt = _env_float("TRADE_FIXED_USDT", 1.0)
    equity_percent = _env_float("TRADE_EQUITY_PERCENT", 0.01)
    leverage = _env_int("TRADE_LEVERAGE", 10)
    margin_mode = _env_str("TRADE_MARGIN_MODE", "ISOLATION").upper()
    position_mode = _env_str("TRADE_POSITION_MODE", "oneway").lower()
    sl_percent = _env_float("TRADE_SL_PERCENT", 0.05)
    tp_percent = _env_float("TRADE_TP_PERCENT", 0.05)
    margin_coin = _env_str("TRADE_MARGIN_COIN", "USDT")

    whitelist = {
        s.strip().upper()
        for s in _env_str("TRADE_SYMBOL_WHITELIST").split(",")
        if s.strip()
    }

    @classmethod
    def validate(cls) -> List[str]:
        problems = []
        if not cls.webhook_api_key:
            problems.append("WEBHOOK_API_KEY 未設定")
        if not cls.webhook_secret:
            problems.append("WEBHOOK_SIGNATURE_SECRET 未設定")
        if not cls.bitunix_api_key:
            problems.append("BITUNIX_API_KEY 未設定")
        if not cls.bitunix_secret_key:
            problems.append("BITUNIX_SECRET_KEY 未設定")
        if cls.position_mode not in ("oneway", "hedge"):
            problems.append(f"TRADE_POSITION_MODE 只能是 oneway / hedge，目前是 {cls.position_mode}")
        if not 0 < cls.equity_percent <= 1:
            problems.append("TRADE_EQUITY_PERCENT 應在 0 與 1 之間")
        if cls.leverage < 1:
            problems.append("TRADE_LEVERAGE 至少為 1")
        return problems


settings = Settings()


# ============================================================
# 例外
# ============================================================
class BitunixError(Exception):
    """Bitunix 回傳非 0 的 code。"""

    def __init__(self, code: Any, msg: str, raw: Any = None):
        self.code = str(code)
        self.msg = msg
        self.raw = raw
        super().__init__(f"Bitunix API Error {code}: {msg}")


class BitunixNetworkError(Exception):
    pass


# 已知需要特別處理的錯誤碼
CODE_DUPLICATE_CLIENT_ID = "30042"


# ============================================================
# Bitunix Client
# ============================================================
class BitunixClient:
    """Bitunix 合約 API 用戶端。

    簽名規則 (官方文件):
        digest = SHA256(nonce + timestamp + api_key + queryParams + body)
        sign   = SHA256(digest + secret_key)

    queryParams 為 GET 參數依 key 昇冪排序後以 k1v1k2v2 拼接。
    body 為壓縮過的 JSON 字串，且送出的 bytes 必須與簽名用的字串完全一致。
    """

    def __init__(self) -> None:
        self.api_key = settings.bitunix_api_key
        self.secret_key = settings.bitunix_secret_key
        self.base_url = settings.bitunix_base_url

        self.session = requests.Session()
        self.session.headers.update({"language": "en-US"})

        self._precision_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

    # ---------- 簽名 ----------
    @staticmethod
    def _nonce() -> str:
        return uuid.uuid4().hex  # 32 字元

    @staticmethod
    def _timestamp() -> str:
        return str(int(time.time() * 1000))

    @staticmethod
    def _sort_params(params: Optional[Dict]) -> str:
        if not params:
            return ""
        return "".join(f"{k}{v}" for k, v in sorted(params.items()))

    def _sign(self, query_params: str, body: str) -> tuple:
        nonce = self._nonce()
        timestamp = self._timestamp()

        digest = hashlib.sha256(
            f"{nonce}{timestamp}{self.api_key}{query_params}{body}".encode("utf-8")
        ).hexdigest()
        sign = hashlib.sha256((digest + self.secret_key).encode("utf-8")).hexdigest()

        return sign, nonce, timestamp

    # ---------- 請求 ----------
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        signed: bool = True,
    ) -> Any:
        method = method.upper()

        # body 必須是壓縮 JSON，且簽名與送出使用「同一個字串」
        body_str = json.dumps(data, separators=(",", ":")) if data else ""

        headers = {"Content-Type": "application/json"}
        if signed:
            query_str = self._sort_params(params)
            sign, nonce, timestamp = self._sign(query_str, body_str)
            headers.update(
                {
                    "api-key": self.api_key,
                    "sign": sign,
                    "nonce": nonce,
                    "timestamp": timestamp,
                }
            )

        url = f"{self.base_url}{endpoint}"
        log.info("→ %s %s params=%s body=%s", method, endpoint, params, body_str or "-")

        try:
            resp = self.session.request(
                method,
                url,
                headers=headers,
                params=params,
                data=body_str.encode("utf-8") if body_str else None,
                timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            raise BitunixNetworkError(f"連線失敗: {exc}") from exc

        log.info("← HTTP %s %s", resp.status_code, resp.text[:800])

        try:
            result = resp.json()
        except ValueError:
            raise BitunixNetworkError(
                f"非 JSON 回應 (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        code = str(result.get("code", ""))
        if code not in ("0", "00000"):
            raise BitunixError(code, result.get("msg", "未知錯誤"), result)

        return result.get("data", result)

    # ---------- Symbol 處理 ----------
    @staticmethod
    def clean_symbol(symbol: str) -> str:
        """BITUNIX:BTCUSDT.P -> BTCUSDT"""
        symbol = (symbol or "").strip().upper()
        if ":" in symbol:
            symbol = symbol.split(":")[-1]
        for suffix in (".P", "PERP", ".PERP"):
            if symbol.endswith(suffix):
                symbol = symbol[: -len(suffix)]
        return symbol.strip()

    def _load_contracts(self) -> None:
        """載入所有交易對的精度資訊。欄位名稱在不同版本可能不同，故做多重 fallback。"""

        def pick(d: Dict, keys: List[str], default):
            for k in keys:
                if d.get(k) not in (None, ""):
                    return d[k]
            return default

        try:
            data = self._request(
                "GET", "/api/v1/futures/market/trading_pairs", signed=False
            )
        except Exception as exc:
            log.warning("載入合約資訊失敗，將使用預設精度: %s", exc)
            return

        rows: List[Dict] = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("list", "contracts", "data", "symbols"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break

        cache = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol")
            if not sym:
                continue
            cache[sym.upper()] = {
                "size_precision": int(
                    pick(row, ["basePrecision", "sizePrecision", "volumePrecision"], 3)
                ),
                "price_precision": int(
                    pick(row, ["quotePrecision", "pricePrecision"], 4)
                ),
                "min_qty": float(
                    pick(row, ["minTradeVolume", "minQty", "minVolume"], 0)
                ),
            }

        if cache:
            self._precision_cache = cache
            log.info("已快取 %d 個交易對的精度資訊", len(cache))
        else:
            log.warning("合約資訊回應中找不到可用的交易對清單: %s", str(data)[:300])

    def get_precision(self, symbol: str) -> Dict[str, Any]:
        with self._cache_lock:
            if not self._precision_cache:
                self._load_contracts()
        return self._precision_cache.get(
            symbol.upper(),
            {"size_precision": 3, "price_precision": 4, "min_qty": 0.0},
        )

    def quantize_qty(self, symbol: str, size: float) -> str:
        """依交易對精度量化下單數量。無條件捨去，避免超出預期風險。"""
        prec = self.get_precision(symbol)
        digits = prec["size_precision"]
        step = Decimal(1).scaleb(-digits)

        qty = Decimal(str(size)).quantize(step, rounding=ROUND_DOWN)

        min_qty = Decimal(str(prec.get("min_qty", 0)))
        if min_qty > 0 and qty < min_qty:
            raise ValueError(
                f"{symbol} 計算數量 {qty} 低於最小下單量 {min_qty}，請提高保證金或槓桿"
            )
        if qty <= 0:
            raise ValueError(f"{symbol} 計算數量量化後為 0 (原始值 {size})")

        return f"{qty:.{digits}f}"

    def quantize_price(self, symbol: str, price: float) -> str:
        prec = self.get_precision(symbol)
        digits = prec["price_precision"]
        step = Decimal(1).scaleb(-digits)
        value = Decimal(str(price)).quantize(step, rounding=ROUND_HALF_UP)
        return f"{value:.{digits}f}"

    # ---------- 帳戶 ----------
    def get_account_balance(self, margin_coin: str = "USDT") -> float:
        data = self._request(
            "GET", "/api/v1/futures/account", params={"marginCoin": margin_coin}
        )

        def extract(asset: Dict) -> Optional[float]:
            if asset.get("marginCoin", margin_coin) != margin_coin:
                return None
            for key in ("equity", "available", "balance"):
                if asset.get(key) not in (None, ""):
                    return float(asset[key])
            return None

        candidates: List[Dict] = []
        if isinstance(data, dict):
            if isinstance(data.get("list"), list):
                candidates = data["list"]
            else:
                candidates = [data]
        elif isinstance(data, list):
            candidates = data

        for asset in candidates:
            if isinstance(asset, dict):
                value = extract(asset)
                if value is not None:
                    return value

        raise BitunixError("PARSE", f"無法解析帳戶餘額回應: {str(data)[:300]}")

    def get_ticker_price(self, symbol: str) -> float:
        data = self._request(
            "GET",
            "/api/v1/futures/market/tickers",
            params={"symbols": symbol},
            signed=False,
        )

        rows = data.get("list", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return 0.0

        for tick in rows:
            if not isinstance(tick, dict) or tick.get("symbol") != symbol:
                continue
            for key in ("lastPrice", "lastPr", "last", "markPrice"):
                if tick.get(key) not in (None, ""):
                    return float(tick[key])
        return 0.0

    def set_leverage(self, symbol: str, leverage: int, margin_coin: str = "USDT") -> None:
        """設定槓桿。失敗只警告不中斷 — 帳戶可能已是目標值，或已有持倉不允許變更。"""
        try:
            self._request(
                "POST",
                "/api/v1/futures/account/change_leverage",
                data={
                    "symbol": symbol,
                    "marginCoin": margin_coin,
                    "leverage": str(leverage),
                },
            )
            log.info("已設定 %s 槓桿為 %sx", symbol, leverage)
        except Exception as exc:
            log.warning("設定槓桿失敗 (%s)，沿用帳戶目前設定: %s", symbol, exc)

    def set_margin_mode(self, symbol: str, mode: str, margin_coin: str = "USDT") -> None:
        try:
            self._request(
                "POST",
                "/api/v1/futures/account/change_margin_mode",
                data={
                    "symbol": symbol,
                    "marginCoin": margin_coin,
                    "marginMode": mode.upper(),
                },
            )
            log.info("已設定 %s 保證金模式為 %s", symbol, mode)
        except Exception as exc:
            log.warning("設定保證金模式失敗 (%s): %s", symbol, exc)

    # ---------- 下單 ----------
    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        client_id: Optional[str] = None,
        reduce_only: bool = False,
    ) -> Dict:
        symbol = self.clean_symbol(symbol)
        order_type = (order_type or "MARKET").upper()
        side = side.upper()

        if side not in ("BUY", "SELL"):
            raise ValueError(f"side 只能是 BUY / SELL，收到 {side}")
        if order_type not in ("MARKET", "LIMIT"):
            raise ValueError(f"orderType 只能是 MARKET / LIMIT，收到 {order_type}")

        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": self.quantize_qty(symbol, qty),
            "reduceOnly": reduce_only,
        }

        # tradeSide 僅在雙向持倉模式需要；單向模式帶了反而會被拒
        if settings.position_mode == "hedge":
            body["tradeSide"] = "OPEN"

        if order_type == "LIMIT":
            if price is None:
                raise ValueError("LIMIT 單必須提供 price")
            body["price"] = self.quantize_price(symbol, price)
            body["effect"] = "GTC"

        if tp_price:
            body["tpPrice"] = self.quantize_price(symbol, tp_price)
            body["tpStopType"] = "LAST_PRICE"
            body["tpOrderType"] = "MARKET"

        if sl_price:
            body["slPrice"] = self.quantize_price(symbol, sl_price)
            body["slStopType"] = "LAST_PRICE"
            body["slOrderType"] = "MARKET"

        if client_id:
            body["clientId"] = client_id

        if settings.dry_run:
            log.warning("[DRY RUN] 未實際送出，body = %s", json.dumps(body))
            return {"dryRun": True, "body": body}

        try:
            return self._request(
                "POST", "/api/v1/futures/trade/place_order", data=body
            )
        except BitunixError as exc:
            if exc.code == CODE_DUPLICATE_CLIENT_ID:
                log.warning("clientId %s 重複，視為已處理過的訊號", client_id)
                return {"duplicate": True, "clientId": client_id}
            raise


bitunix = BitunixClient()


# ============================================================
# FastAPI
# ============================================================
app = FastAPI(
    title="TradingView Webhook -> Bitunix Auto Trade",
    description="TradingView Alert 轉 Bitunix 合約自動下單",
    version="3.0.0",
)


@app.on_event("startup")
async def on_startup() -> None:
    problems = settings.validate()
    if problems:
        for p in problems:
            log.error("設定問題: %s", p)
        log.error("請修正 .env 後重新啟動")
    if settings.dry_run:
        log.warning("=" * 55)
        log.warning("DRY RUN 模式啟用中 — 不會送出任何真實訂單")
        log.warning("確認無誤後在 .env 設定 TRADE_DRY_RUN=false")
        log.warning("=" * 55)

    await run_in_threadpool(bitunix.get_precision, "BTCUSDT")


@app.middleware("http")
async def capture_body(request: Request, call_next):
    request.state.raw_body = await request.body()
    return await call_next(request)


# ---------- 驗證 ----------
def authenticate(raw_body: bytes) -> Dict:
    if b"{{" in raw_body:
        raise HTTPException(
            status_code=400,
            detail="Payload 含有未被替換的 TradingView 佔位符，已拒絕執行",
        )

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON 格式錯誤: {exc}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Payload 必須是 JSON 物件")

    token = str(data.get("token", ""))
    api_key = str(data.get("api_key", ""))

    if not hmac.compare_digest(token, settings.webhook_secret):
        raise HTTPException(status_code=403, detail="Token 驗證失敗")
    if not hmac.compare_digest(api_key, settings.webhook_api_key):
        raise HTTPException(status_code=403, detail="API Key 驗證失敗")

    return data


def make_client_id(payload: Dict) -> str:
    """用訊號內容產生穩定 ID，讓 TradingView 重送時不會重複開倉。"""
    tv = payload.get("payload", {})
    seed = "|".join(
        str(tv.get(k, ""))
        for k in ("ticker", "action", "time", "price")
    )
    return "tv" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


# ---------- 倉位計算 ----------
BUY_ACTIONS = {"buy", "long", "open_long"}
SELL_ACTIONS = {"sell", "short", "open_short"}


def calculate_order(payload: Dict) -> Dict:
    tv = payload.get("payload", {})

    symbol = bitunix.clean_symbol(tv.get("ticker", ""))
    action = str(tv.get("action", "")).strip().lower()

    if not symbol:
        raise ValueError("payload.ticker 缺失")
    if not action:
        raise ValueError("payload.action 缺失")

    if settings.whitelist and symbol not in settings.whitelist:
        raise ValueError(f"{symbol} 不在白名單 {sorted(settings.whitelist)} 中")

    if action in BUY_ACTIONS:
        side = "BUY"
    elif action in SELL_ACTIONS:
        side = "SELL"
    else:
        raise ValueError(f"無法辨識的 action: {action}")

    try:
        entry_price = float(tv.get("price", 0) or 0)
    except (TypeError, ValueError):
        entry_price = 0.0

    if entry_price <= 0:
        entry_price = bitunix.get_ticker_price(symbol)
    if entry_price <= 0:
        raise ValueError(f"無法取得 {symbol} 的參考價格")

    # 保證金
    if settings.use_fixed_usdt:
        margin = settings.fixed_usdt
    else:
        equity = bitunix.get_account_balance(settings.margin_coin)
        if equity <= 0:
            raise ValueError(f"{settings.margin_coin} 權益為 0")
        margin = equity * settings.equity_percent

    qty = (margin * settings.leverage) / entry_price
    if qty <= 0:
        raise ValueError(f"計算出的數量無效: {qty}")

    direction = 1 if side == "BUY" else -1
    tp_price = entry_price * (1 + direction * settings.tp_percent)
    sl_price = entry_price * (1 - direction * settings.sl_percent)

    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry_price": entry_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "margin": margin,
    }


def execute_trade(payload: Dict) -> Dict:
    """同步執行 — 由 run_in_threadpool 呼叫，避免阻塞 event loop。"""
    order = calculate_order(payload)

    log.info(
        "準備下單: %s %s 約 %.8f @ %.6f | 保證金 %.4f %s | 槓桿 %sx",
        order["side"], order["symbol"], order["qty"], order["entry_price"],
        order["margin"], settings.margin_coin, settings.leverage,
    )
    log.info("TP %.6f | SL %.6f", order["tp_price"], order["sl_price"])

    if not settings.dry_run:
        bitunix.set_margin_mode(order["symbol"], settings.margin_mode, settings.margin_coin)
        bitunix.set_leverage(order["symbol"], settings.leverage, settings.margin_coin)

    return bitunix.place_order(
        symbol=order["symbol"],
        side=order["side"],
        qty=order["qty"],
        order_type="MARKET",
        tp_price=order["tp_price"],
        sl_price=order["sl_price"],
        client_id=make_client_id(payload),
    )


# ---------- 端點 ----------
@app.post("/webhook")
async def webhook(request: Request):
    data = authenticate(request.state.raw_body)

    log.info("收到已驗證訊號:\n%s", json.dumps(data, indent=2, ensure_ascii=False))

    action = str(data.get("payload", {}).get("action", "")).strip().lower()
    if action in ("", "test", "ping"):
        return {"status": "ignored", "message": "測試訊號，未執行交易"}

    try:
        result = await run_in_threadpool(execute_trade, data)
        log.info("下單完成: %s", json.dumps(result, ensure_ascii=False))
        return {"status": "success", "data": result}

    except ValueError as exc:
        log.error("參數錯誤: %s", exc)
        return {"status": "rejected", "message": str(exc)}
    except BitunixError as exc:
        log.error("Bitunix 拒絕: code=%s msg=%s", exc.code, exc.msg)
        return {"status": "error", "code": exc.code, "message": exc.msg}
    except Exception as exc:
        log.exception("下單流程發生未預期錯誤")
        return {"status": "error", "message": str(exc)}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "dry_run": settings.dry_run,
        "position_mode": settings.position_mode,
        "leverage": settings.leverage,
        "config_problems": settings.validate(),
        "time": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/test/balance")
async def test_balance():
    try:
        equity = await run_in_threadpool(
            bitunix.get_account_balance, settings.margin_coin
        )
        return {"status": "success", "coin": settings.margin_coin, "equity": equity}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/test/ticker")
async def test_ticker(symbol: str = "BTCUSDT"):
    try:
        price = await run_in_threadpool(bitunix.get_ticker_price, symbol)
        return {"status": "success", "symbol": symbol, "price": price}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/test/contracts")
async def test_contracts(symbol: Optional[str] = None):
    """檢查精度快取 — 排查數量/價格被拒時的第一站。"""
    await run_in_threadpool(bitunix.get_precision, "BTCUSDT")
    if symbol:
        sym = bitunix.clean_symbol(symbol)
        return {"symbol": sym, "precision": bitunix.get_precision(sym)}
    return {
        "cached": len(bitunix._precision_cache),
        "sample": dict(list(bitunix._precision_cache.items())[:5]),
    }


@app.post("/test/trade")
async def test_trade(symbol: str = "BTCUSDT", side: str = "BUY", qty: float = 0.001):
    try:
        result = await run_in_threadpool(
            bitunix.place_order, symbol, side, qty
        )
        return {"status": "success", "data": result}
    except Exception as exc:
        raise HTTPException(500, str(exc))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)

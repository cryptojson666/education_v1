# test_local.py
import json, time, requests
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv("WEBHOOK_SIGNATURE_SECRET")
API_KEY = os.getenv("WEBHOOK_API_KEY")
URL = "http://localhost:8000/webhook"

# 模擬 TradingView 真實 Payload 結構
payload_dict = {
    "token": TOKEN,
    "api_key": API_KEY,
    "timestamp": "{{timenow}}",  # 字串格式，TV 會自動替換
    "payload": {
        "ticker": "BTCUSDT",
        "exchange": "BINANCE",
        "price": 65000.0,
        "time": "{{time}}",
        "action": "buy",  # 實際測試時請改為 buy/sell，而非模板字串
        "comment": "test"
    }
}

# 序列化
body_bytes = json.dumps(payload_dict, separators=(',', ':'), sort_keys=True).encode('utf-8')
headers = {"Content-Type": "application/json"}

print("Sending test request (Body Token)...")
print(f"Body: {body_bytes.decode()}")

r = requests.post(URL, data=body_bytes, headers=headers, timeout=(3, 10))
print(f"Status: {r.status_code}")
print(f"Response: {r.json()}")
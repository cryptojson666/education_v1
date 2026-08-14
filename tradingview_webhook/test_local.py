# test_local.py
import json, time, requests
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv("WEBHOOK_SIGNATURE_SECRET")  # 當作共享密鑰
API_KEY = os.getenv("WEBHOOK_API_KEY")
URL = "http://localhost:8000/webhook"

# 1. 構建 Body (含 token 欄位)
payload_dict = {
    "token": TOKEN,
    "api_key": API_KEY,
    "timestamp": int(time.time()),
    "payload": {"ticker": "BTCUSDT", "price": 65000, "action": "buy"}
}

# 2. 序列化
body_bytes = json.dumps(payload_dict, separators=(',', ':'), sort_keys=True).encode('utf-8')

headers = {
    "Content-Type": "application/json",
    # 不再需要 X-Signature, X-Timestamp, X-API-Key Header
}

print("Sending test request (Body Token)...")
print(f"Body: {body_bytes.decode()}")

r = requests.post(URL, data=body_bytes, headers=headers, timeout=(3, 10))
print(f"Status: {r.status_code}")
print(f"Response: {r.json()}")
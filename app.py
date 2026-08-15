# app.py

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import json

app = FastAPI()

# Expected validation keys and values (replace with actual values)
EXPECTED_VALIDATION = {
    "api_key": "your_api_key_here",
    "signature": "your_signature_here",
    "timestamp": "your_timestamp_here"
}

class WebhookData(BaseModel):
    payload: dict

@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        data = await request.json()
        # Verify keys and values
        for key, expected_value in EXPECTED_VALIDATION.items():
            if data.get(key) != expected_value:
                raise HTTPException(status_code=403, detail=f"Validation failed for key: {key}")
        # If valid, print to console
        print(json.dumps(data, indent=2))
        return {"status": "success", "message": "Data received and validated"}
except Exception as e:
    return {"status": "error", "message": str(e)}
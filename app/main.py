import os
import csv
import json
import threading
import traceback
from datetime import datetime, date
from functools import lru_cache
from typing import Optional, Dict, Any

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse

from tvlogger import get_logger
log = get_logger("main")
from dhanhq import dhanhq
from dotenv import load_dotenv
# Load .env BEFORE anything else
ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(ENV_PATH)

# ---------------------- CONFIG (use env vars in production) ----------------------
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")
DHAN_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
TV_WEBHOOK_SECRET = os.environ.get("TV_WEBHOOK_SECRET", "mageshtv2025")
INSTRUMENTS_CSV_URL = os.environ.get(
    "INSTRUMENTS_CSV_URL",
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
)
INSTRUMENTS_LOCAL = os.environ.get("INSTRUMENTS_LOCAL", "dhan_instruments_detailed.csv")
STRIKE_STEP_DEFAULT = int(os.environ.get("STRIKE_STEP_DEFAULT", "50"))
STATE_FILE = os.environ.get("STATE_FILE", "tv_bridge_state.json")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8589661497:AAHkKYPlDBk63psDqtGIbAJXB7QmObGscm8")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "726033937")
DEBUG = str(os.environ.get("DEBUG", "false")).lower() in ("1", "true", "yes")
# --------------------------------------------------------------------------------

app = FastAPI()
lock = threading.Lock()

# ------------------------ Symbol normalization -------------------
MCX_SYMBOL_MAP = {
    "CRUDEOILM": "CRUDEOIL",
    "GOLDM": "GOLD",
    "SILVERM": "SILVER",
    "COPPERM": "COPPER",
}

dhan = dhanhq(DHAN_CLIENT_ID,DHAN_ACCESS_TOKEN)

def normalize_symbol(tv_symbol: str) -> str:
    if not tv_symbol:
        return tv_symbol
    s = tv_symbol.strip().upper()
    for suf in ("1!", "2!", "3!"):
        if s.endswith(suf):
            s = s[:-len(suf)]
            break
    if ":" in s:
        s = s.split(":", 1)[1]
    if s in MCX_SYMBOL_MAP:
        return MCX_SYMBOL_MAP[s]
    for k, v in MCX_SYMBOL_MAP.items():
        if s == k:
            return v
    return s
# --------------------------------------------------------------------------------

# ------------------------ Utilities: Instruments & State ------------------------
def download_instruments_csv(local_path=INSTRUMENTS_LOCAL):
    log.info("Downloading instruments CSV from %s", INSTRUMENTS_CSV_URL)
    r = requests.get(INSTRUMENTS_CSV_URL, timeout=20)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(r.content)
    log.info("Saved instruments CSV -> %s", local_path)

@lru_cache(maxsize=1)
def load_instruments(local_path=INSTRUMENTS_LOCAL):
    if not os.path.exists(local_path):
        download_instruments_csv(local_path)
    rows = []
    with open(local_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    log.debug("Loaded %d instrument rows", len(rows))
    return rows

def parse_date_try(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except Exception:
            continue
    return None

def get_nearest_expiry_for_underlying(underlying="NIFTY"):
    rows = load_instruments()
    today = datetime.utcnow().date()
    expiries = set()
    for r in rows:
        ts = (r.get("UNDERLYING_SYMBOL")).upper()
        if underlying.upper() not in ts:
            continue
        exp = parse_date_try(r.get("SM_EXPIRY_DATE"))
        if exp and exp >= today:
            expiries.add(exp)
    if not expiries:
        return None
    return min(expiries)

def find_option_row(underlying="NIFTY", expiry: date = None, strike: int = None, option_type: str = "CE"):
    rows = load_instruments()
    for r in rows:
        ts = (r.get("UNDERLYING_SYMBOL")).upper()
        if underlying.upper() not in ts:
            continue
        if option_type.upper() not in ts and (r.get("OPTION_TYPE") or "").upper() != option_type.upper():
            continue
        r_strike = r.get("STRIKE_PRICE")
        try:
            if strike is not None and r_strike is not None and int(float(r_strike)) != int(strike):
                continue
        except Exception:
            pass
        r_exp = parse_date_try(r.get("SM_EXPIRY_DATE"))
        if expiry and r_exp and r_exp != expiry:
            continue
        return r
    return None

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, "r"))
        except Exception:
            log.exception("Failed to load state file; starting fresh.")
    return {"open_leg": None, "processed_alert_ids": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ------------------------ Trading logic --------------------------------------
def compute_itm1_strike(spot: float, step: int, intent: str):
    spot = float(spot)
    step = int(step)
    floor = (int(spot) // step) * step
    ceil = floor if floor == int(spot) else floor + step
    if int(spot) == floor:
        return floor - step if intent == "CE" else floor + step
    else:
        return floor if intent == "CE" else ceil

def quantity_for_instrument_row(row: Dict[str, Any], lots: int = 1):
    for k in "LOT_SIZE":
        if row.get(k):
            try:
                return int(row[k]) * lots
            except Exception:
                pass
    return int(lots)

def notify_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, data=data, timeout=5)
    except Exception:
        log.exception("Telegram notify failed")

# ------------------------ Webhook endpoint ------------------------------------
@app.post("/webhook")
async def webhook(request: Request):
    DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")
    DHAN_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
    dhan = dhanhq(DHAN_CLIENT_ID,DHAN_ACCESS_TOKEN)
    body = await request.json()
    log.debug("Webhook payload: %s", body)
    tv_secret = body.get("secret", "")
    if tv_secret != TV_WEBHOOK_SECRET:
        log.warning("Invalid TV secret in payload")
        raise HTTPException(status_code=403, detail="invalid secret")
    signal = (body.get("signal")).strip()
    incoming_symbol = (body.get("symbol") or "NIFTY")
    symbol = normalize_symbol(incoming_symbol)
    spot = body.get("spot")
    alert_id = body.get("alert_id") or body.get("id") or None
    if not signal or spot is None:
        return JSONResponse({"error": "missing signal or spot"}, status_code=400)
    with lock:
        state = load_state()
        if alert_id and alert_id in state.get("processed_alert_ids", []):
            log.debug("Duplicate alert_id received: %s -> ignoring", alert_id)
            return JSONResponse({"status": "duplicate_alert_ignored"}, status_code=200)
    try:
        spot = float(spot)
    except:
        return JSONResponse({"error": "invalid spot value"}, status_code=400)
    expiry = get_nearest_expiry_for_underlying(symbol)
    if not expiry:
        return JSONResponse({"error": "no expiry found for underlying", "symbol_checked": symbol, "incoming_symbol": incoming_symbol}, status_code=500)
    strike_step = STRIKE_STEP_DEFAULT
    response_payload = {"signal":signal, "symbol":symbol, "spot":spot, "expiry":str(expiry)}

    with lock:
        state = load_state()
        open_leg = state.get("open_leg")

        def close_leg(leg):
            try:
                sid = leg["security_id"]
                qty = leg.get("quantity",75)
                log.debug("place_order_on_dhan with sid : %s , type : SELL, quantity: %s", sid, qty)
                sellorder = dhan.place_order(security_id=sid,
                                             exchange_segment=dhan.NSE_FNO,
                                             transaction_type=dhan.SELL,
                                             quantity=75,
                                             order_type=dhan.MARKET,
                                             product_type=dhan.MARGIN,
                                             price=0)
                log.debug("Closed leg %s -> order: %s", leg, sellorder)
                notify_telegram(f"Closed {leg.get('type')} {leg.get('strike')} {leg.get('expiry')}: {sellorder}")
                return sellorder
            except Exception:
                log.exception("Failed to close leg")
                notify_telegram(f"Failed to close leg: {leg}")
                raise

        def open_leg_buy(option_type):
            strike = compute_itm1_strike(spot, strike_step, option_type)
            row = find_option_row(underlying=symbol, expiry=expiry, strike=strike, option_type=option_type)
            if not row:
                raise RuntimeError(f"Instrument not found for {symbol} {option_type} strike {strike} exp {expiry}")
            sid = row.get("SECURITY_ID")
            qty = quantity_for_instrument_row(row, lots=1)
            log.debug("place_order_on_dhan with sid : %s , type : BUY, quantity: %s", sid, qty)
            order = dhan.place_order(security_id=sid,
                                     exchange_segment=dhan.NSE_FNO,
                                     transaction_type=dhan.BUY,
                                     quantity=75,
                                     order_type=dhan.MARKET,
                                     product_type=dhan.MARGIN,
                                     price=0)
            new_leg = {"type": option_type, "strike": int(strike), "expiry": str(expiry), "security_id": sid, "quantity":qty, "order": order}
            state["open_leg"] = new_leg
            save_state(state)
            notify_telegram(f"Opened {option_type} {strike} {expiry}: {order}")
            log.debug("Opened leg: %s", new_leg)
            return new_leg

        try:
            sig = signal.strip().lower()
            if sig == "smart buy":
                if open_leg and open_leg.get("type") == "PE":
                    close_leg(open_leg)
                    state["open_leg"] = None
                new_leg = open_leg_buy("CE")
                response_payload.update({"result":"bought CE", "leg": new_leg})
            elif sig == "smart sell":
                if open_leg and open_leg.get("type") == "CE":
                    close_leg(open_leg)
                    state["open_leg"] = None
                new_leg = open_leg_buy("PE")
                response_payload.update({"result":"bought PE", "leg": new_leg})
            elif sig == "book profit":
                if not open_leg:
                    response_payload.update({"result":"no open leg"})
                else:
                    order = close_leg(open_leg)
                    state["open_leg"] = None
                    save_state(state)
                    response_payload.update({"result":"closed leg", "order": order})
            else:
                return JSONResponse({"error": "unknown signal"}, status_code=400)

            if alert_id:
                state.setdefault("processed_alert_ids", []).append(alert_id)
                state["processed_alert_ids"] = state["processed_alert_ids"][-200:]
                save_state(state)

            return JSONResponse(response_payload)
        except Exception as e:
            log.exception("Error handling signal")
            notify_telegram(f"Error in webhook handling: {str(e)}")
            return JSONResponse({"error":"internal", "details": str(e)}), 500


NGROK_API = "http://ngrok:4040/api/tunnels"

@app.get("/api/test-dhan")
def test_dhan_connection():
    try:
        DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")
        DHAN_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
        dhan = dhanhq(DHAN_CLIENT_ID,DHAN_ACCESS_TOKEN)
        # Try a simple API call such as getting fund limits or order list.
        res = dhan.get_order_list()

        return {
            "status": "success",
            "message": "DHAN connection successful!",
            "details": res
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "DHAN connection failed!",
            "error": str(e)
        }

@app.get("/get-ngrok-url")
def get_ngrok_url():
    try:
        # Call ngrok API
        resp = requests.get(NGROK_API)
        resp.raise_for_status()
        tunnels = resp.json().get("tunnels", [])

        if not tunnels:
            log.warning("No ngrok tunnels found")
            return {"error": "No ngrok tunnels available"}

        public_url = tunnels[0]["public_url"]

        return {"public_url": public_url}

    except Exception as e:
        log.exception("Failed to get NGROK URL: %s", e)
        return {"error": str(e)}

# ------------------------ Health check --------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "time": str(datetime.utcnow())}

@app.on_event("startup")
async def startup_event():
    log.info("Application is starting")
    load_instruments()
#----------------------------UI changes-------------------------------
@app.get("/api/token")
def read_dhan_token():
    token = os.environ.get("DHAN_ACCESS_TOKEN", "")
    return {"token": token}

@app.post("/api/token/update")
async def update_dhan_token(request: Request):
    try:
        
        form = await request.form()
        new_token = form.get("token")

        if not new_token:
            return JSONResponse({"error": "Token cannot be empty"}, status_code=400)

        update_env_variable("DHAN_ACCESS_TOKEN", new_token)

        # Update running environment also
        os.environ["DHAN_ACCESS_TOKEN"] = new_token
        load_dotenv(override=True)
        return RedirectResponse("/settings", status_code=303)
    except Exception as e:
        print("\n======= ERROR IN UPDATE TOKEN =======")
        traceback.print_exc()
        print("=====================================\n")
        raise
def update_env_variable(key: str, value: str):
    """Updates or adds a key=value pair inside the .env file."""
    lines = []
    found = False

    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        if line.startswith(key + "="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)

@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    token = os.environ.get("DHAN_ACCESS_TOKEN", "")

    # DO NOT USE f-string. Use plain triple quotes.
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>DHAN Token Manager</title>

        <link rel="stylesheet"
              href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    </head>

    <body class="bg-light">
        <div class="container mt-5">
            <div class="card shadow p-4">
                <h2 class="mb-4">DHAN Access Token Manager</h2>

                <!-- Current Token -->
                <div class="mb-3">
                    <label class="form-label fw-bold">Current Token:</label>
                    <textarea class="form-control" rows="3" readonly>__TOKEN__</textarea>
                </div>

                <!-- Update Form -->
                <form action="/api/token/update" method="POST">
                    <label class="form-label fw-bold">Update Token:</label>
                    <textarea name="token" class="form-control" rows="3"
                              placeholder="Enter new DHAN access token" required></textarea>

                    <button class="btn btn-primary mt-3" type="submit">
                        Update Token
                    </button>
                </form>

                <hr class="my-4">

                <!-- Test DHAN Button -->
                <button class="btn btn-success" onclick="testDhan()">
                    Test DHAN Connection
                </button>
                
            </div>
        </div>

        <!-- Modal -->
        <div class="modal fade" id="resultModal" tabindex="-1">
          <div class="modal-dialog">
            <div class="modal-content">
              <div class="modal-header">
                <h5 class="modal-title">DHAN Connection Test</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body" id="modalBody">Loading...</div>
              <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
              </div>
            </div>
          </div>
        </div>

        <script>
          function testDhan() {
            fetch("/api/test-dhan")
              .then(res => res.json())
              .then(data => {
                const modalBody = document.getElementById("modalBody");

                if (data.status === "success") {
                  modalBody.innerHTML = `
                    <div class='alert alert-success'>
                      <strong>${JSON.stringify(data.details, null, 2)}</strong>
                    </div>
                  `;
                } else {
                  modalBody.innerHTML = `
                    <div class='alert alert-danger'>
                      <strong>Failed!</strong> ${data.message}<br>
                      Error: ${data.error}
                    </div>
                  `;
                }

                const modal = new bootstrap.Modal(document.getElementById("resultModal"));
                modal.show();
              });
          }
          
        </script>

    </body>
    </html>
    """

    # Insert the token safely
    html = html.replace("__TOKEN__", token)

    return HTMLResponse(content=html)

# ------------------------ Main entry point ------------------------------------
if __name__ == "__main__":
    import uvicorn
    try:
        load_instruments()
    except Exception:
        log.exception("Instrument load failed at startup")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        reload=True
    )
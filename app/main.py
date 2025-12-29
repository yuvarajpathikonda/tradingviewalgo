import os
import csv
import json
import threading
import traceback
from datetime import datetime, date
from functools import lru_cache
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

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
STRIKE_STEP_DEFAULT = int(os.environ.get("STRIKE_STEP"))
STATE_FILE = os.environ.get("STATE_FILE", "tv_bridge_state.json")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# --------------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Application is starting")
    load_instruments()
    yield

app = FastAPI(lifespan=lifespan)
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

def get_nearest_expiry_for_underlying(underlying, n: int = 0):
    rows = load_instruments()
    today = datetime.utcnow().date()
    expiries = set()
    for r in rows:
        ts = (r.get("UNDERLYING_SYMBOL")).upper()
        if underlying.upper() != ts:
            continue
        exp = parse_date_try(r.get("SM_EXPIRY_DATE"))
        if exp and exp >= today:
            expiries.add(exp)
    if not expiries:
        return None
    
    sorted_expiries = sorted(expiries)
    return sorted_expiries[n]

def find_option_row(underlying="NIFTY", expiry: date = None, strike: int = None, option_type: str = "CE"):
    rows = load_instruments()
    for r in rows:
        ts = (r.get("UNDERLYING_SYMBOL")).upper()
        if underlying.upper() != ts:
            continue
        if option_type.upper() != ts and (r.get("OPTION_TYPE") or "").upper() != option_type.upper():
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
def quantity_for_instrument_row(row: Dict[str, Any], lots: int = 1):
    if lots is None:
        lots = int(os.environ.get("LOTS", "1"))
        
    lot_size = int(float(row.get("LOT_SIZE")))
    return lot_size * lots

def notify_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, data=data, timeout=5)
    except Exception:
        log.exception("Telegram notify failed")

def compute_atm_strike(spot: float, step: int) -> int:
    spot = float(spot)
    step = int(step)
    return round(spot / step) * step


def _direction(intent: str) -> int:
    intent = intent.upper()
    if intent == "CE":
        return 1
    elif intent == "PE":
        return -1
    else:
        raise ValueError("intent must be 'CE' or 'PE'")


def compute_itm_strike(spot: float, step: int, intent: str, depth: int) -> int:
    atm = compute_atm_strike(spot, step)
    d = _direction(intent)
    return atm - (d * step * depth)

def compute_otm_strike(spot: float, step: int, intent: str, depth: int) -> int:
    atm = compute_atm_strike(spot, step)
    d = _direction(intent)
    return atm + (d * step * depth)

def compute_strike_by_type(
    spot: float,
    step: int,
    intent: str,
    strike_type: str
) -> int:
    strike_type = strike_type.upper()

    if strike_type == "ATM":
        return compute_atm_strike(spot, step)

    if strike_type == "ITM1":
        return compute_itm_strike(spot, step, intent,1)

    if strike_type == "ITM2":
        return compute_itm_strike(spot, step, intent,2)

    if strike_type == "OTM1":
        return compute_otm_strike(spot, step, intent,1)

    if strike_type == "OTM2":
        return compute_otm_strike(spot, step, intent,2)

    raise ValueError(f"Invalid strike type: {strike_type}")

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
    incoming_symbol = (body.get("symbol"))
    symbol = normalize_symbol(incoming_symbol)
    spot = body.get("spot")
    alert_id = body.get("alert_id") or body.get("id") or None
    EXPIRY_INDEX = int(os.environ.get("EXPIRY_INDEX", "0"))
    CE_STRIKE_TYPE = os.environ.get("CE_STRIKE_TYPE", "ITM1")
    PE_STRIKE_TYPE = os.environ.get("PE_STRIKE_TYPE", "ITM1")
    STRIKE_STEP_DEFAULT = int(os.environ.get("STRIKE_STEP"))
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
    expiry = get_nearest_expiry_for_underlying(symbol,EXPIRY_INDEX)
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
                qty = leg.get("quantity")
                log.debug("place_order_on_dhan with sid : %s , type : SELL, quantity: %s", sid, qty)
                sellorder = "123456"
                # sellorder = dhan.place_order(security_id=sid,
                #                              exchange_segment=dhan.NSE_FNO,
                #                              transaction_type=dhan.SELL,
                #                              quantity=qty,
                #                              order_type=dhan.MARKET,
                #                              product_type=dhan.MARGIN,
                #                              price=0)
                log.debug("Closed leg %s -> order: %s", leg, sellorder)
                notify_telegram(f"Closed {leg.get('type')} {leg.get('strike')} {leg.get('strike_type')} {leg.get('expiry')}: {sellorder}")
                return sellorder
            except Exception:
                log.exception("Failed to close leg")
                notify_telegram(f"Failed to close leg: {leg}")
                raise

        def open_leg_buy(option_type):
            strike_type = CE_STRIKE_TYPE if option_type == "CE" else PE_STRIKE_TYPE

            strike = compute_strike_by_type(
                spot=spot,
                step=strike_step,
                intent=option_type,
                strike_type=strike_type
            )
            row = find_option_row(underlying=symbol, expiry=expiry, strike=strike, option_type=option_type)
            if not row:
                raise RuntimeError(f"Instrument not found for {symbol} {option_type} strike {strike} exp {expiry}")
            sid = row.get("SECURITY_ID")
            qty = quantity_for_instrument_row(row, lots=1)
            log.debug("place_order_on_dhan with sid : %s , type : BUY, quantity: %s", sid, qty)
            # order = dhan.place_order(security_id=sid,
            #                          exchange_segment=dhan.NSE_FNO,
            #                          transaction_type=dhan.BUY,
            #                          quantity=qty,
            #                          order_type=dhan.MARKET,
            #                          product_type=dhan.MARGIN,
            #                          price=0)
            order = "654321"
            new_leg = {"type": option_type, "strike": int(strike), "strike_type": strike_type, "expiry": str(expiry), "security_id": sid, "quantity": qty, "order": order}
            state["open_leg"] = new_leg
            save_state(state)
            notify_telegram(f"Opened {option_type} {strike} {strike_type} {expiry}: {order}")
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
    return {"status": "Somi server is up", "time": str(datetime.utcnow())}

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
        expiry_index = form.get("expiry_index", "0")

        if not new_token:
            return JSONResponse({"error": "Token cannot be empty"}, status_code=400)
        if expiry_index not in ("0", "1", "2"):
            return JSONResponse(
                {"error": "Expiry index must be 0, 1 or 2"},
                status_code=400
            )
        if new_token:
            update_env_variable("DHAN_ACCESS_TOKEN", new_token)
            os.environ["DHAN_ACCESS_TOKEN"] = new_token

        update_env_variable("EXPIRY_INDEX", expiry_index)
        os.environ["EXPIRY_INDEX"] = expiry_index
        
        ce_strike_type = form.get("ce_strike_type")
        pe_strike_type = form.get("pe_strike_type")

        update_env_variable("CE_STRIKE_TYPE", ce_strike_type)
        update_env_variable("PE_STRIKE_TYPE", pe_strike_type)

        os.environ["CE_STRIKE_TYPE"] = ce_strike_type
        os.environ["PE_STRIKE_TYPE"] = pe_strike_type
        strike_step = form.get("strike_step", "50")

        if not strike_step.isdigit() or int(strike_step) <= 0:
            return JSONResponse(
                {"error": "Invalid strike step"},
                status_code=400
            )

        update_env_variable("STRIKE_STEP", strike_step)
        os.environ["STRIKE_STEP"] = strike_step
        lots = form.get("lots", "1")
        if not lots.isdigit() or int(lots) <= 0:
            return JSONResponse(
                {"error": "Invalid lots value"},
                status_code=400
            )
        update_env_variable("LOTS", lots)
        os.environ["LOTS"] = lots  
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
    expiry_index = os.environ.get("EXPIRY_INDEX", "0")
    lots = os.environ.get("LOTS")
    ce_strike_type = os.environ.get("CE_STRIKE_TYPE", "ITM1")
    pe_strike_type = os.environ.get("PE_STRIKE_TYPE", "ITM1")
    strike_step = os.environ.get("STRIKE_STEP", "50")



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
                <h2 class="text-center mb-4">Trading View Algo Settings</h2>

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

                    <div class="mb-3">
                        <label class="form-label fw-bold d-block">
                            Expiry Index:
                        </label>

                        <div class="d-flex gap-4">
                            <div class="form-check">
                                <input class="form-check-input"
                                    type="radio"
                                    name="expiry_index"
                                    value="0"
                                    id="exp0"
                                    __EXP0__>
                                <label class="form-check-label" for="exp0">
                                    0 – Near
                                </label>
                            </div>

                            <div class="form-check">
                                <input class="form-check-input"
                                    type="radio"
                                    name="expiry_index"
                                    value="1"
                                    id="exp1"
                                    __EXP1__>
                                <label class="form-check-label" for="exp1">
                                    1 – Next
                                </label>
                            </div>

                            <div class="form-check">
                                <input class="form-check-input"
                                    type="radio"
                                    name="expiry_index"
                                    value="2"
                                    id="exp2"
                                    __EXP2__>
                                <label class="form-check-label" for="exp2">
                                    2 – Far
                                </label>
                            </div>
                        </div>
                    </div>

                    <div class="mb-3">
                        <label class="form-label fw-bold">Strike Step</label>
                        <input
                            type="number"
                            name="strike_step"
                            class="form-control"
                            min="25"
                            max="500"
                            step="25"
                            value="__STRIKE_STEP__"
                            required
                        >
                        <small class="text-muted">
                            NIFTY: 50 | BANKNIFTY: 100 | FINNIFTY: 50
                        </small>
                    </div>

                    <div class="mb-3">
                        <label class="form-label fw-bold">Lots:</label>
                        <input type="number"
                            min="1"
                            max="50"
                            name="lots"
                            class="form-control"
                            value="__LOTS__"
                            required>
                    </div>
                    <div class="row mb-3">
                        <div class="col-md-6">
                            <label class="form-label fw-bold">CE Strike Type</label>
                            <select name="ce_strike_type" class="form-select">
                            <option value="ITM1" __CE_ITM1__>ITM1</option>
                            <option value="ITM2" __CE_ITM2__>ITM2</option>
                            <option value="ATM"  __CE_ATM__>ATM</option>
                            <option value="OTM1" __CE_OTM1__>OTM1</option>
                            <option value="OTM2" __CE_OTM2__>OTM2</option>
                            </select>
                        </div>

                        <div class="col-md-6">
                            <label class="form-label fw-bold">PE Strike Type</label>
                            <select name="pe_strike_type" class="form-select">
                            <option value="ITM1" __PE_ITM1__>ITM1</option>
                            <option value="ITM2" __PE_ITM2__>ITM2</option>
                            <option value="ATM"  __PE_ATM__>ATM</option>
                            <option value="OTM1" __PE_OTM1__>OTM1</option>
                            <option value="OTM2" __PE_OTM2__>OTM2</option>
                            </select>
                        </div>
                        </div>

                        <div class="d-flex justify-content-center gap-3 my-4">
                            <button class="btn btn-primary px-4" type="submit">
                                Update Settings
                            </button>
                            <button type="button" class="btn btn-success" onclick="testDhan()">
                                Test DHAN Connection
                            </button>
                        </div>

                </form>
    
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
    html = html.replace("__LOTS__", lots)
    html = html.replace("__EXP0__", "selected" if expiry_index == "0" else "")
    html = html.replace("__EXP1__", "selected" if expiry_index == "1" else "")
    html = html.replace("__EXP2__", "selected" if expiry_index == "2" else "")
    for v in ["ITM1", "ITM2", "ATM", "OTM1", "OTM2"]:
        html = html.replace(f"__CE_{v}__", "selected" if ce_strike_type == v else "")
        html = html.replace(f"__PE_{v}__", "selected" if pe_strike_type == v else "")
    html = html.replace("__STRIKE_STEP__", strike_step)

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
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import date, datetime, time
import pytz
from supabase import create_client, Client
from passlib.context import CryptContext

# ─── Configuration ────────────────────────────────────────────────────────────
# Set these two environment variables in Render dashboard:
#   SUPABASE_URL              e.g. https://xxxxxxxxxxxx.supabase.co
#   SUPABASE_SERVICE_ROLE_KEY (secret service_role key from Supabase → Settings → API)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "Missing environment variables. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Render."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ─── App ──────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield  # no connect/disconnect needed with supabase-py

app = FastAPI(title="Canteen Portal API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Helpers ──────────────────────────────────────────────────────────────────
IST = pytz.timezone("Asia/Kolkata")

def get_ist_now():
    return datetime.now(IST)

def parse_time(val) -> time:
    """Supabase returns time as a string like '07:00:00' — convert to time object."""
    if isinstance(val, time):
        return val
    return datetime.strptime(str(val), "%H:%M:%S").time()

async def check_meal_cutoff(meal_type: str, target_date: date):
    """Only end_time is used as the cutoff — start_time is ignored."""
    ist_now = get_ist_now()
    if target_date < ist_now.date():
        return False, "Cannot modify registrations for past dates."
    if target_date > ist_now.date():
        return True, ""
    res = supabase.table("meal_timelines").select("end_time").eq("meal_type", meal_type).execute()
    if not res.data:
        return False, "Meal type not configured."
    end_t = parse_time(res.data[0]["end_time"])
    if ist_now.time() > end_t:
        cutoff_str = datetime.combine(ist_now.date(), end_t).strftime('%I:%M %p').lstrip('0')
        return False, f"Registration closed. Cutoff was {cutoff_str} IST."
    return True, ""

# ─── Models ───────────────────────────────────────────────────────────────────
class FirstLoginRequest(BaseModel):
    emp_id: str
    dob: str

class PasswordChangeRequest(BaseModel):
    emp_id: str
    new_password: str

class LoginRequest(BaseModel):
    emp_id: str
    password: str

class MealRequest(BaseModel):
    emp_id: str
    meal_date: str
    meal_type: str

# ─── Health ───────────────────────────────────────────────────────────────────
@app.get("/health")
@app.get("/api/health")
async def health_check():
    try:
        supabase.table("employees").select("emp_id").limit(1).execute()
        return {
            "status": "healthy",
            "database": "connected",
            "timestamp": get_ist_now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {str(e)}")

# ─── Meal Timelines ───────────────────────────────────────────────────────────
@app.get("/meal-timelines")
async def get_meal_timelines():
    res = supabase.table("meal_timelines").select("meal_type, end_time").order("end_time").execute()
    ist_now = get_ist_now()
    result = {}
    for r in res.data:
        end_t = parse_time(r["end_time"])
        end_str = datetime.combine(ist_now.date(), end_t).strftime('%I:%M %p').lstrip('0')
        result[r["meal_type"]] = {
            "end_time": end_str,
            "open_today": ist_now.time() <= end_t
        }
    return result

# ─── Auth ─────────────────────────────────────────────────────────────────────
@app.post("/first-login")
async def first_login(req: FirstLoginRequest):
    res = supabase.table("employees").select("emp_id, dob, password_hash, is_first_login").eq("emp_id", req.emp_id.strip()).execute()
    if not res.data:
        raise HTTPException(404, "Employee not found")
    row = res.data[0]
    if str(row["dob"]) != req.dob.strip():
        raise HTTPException(401, "DOB mismatch")
    if not row["is_first_login"] and row["password_hash"]:
        raise HTTPException(403, "Account already activated. Please login with your password.")
    return {"status": "verified", "emp_id": row["emp_id"]}

@app.post("/change-password")
async def change_password(req: PasswordChangeRequest):
    res = supabase.table("employees").select("is_first_login").eq("emp_id", req.emp_id).execute()
    if not res.data:
        raise HTTPException(404, "Employee not found")
    if not res.data[0]["is_first_login"]:
        raise HTTPException(403, "Password already set. Use login instead.")
    hashed = pwd_context.hash(str(req.new_password)[:72])
    supabase.table("employees").update({"password_hash": hashed, "is_first_login": False}).eq("emp_id", req.emp_id).execute()
    return {"status": "success"}

@app.post("/login")
async def login(req: LoginRequest):
    res = supabase.table("employees").select("emp_id, name, password_hash, is_first_login").eq("emp_id", req.emp_id.strip()).execute()
    if not res.data or res.data[0]["is_first_login"]:
        raise HTTPException(401, "Unauthorized")
    row = res.data[0]
    if not pwd_context.verify(req.password[:72], row["password_hash"]):
        raise HTTPException(401, "Invalid password")
    return {"status": "success", "emp_id": row["emp_id"], "name": row["name"]}

# ─── Meals ────────────────────────────────────────────────────────────────────
@app.post("/meals/register")
async def register_meal(req: MealRequest):
    d_obj = date.fromisoformat(req.meal_date)
    allowed, msg = await check_meal_cutoff(req.meal_type, d_obj)
    if not allowed:
        raise HTTPException(403, msg)
    try:
        supabase.table("meal_registrations").insert({
            "emp_id": req.emp_id,
            "meal_date": str(d_obj),
            "meal_type": req.meal_type
        }).execute()
        return {"status": "success"}
    except Exception:
        raise HTTPException(409, "Already registered")

@app.post("/meals/unregister")
async def unregister_meal(req: MealRequest):
    d_obj = date.fromisoformat(req.meal_date)
    allowed, msg = await check_meal_cutoff(req.meal_type, d_obj)
    if not allowed:
        raise HTTPException(403, msg)
    supabase.table("meal_registrations").delete().eq("emp_id", req.emp_id).eq("meal_date", str(d_obj)).eq("meal_type", req.meal_type).execute()
    return {"status": "unregistered"}

@app.get("/employee/{emp_id}/meals/all")
async def get_all_meals(emp_id: str):
    res = supabase.table("meal_registrations").select("meal_date, meal_type").eq("emp_id", emp_id).order("meal_date", desc=True).order("meal_type").execute()
    return {"registrations": [
        {"meal_date": str(r["meal_date"]), "meal_type": r["meal_type"]}
        for r in res.data
    ]}

@app.get("/employee/{emp_id}/meals/stats")
async def get_meal_stats(emp_id: str):
    today = get_ist_now().date()
    res = supabase.table("meal_registrations").select("meal_date").eq("emp_id", emp_id).execute()
    total = len(res.data)
    upcoming = sum(1 for r in res.data if date.fromisoformat(str(r["meal_date"])) >= today)
    return {"total": total, "upcoming": upcoming, "past": total - upcoming}

@app.get("/employee/{emp_id}/tokens/{meal_date}")
async def get_token_status(emp_id: str, meal_date: str):
    """
    Returns token collection status for all meals on a given date.
    Queries canteen_tokens table (pushed by the biometric canteen machine).
    Returns: { tokens: { breakfast: {collected, token_number, issued_at} | null, ... } }
    """
    d_obj = date.fromisoformat(meal_date)
    res = supabase.table("canteen_tokens") \
        .select("meal, token_number, issued_at") \
        .eq("emp_id", emp_id) \
        .eq("token_date", str(d_obj)) \
        .execute()
    tokens = {}
    for r in res.data:
        tokens[r["meal"]] = {
            "collected": True,
            "token_number": r["token_number"],
            "issued_at": str(r["issued_at"])[:5] if r["issued_at"] else None
        }
    return {"tokens": tokens}

@app.get("/employee/{emp_id}/meals/with-tokens")
async def get_all_meals_with_tokens(emp_id: str):
    """
    Returns all registrations joined with token status.
    Used for the 'Registered Meals' history list.
    """
    regs_res = supabase.table("meal_registrations") \
        .select("meal_date, meal_type") \
        .eq("emp_id", emp_id) \
        .order("meal_date", desc=True) \
        .execute()

    if not regs_res.data:
        return {"registrations": []}

    # Get unique dates to batch-fetch tokens
    dates = list({str(r["meal_date"]) for r in regs_res.data})
    toks_res = supabase.table("canteen_tokens") \
        .select("meal, token_date, token_number, issued_at") \
        .eq("emp_id", emp_id) \
        .in_("token_date", dates) \
        .execute()

    # Build token lookup: (meal, date) -> token info
    tok_map = {}
    for t in toks_res.data:
        tok_map[(t["meal"], str(t["token_date"]))] = {
            "collected": True,
            "token_number": t["token_number"],
            "issued_at": str(t["issued_at"])[:5] if t["issued_at"] else None
        }

    result = []
    for r in regs_res.data:
        d = str(r["meal_date"])
        m = r["meal_type"]
        tok = tok_map.get((m, d))
        result.append({
            "meal_date": d,
            "meal_type": m,
            "token": tok  # None if not collected, dict if collected
        })
    return {"registrations": result}

# ── Dynamic date route — MUST be last ─────────────────────────────────────────
@app.get("/meals/{emp_id}/{meal_date}")
async def get_user_meals(emp_id: str, meal_date: str):
    d_obj = date.fromisoformat(meal_date)
    res = supabase.table("meal_registrations").select("meal_type").eq("emp_id", emp_id).eq("meal_date", str(d_obj)).execute()
    return {"registered_meals": [r["meal_type"] for r in res.data]}

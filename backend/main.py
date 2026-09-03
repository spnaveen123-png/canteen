import os
import json
import logging
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import date, datetime, time, timedelta
import pytz
from supabase import create_client, Client
from passlib.context import CryptContext

# ─── Configuration ────────────────────────────────────────────────────────────
# Required in Render:
#   SUPABASE_URL
#   SUPABASE_SERVICE_ROLE_KEY
# Required for reminders:
#   VAPID_PUBLIC_KEY          urlsafe-base64 public key
#   VAPID_PRIVATE_KEY         urlsafe-base64 private key
#   VAPID_SUBJECT             mailto:canteen@yourcompany.com
# Optional:
#   REMINDER_SECRET           shared secret for the /push/run-reminders cron hook
#   ENABLE_SCHEDULER          "1" to run the in-process 7/12/20 IST scheduler

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
def _normalise_vapid_subject(raw: str) -> str:
    """
    py-vapid demands a mailto: or https:// URL and rejects anything else with
    "Missing 'sub' from claims" — the same message it gives for a blank value,
    which makes a missing mailto: prefix look like a missing variable. Accept
    a bare email address and fix it up rather than failing every push.
    """
    v = (raw or "").strip()
    if not v:
        return ""
    if v.startswith("mailto:") or v.startswith("https://"):
        return v
    if "@" in v and " " not in v:
        return "mailto:" + v
    return ""


VAPID_SUBJECT_RAW = os.getenv("VAPID_SUBJECT", "")
VAPID_SUBJECT     = _normalise_vapid_subject(VAPID_SUBJECT_RAW)
if VAPID_SUBJECT_RAW.strip() and VAPID_SUBJECT != VAPID_SUBJECT_RAW.strip():
    logging.getLogger("canteen").warning(
        "VAPID_SUBJECT %r is not a mailto: link; using %r",
        VAPID_SUBJECT_RAW, VAPID_SUBJECT or "(nothing — push will fail)")
REMINDER_SECRET   = os.getenv("REMINDER_SECRET", "")
ENABLE_SCHEDULER  = os.getenv("ENABLE_SCHEDULER", "1") == "1"

# Keep-alive. Render's free tier sleeps a service after ~15 minutes with no
# inbound request, and the cold start that follows takes close to a minute.
# A request the service makes to itself still counts as inbound traffic, so a
# short self-ping keeps it warm. Confined to a daily window because free
# instance-hours are capped — see SETUP.md.
KEEPALIVE_URL     = os.getenv("KEEPALIVE_URL", "").rstrip("/")
KEEPALIVE_MINUTES = int(os.getenv("KEEPALIVE_MINUTES", "12"))
KEEPALIVE_FROM    = int(os.getenv("KEEPALIVE_FROM_HOUR", "6"))    # IST, inclusive
KEEPALIVE_TO      = int(os.getenv("KEEPALIVE_TO_HOUR", "22"))     # IST, exclusive

# The plant runs continuously, so the canteen serves every day by default.
# This is only here for a genuine full shutdown; leave it empty normally.
# Python weekday numbers, Mon=0 … Sun=6.
CLOSED_WEEKDAYS = {
    int(x) for x in os.getenv("CANTEEN_CLOSED_WEEKDAYS", "").split(",") if x.strip().isdigit()
}

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "Missing environment variables. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Render."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

log = logging.getLogger("canteen")
logging.basicConfig(level=logging.INFO)

IST = pytz.timezone("Asia/Kolkata")
# Fallback only. The real list is read from meal_timelines, so adding a row
# there (e.g. 'snacks') makes it appear everywhere with no code change.
DEFAULT_MEAL_TYPES = ["breakfast", "lunch", "dinner"]
# Fallback only. The live value is the reminder_hours row in app_settings,
# so the timings can be changed in Supabase without a redeploy.
DEFAULT_REMINDER_HOURS = [6, 8, 18]       # IST

_meal_cache = {"at": None, "cutoffs": {}}


def get_ist_now():
    return datetime.now(IST)


def parse_time(val) -> time:
    """Supabase returns time as a string like '07:00:00' — convert to time object."""
    if isinstance(val, time):
        return val
    return datetime.strptime(str(val), "%H:%M:%S").time()


def fetch_cutoffs(force: bool = False) -> dict:
    """{ meal_type: time } straight from meal_timelines, cached for a minute."""
    now = get_ist_now()
    if not force and _meal_cache["at"] and (now - _meal_cache["at"]).total_seconds() < 60:
        return _meal_cache["cutoffs"]
    try:
        res = supabase.table("meal_timelines").select("meal_type, end_time").order("end_time").execute()
        cutoffs = {r["meal_type"]: parse_time(r["end_time"]) for r in (res.data or [])}
        if cutoffs:
            _meal_cache["at"] = now
            _meal_cache["cutoffs"] = cutoffs
            return cutoffs
    except Exception as e:
        log.warning("Could not read meal_timelines: %s", e)
    return _meal_cache["cutoffs"] or {}


def meal_types() -> List[str]:
    """Every meal the canteen serves, earliest cutoff first."""
    cutoffs = fetch_cutoffs()
    if not cutoffs:
        return list(DEFAULT_MEAL_TYPES)
    return sorted(cutoffs, key=lambda m: cutoffs[m])


def open_meals_now() -> List[str]:
    """Meal types whose cutoff hasn't passed yet today."""
    now_t = get_ist_now().time()
    cutoffs = fetch_cutoffs()
    return [m for m in meal_types() if m in cutoffs and now_t <= cutoffs[m]]


def usual_meal(emp_id: str, days: int = 45) -> Optional[str]:
    """
    The meal this employee books most often. Most people book exactly one meal
    a day, so the reminder can offer that one directly instead of asking them
    to open the app and choose.
    """
    since = get_ist_now().date() - timedelta(days=days)
    try:
        res = supabase.table("meal_registrations").select("meal_type") \
            .eq("emp_id", emp_id).gte("meal_date", str(since)).execute()
    except Exception:
        return None
    counts = {}
    for r in (res.data or []):
        counts[r["meal_type"]] = counts.get(r["meal_type"], 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


_settings_cache = {"at": None, "rows": {}}


def app_settings() -> dict:
    """Key/value rows from app_settings, cached for a minute."""
    now = get_ist_now()
    if _settings_cache["at"] and (now - _settings_cache["at"]).total_seconds() < 60:
        return _settings_cache["rows"]
    try:
        res = supabase.table("app_settings").select("key, value").execute()
        _settings_cache["rows"] = {r["key"]: r["value"] for r in (res.data or [])}
    except Exception as e:
        log.warning("Could not read app_settings: %s", e)
        _settings_cache["rows"] = _settings_cache["rows"] or {}
    _settings_cache["at"] = now
    return _settings_cache["rows"]


def reminder_hours() -> List[int]:
    """
    When the daily prompts go out, IST. Change the reminder_hours row in
    app_settings (e.g. '6,8,18') and it takes effect within a minute — no
    redeploy, and no editing the cron schedule, because the cron calls in
    every 30 minutes and this decides whether a round is due.
    """
    raw = app_settings().get("reminder_hours", "")
    hours = sorted({int(x) for x in str(raw).split(",")
                    if x.strip().lstrip("-").isdigit() and 0 <= int(x) <= 23})
    return hours or list(DEFAULT_REMINDER_HOURS)


def weekly_off_map(emp_ids=None) -> dict:
    """{emp_id: weekday int} — Mon=0 … Sun=6. Missing means never asked."""
    try:
        q = supabase.table("employee_settings").select("emp_id, weekly_off")
        if emp_ids:
            q = q.in_("emp_id", list(emp_ids))
        res = q.execute()
    except Exception as e:
        log.warning("Could not read employee_settings: %s", e)
        return {}
    return {r["emp_id"]: r["weekly_off"] for r in (res.data or [])
            if r.get("weekly_off") is not None}


def weekly_off_for(emp_id: str):
    return weekly_off_map([emp_id]).get(emp_id)


_holiday_cache = {"at": None, "days": {}}


def holidays() -> dict:
    """{date_string: reason} from canteen_holidays, cached for 10 minutes."""
    now = get_ist_now()
    if _holiday_cache["at"] and (now - _holiday_cache["at"]).total_seconds() < 600:
        return _holiday_cache["days"]
    try:
        since = str(now.date() - timedelta(days=1))
        res = supabase.table("canteen_holidays").select("holiday_date, reason") \
            .gte("holiday_date", since).execute()
        _holiday_cache["days"] = {str(r["holiday_date"]): (r.get("reason") or "Holiday")
                                  for r in (res.data or [])}
        _holiday_cache["at"] = now
    except Exception as e:
        # Table not created yet — treat every day as a serving day.
        log.warning("Could not read canteen_holidays: %s", e)
        _holiday_cache["days"] = {}
        _holiday_cache["at"] = now
    return _holiday_cache["days"]


def day_status(d: date):
    """(serving, reason). Stops meals being booked on days nobody cooks."""
    hit = holidays().get(str(d))
    if hit:
        return False, f"Canteen closed \u2014 {hit}"
    if d.weekday() in CLOSED_WEEKDAYS:
        return False, f"Canteen closed on {d.strftime('%A')}s"
    return True, None


def open_meals_on(d: date) -> List[str]:
    """Meals that can still be booked for a given date."""
    serving, _ = day_status(d)
    if not serving:
        return []
    today = get_ist_now().date()
    if d < today:
        return []
    if d > today:
        return meal_types()
    return open_meals_now()


async def check_meal_cutoff(meal_type: str, target_date: date):
    """Only end_time is used as the cutoff — start_time is ignored."""
    ist_now = get_ist_now()
    if target_date < ist_now.date():
        return False, "Registration for past dates can't be changed."
    serving, reason = day_status(target_date)
    if not serving:
        return False, reason
    if target_date > ist_now.date():
        return True, ""
    res = supabase.table("meal_timelines").select("end_time").eq("meal_type", meal_type).execute()
    if not res.data:
        return False, "Meal type not configured."
    end_t = parse_time(res.data[0]["end_time"])
    if ist_now.time() > end_t:
        cutoff_str = datetime.combine(ist_now.date(), end_t).strftime("%I:%M %p").lstrip("0")
        return False, f"{meal_type.capitalize()} closed at {cutoff_str}."
    return True, ""


# ─── App ──────────────────────────────────────────────────────────────────────
scheduler = None
keepalive_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    if ENABLE_SCHEDULER and VAPID_PRIVATE_KEY:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            scheduler = BackgroundScheduler(timezone=IST)
            # Fire on the hour, every hour. send_daily_reminders() checks the
            # live reminder_hours setting and no-ops when a round isn't due, so
            # changing the timing in Supabase works without a restart.
            scheduler.add_job(
                send_daily_reminders,
                CronTrigger(minute=0, timezone=IST),
                id="reminder-sweep",
                replace_existing=True,
            )
            scheduler.start()
            log.info("Reminder sweep started; current hours %s IST", reminder_hours())
        except Exception as e:
            log.warning("Scheduler not started: %s", e)

    if KEEPALIVE_URL:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
            global keepalive_scheduler
            keepalive_scheduler = BackgroundScheduler(timezone=IST)
            keepalive_scheduler.add_job(
                self_ping,
                IntervalTrigger(minutes=max(5, KEEPALIVE_MINUTES), timezone=IST),
                id="keepalive",
                replace_existing=True,
            )
            keepalive_scheduler.start()
            log.info("Keep-alive every %s min, %02d:00-%02d:00 IST",
                     KEEPALIVE_MINUTES, KEEPALIVE_FROM, KEEPALIVE_TO)
        except Exception as e:
            log.warning("Scheduler not started: %s", e)
    yield
    for sch in (scheduler, keepalive_scheduler):
        if sch:
            try:
                sch.shutdown(wait=False)
            except Exception:
                pass


app = FastAPI(title="Canteen Portal API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


class RespondRequest(BaseModel):
    """The Yes/No answer — 'meals' is the complete list the employee wants."""
    emp_id: str
    meal_date: str
    meals: List[str] = []


class AnsweredRequest(BaseModel):
    emp_id: str
    meal_date: str


class SubscribeRequest(BaseModel):
    emp_id: str
    endpoint: str
    p256dh: str
    auth: str
    tz_offset_minutes: Optional[int] = None


class ResubscribeRequest(BaseModel):
    old_endpoint: Optional[str] = None
    endpoint: str
    p256dh: str
    auth: str


# ─── Keep-alive ───────────────────────────────────────────────────────────────
def self_ping():
    """
    Hit our own /ping so Render sees inbound traffic and doesn't spin the
    service down. Silent outside the configured window so we don't burn free
    instance-hours overnight when nobody is booking meals.
    """
    now = get_ist_now()
    if not (KEEPALIVE_FROM <= now.hour < KEEPALIVE_TO):
        return
    try:
        import urllib.request
        req = urllib.request.Request(KEEPALIVE_URL + "/ping",
                                     headers={"User-Agent": "canteen-keepalive"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read(64)
    except Exception as e:
        log.warning("Keep-alive ping failed: %s", e)


@app.get("/ping")
async def ping():
    """
    Deliberately does nothing — no database call, no auth. Point uptime
    monitors here rather than /health so a warm-up never touches Supabase.
    """
    return {"ok": True, "ist": get_ist_now().strftime("%Y-%m-%d %H:%M:%S")}


# ─── Health ───────────────────────────────────────────────────────────────────
@app.get("/health")
@app.get("/api/health")
async def health_check():
    try:
        supabase.table("employees").select("emp_id").limit(1).execute()
        return {
            "status": "healthy",
            "database": "connected",
            "push": bool(VAPID_PRIVATE_KEY),
            "keepalive": bool(KEEPALIVE_URL),
            "vapid_subject_valid": bool(VAPID_SUBJECT),
            "reminder_hours": reminder_hours(),
            "timestamp": get_ist_now().isoformat(),
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
        end_str = datetime.combine(ist_now.date(), end_t).strftime("%I:%M %p").lstrip("0")
        result[r["meal_type"]] = {
            "end_time": end_str,
            "open_today": ist_now.time() <= end_t,
        }
    return result


@app.get("/settings")
async def get_settings():
    """Timings the portal shows on the reminders card."""
    return {"reminder_hours": reminder_hours()}


@app.get("/employee/{emp_id}/settings")
async def get_employee_settings(emp_id: str):
    off = weekly_off_for(emp_id)
    return {
        "emp_id": emp_id,
        "weekly_off": off,
        "weekly_off_name": WEEKDAY_NAMES[off] if off is not None else None,
        "asked": off is not None,
    }


class EmployeeSettings(BaseModel):
    weekly_off: Optional[int] = None      # Mon=0 … Sun=6, or null for none


@app.post("/employee/{emp_id}/settings")
async def set_employee_settings(emp_id: str, req: EmployeeSettings):
    off = req.weekly_off
    if off is not None and not (0 <= off <= 6):
        raise HTTPException(400, "weekly_off must be 0 (Monday) to 6 (Sunday)")
    supabase.table("employee_settings").upsert(
        {"emp_id": emp_id, "weekly_off": off, "updated_at": get_ist_now().isoformat()},
        on_conflict="emp_id",
    ).execute()
    return {
        "status": "saved",
        "weekly_off": off,
        "weekly_off_name": WEEKDAY_NAMES[off] if off is not None else None,
    }


@app.get("/day/{meal_date}")
async def get_day(meal_date: str, emp_id: Optional[str] = None):
    """
    Everything the portal needs about one date: whether the canteen serves at
    all, and which meals are still open. Replaces the client guessing from
    'is it today or tomorrow'.
    """
    d_obj = date.fromisoformat(meal_date)
    serving, reason = day_status(d_obj)
    cutoffs = fetch_cutoffs()
    open_now = set(open_meals_on(d_obj))
    meals = {}
    for m in meal_types():
        end_t = cutoffs.get(m)
        meals[m] = {
            "end_time": (datetime.combine(d_obj, end_t).strftime("%I:%M %p").lstrip("0")
                         if end_t else ""),
            "open": m in open_now,
        }

    # A weekly off is a hint, never a block. People do come in for audits and
    # overtime, and when they do they still need feeding.
    weekly_off = False
    if emp_id:
        off = weekly_off_for(emp_id)
        weekly_off = off is not None and d_obj.weekday() == off

    return {"date": str(d_obj), "serving": serving, "reason": reason,
            "weekly_off": weekly_off, "meals": meals}


# ─── Auth ─────────────────────────────────────────────────────────────────────
@app.post("/first-login")
async def first_login(req: FirstLoginRequest):
    res = supabase.table("employees").select(
        "emp_id, dob, password_hash, is_first_login"
    ).eq("emp_id", req.emp_id.strip()).execute()
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
    supabase.table("employees").update(
        {"password_hash": hashed, "is_first_login": False}
    ).eq("emp_id", req.emp_id).execute()
    return {"status": "success"}


@app.post("/login")
async def login(req: LoginRequest):
    res = supabase.table("employees").select(
        "emp_id, name, password_hash, is_first_login"
    ).eq("emp_id", req.emp_id.strip()).execute()
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
    # Idempotent on purpose. A double tap, or a retry after a request that
    # actually succeeded but timed out on a cold start, must not come back as
    # an error — the client would roll its tick back while the booking exists.
    try:
        supabase.table("meal_registrations").insert({
            "emp_id": req.emp_id,
            "meal_date": str(d_obj),
            "meal_type": req.meal_type,
        }).execute()
        return {"status": "success"}
    except Exception:
        existing = supabase.table("meal_registrations").select("meal_type") \
            .eq("emp_id", req.emp_id).eq("meal_date", str(d_obj)) \
            .eq("meal_type", req.meal_type).execute()
        if existing.data:
            return {"status": "already_registered"}
        raise HTTPException(500, "Couldn't save that. Please try again.")


@app.post("/meals/unregister")
async def unregister_meal(req: MealRequest):
    d_obj = date.fromisoformat(req.meal_date)
    allowed, msg = await check_meal_cutoff(req.meal_type, d_obj)
    if not allowed:
        raise HTTPException(403, msg)
    # Deleting something that isn't there is also success — same reasoning.
    supabase.table("meal_registrations").delete() \
        .eq("emp_id", req.emp_id).eq("meal_date", str(d_obj)).eq("meal_type", req.meal_type).execute()
    return {"status": "unregistered"}


@app.post("/meals/respond")
async def respond(req: RespondRequest):
    """
    One-shot answer used by the notification's Yes/No buttons and the
    'Yes to all' / 'No meals' buttons in the portal.

    'meals' is the full list the employee wants for that date. Anything not in
    the list is removed. Meals whose cutoff has already passed are left alone.
    """
    d_obj = date.fromisoformat(req.meal_date)
    all_types = meal_types()
    wanted = {m for m in req.meals if m in all_types}

    existing_res = supabase.table("meal_registrations").select("meal_type") \
        .eq("emp_id", req.emp_id).eq("meal_date", str(d_obj)).execute()
    existing = {r["meal_type"] for r in (existing_res.data or [])}

    changed, skipped = [], []
    for m in all_types:
        allowed, _ = await check_meal_cutoff(m, d_obj)
        if not allowed:
            if (m in wanted) != (m in existing):
                skipped.append(m)
            continue
        if m in wanted and m not in existing:
            try:
                supabase.table("meal_registrations").insert({
                    "emp_id": req.emp_id, "meal_date": str(d_obj), "meal_type": m
                }).execute()
                changed.append(m)
            except Exception:
                pass
        elif m not in wanted and m in existing:
            supabase.table("meal_registrations").delete() \
                .eq("emp_id", req.emp_id).eq("meal_date", str(d_obj)).eq("meal_type", m).execute()
            changed.append(m)

    _mark_answered(req.emp_id, d_obj)

    final_res = supabase.table("meal_registrations").select("meal_type") \
        .eq("emp_id", req.emp_id).eq("meal_date", str(d_obj)).execute()
    return {
        "status": "success",
        "registered_meals": [r["meal_type"] for r in (final_res.data or [])],
        "skipped_closed": skipped,
    }


@app.post("/meals/answered")
async def mark_answered_endpoint(req: AnsweredRequest):
    """Employee picked meals by hand — stop the later reminders for that date."""
    _mark_answered(req.emp_id, date.fromisoformat(req.meal_date))
    return {"status": "ok"}


def _mark_answered(emp_id: str, d_obj: date):
    try:
        supabase.table("meal_prompt_answers").upsert(
            {
                "emp_id": emp_id,
                "meal_date": str(d_obj),
                "answered_at": get_ist_now().isoformat(),
            },
            on_conflict="emp_id,meal_date",
        ).execute()
    except Exception as e:
        log.warning("Could not record answer for %s %s: %s", emp_id, d_obj, e)


@app.get("/employee/{emp_id}/answered/{meal_date}")
async def get_answered(emp_id: str, meal_date: str):
    res = supabase.table("meal_prompt_answers").select("answered_at") \
        .eq("emp_id", emp_id).eq("meal_date", meal_date).execute()
    return {"answered": bool(res.data)}


@app.get("/employee/{emp_id}/meals/all")
async def get_all_meals(emp_id: str):
    res = supabase.table("meal_registrations").select("meal_date, meal_type") \
        .eq("emp_id", emp_id).order("meal_date", desc=True).order("meal_type").execute()
    return {"registrations": [
        {"meal_date": str(r["meal_date"]), "meal_type": r["meal_type"]} for r in res.data
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
    """Token collection status pushed by the biometric canteen machine."""
    d_obj = date.fromisoformat(meal_date)
    res = supabase.table("canteen_tokens").select("meal, token_number, issued_at") \
        .eq("emp_id", emp_id).eq("token_date", str(d_obj)).execute()
    tokens = {}
    for r in res.data:
        tokens[r["meal"]] = {
            "collected": True,
            "token_number": r["token_number"],
            "issued_at": str(r["issued_at"])[:5] if r["issued_at"] else None,
        }
    return {"tokens": tokens}


@app.get("/employee/{emp_id}/meals/with-tokens")
async def get_all_meals_with_tokens(emp_id: str):
    regs_res = supabase.table("meal_registrations").select("meal_date, meal_type") \
        .eq("emp_id", emp_id).order("meal_date", desc=True).execute()
    if not regs_res.data:
        return {"registrations": []}

    dates = list({str(r["meal_date"]) for r in regs_res.data})
    toks_res = supabase.table("canteen_tokens").select("meal, token_date, token_number, issued_at") \
        .eq("emp_id", emp_id).in_("token_date", dates).execute()

    tok_map = {}
    for t in toks_res.data:
        tok_map[(t["meal"], str(t["token_date"]))] = {
            "collected": True,
            "token_number": t["token_number"],
            "issued_at": str(t["issued_at"])[:5] if t["issued_at"] else None,
        }

    result = []
    for r in regs_res.data:
        d = str(r["meal_date"])
        m = r["meal_type"]
        result.append({"meal_date": d, "meal_type": m, "token": tok_map.get((m, d))})
    return {"registrations": result}


@app.get("/employee/{emp_id}/usual-meal")
async def get_usual_meal(emp_id: str):
    """The meal this employee books most often — used to label the reminder."""
    return {"meal_type": usual_meal(emp_id)}


# ═══ Push notifications ═══════════════════════════════════════════════════════
@app.get("/push/public-key")
async def push_public_key():
    if not VAPID_PUBLIC_KEY:
        raise HTTPException(503, "Push is not configured on the server.")
    return {"key": VAPID_PUBLIC_KEY}


@app.post("/push/subscribe")
async def push_subscribe(req: SubscribeRequest):
    supabase.table("push_subscriptions").upsert(
        {
            "emp_id": req.emp_id,
            "endpoint": req.endpoint,
            "p256dh": req.p256dh,
            "auth": req.auth,
            "updated_at": get_ist_now().isoformat(),
        },
        on_conflict="endpoint",
    ).execute()
    return {"status": "subscribed"}


@app.post("/push/resubscribe")
async def push_resubscribe(req: ResubscribeRequest):
    emp_id = None
    if req.old_endpoint:
        old = supabase.table("push_subscriptions").select("emp_id") \
            .eq("endpoint", req.old_endpoint).execute()
        if old.data:
            emp_id = old.data[0]["emp_id"]
        supabase.table("push_subscriptions").delete().eq("endpoint", req.old_endpoint).execute()
    if not emp_id:
        raise HTTPException(404, "Unknown subscription. Open the portal to turn reminders back on.")
    supabase.table("push_subscriptions").upsert(
        {"emp_id": emp_id, "endpoint": req.endpoint, "p256dh": req.p256dh, "auth": req.auth},
        on_conflict="endpoint",
    ).execute()
    return {"status": "resubscribed"}


@app.post("/push/unsubscribe")
async def push_unsubscribe(body: dict):
    endpoint = body.get("endpoint")
    if endpoint:
        supabase.table("push_subscriptions").delete().eq("endpoint", endpoint).execute()
    return {"status": "unsubscribed"}


@app.post("/push/run-reminders")
async def run_reminders(force: bool = False, x_reminder_secret: str = Header(default="")):
    """
    Cron hook. Call it every 30 minutes and let the server decide whether a
    round is due — that way the timings live in app_settings and changing them
    needs no edit to the cron schedule.

    force=true sends immediately regardless of the hour, for testing.
    """
    if REMINDER_SECRET and x_reminder_secret != REMINDER_SECRET:
        raise HTTPException(401, "Bad reminder secret")
    return send_daily_reminders(force=force)


def push_ttl_for(target: date) -> int:
    """
    Expire the message when the last meal for that date closes. A phone that
    has been offline all morning shouldn't buzz at 4 PM asking about a lunch
    that shut at 3 — the message is worthless by then, so let it lapse.
    """
    cutoffs = fetch_cutoffs()
    last = max(cutoffs.values()) if cutoffs else time(20, 30)
    expiry = IST.localize(datetime.combine(target, last))
    return max(300, min(int((expiry - get_ist_now()).total_seconds()), 24 * 3600))


def send_one(sub: dict, payload: str, ttl: int):
    """
    Push to a single subscription. Returns (ok, detail). Never raises, and
    surfaces the provider's actual status so failures can be diagnosed
    instead of guessed at.
    """
    from pywebpush import webpush, WebPushException
    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=ttl,
        )
        return True, "sent"
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        body = ""
        try:
            body = (e.response.text or "")[:200]
        except Exception:
            pass
        if status in (404, 410):
            supabase.table("push_subscriptions").delete().eq("endpoint", sub["endpoint"]).execute()
            return False, f"gone ({status}) — subscription deleted, employee must re-enable"
        if status in (401, 403):
            return False, (f"rejected ({status}) — VAPID key mismatch. The browser subscribed "
                           f"with a different public key than the server is signing with. "
                           f"{body}")
        return False, f"failed ({status}) {body}"
    except Exception as e:
        return False, f"error: {type(e).__name__}: {e}"


def claim_round(round_date: date, hour: int) -> bool:
    """
    Only one send per (date, hour). The cron calls in several times an hour and
    the in-process sweep may overlap it; this makes a duplicate a no-op rather
    than a second buzz in someone's pocket.
    """
    try:
        supabase.table("reminder_rounds").insert(
            {"round_date": str(round_date), "round_hour": hour,
             "sent_at": get_ist_now().isoformat()}
        ).execute()
        return True
    except Exception:
        return False   # already claimed, or the table is missing


@app.get("/push/diagnose")
async def push_diagnose(x_reminder_secret: str = Header(default="")):
    """
    One call that answers "why did nobody get a notification?".
    Reports config, subscriptions, and whether a round is due right now.
    """
    if REMINDER_SECRET and x_reminder_secret != REMINDER_SECRET:
        raise HTTPException(401, "Bad reminder secret")

    now = get_ist_now()
    hours = reminder_hours()
    today = now.date()

    try:
        subs = supabase.table("push_subscriptions").select("emp_id, endpoint").execute().data or []
    except Exception as e:
        subs = []
        log.warning("diagnose: %s", e)

    try:
        rounds = supabase.table("reminder_rounds").select("round_hour, sent_at") \
            .eq("round_date", str(today)).execute().data or []
    except Exception:
        rounds = []

    try:
        answered = supabase.table("meal_prompt_answers").select("emp_id") \
            .eq("meal_date", str(today)).execute().data or []
    except Exception:
        answered = []

    problems = []
    if not VAPID_PRIVATE_KEY:
        problems.append("VAPID_PRIVATE_KEY is not set on this service — no push can be sent.")
    if not VAPID_PUBLIC_KEY:
        problems.append("VAPID_PUBLIC_KEY is not set — browsers cannot subscribe.")
    if not VAPID_SUBJECT:
        problems.append(
            f"VAPID_SUBJECT is missing or invalid (raw value: {VAPID_SUBJECT_RAW!r}). "
            f"It must be a mailto: link, e.g. mailto:canteen@gcpl.live. Every push "
            f"fails with \"Missing 'sub' from claims\" until this is set.")
    if not subs:
        problems.append("No push subscriptions stored. Nobody has tapped 'Turn on reminders', "
                        "or the subscribe call failed. Check the browser console on a phone.")
    if now.hour not in hours:
        problems.append(f"No round is due at {now.hour:02d}:00 IST. Rounds run at {hours}. "
                        f"Use ?force=true to send now.")

    return {
        "ist_now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "push_configured": bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY),
        "vapid_public_key_prefix": VAPID_PUBLIC_KEY[:12] + "…" if VAPID_PUBLIC_KEY else None,
        "vapid_subject": VAPID_SUBJECT,
        "reminder_hours": hours,
        "round_due_now": now.hour in hours,
        "rounds_already_sent_today": [r["round_hour"] for r in rounds],
        "subscription_count": len(subs),
        "subscribed_employees": sorted({r["emp_id"] for r in subs}),
        "answered_today": len({r["emp_id"] for r in answered}),
        "problems": problems or ["Nothing obviously wrong."],
    }


class TestPushRequest(BaseModel):
    emp_id: str


@app.post("/push/test")
async def push_test(req: TestPushRequest, x_reminder_secret: str = Header(default="")):
    """
    Send one notification to every device belonging to an employee, right now,
    ignoring rounds and answers. Returns the provider's response per device so
    a failure names itself instead of disappearing into a log.
    """
    if REMINDER_SECRET and x_reminder_secret != REMINDER_SECRET:
        raise HTTPException(401, "Bad reminder secret")
    if not VAPID_PRIVATE_KEY:
        raise HTTPException(503, "VAPID_PRIVATE_KEY is not set on this service.")
    if not VAPID_SUBJECT:
        raise HTTPException(
            503,
            f"VAPID_SUBJECT is missing or invalid (raw value: {VAPID_SUBJECT_RAW!r}). "
            f"Set it to a mailto: link such as mailto:canteen@gcpl.live and redeploy.")

    subs = supabase.table("push_subscriptions") \
        .select("emp_id, endpoint, p256dh, auth").eq("emp_id", req.emp_id).execute().data or []
    if not subs:
        return {"sent": 0, "results": [],
                "hint": f"No subscription stored for {req.emp_id}. Open the portal on the "
                        f"phone, sign in as that employee, and tap 'Turn on reminders'."}

    payload = json.dumps({
        "title": "Canteen test",
        "body": "If you can see this, reminders are working.",
        "emp_id": req.emp_id,
        "date": str(get_ist_now().date()),
        "meals": [],
    })

    results = []
    for sub in subs:
        ok, detail = send_one(sub, payload, 300)
        results.append({"endpoint": sub["endpoint"][:60] + "…", "ok": ok, "detail": detail})
    return {"sent": sum(1 for r in results if r["ok"]), "results": results}


def send_daily_reminders(force: bool = False):
    """
    Ask everyone who hasn't answered yet.

    Three things silence a notification:
      • the employee already answered for that date, from the app or a
        notification — so the later rounds only reach the undecided;
      • it's that employee's weekly off;
      • this round has already gone out (see claim_round).
    """
    if not VAPID_PRIVATE_KEY:
        log.warning("Reminders skipped: VAPID_PRIVATE_KEY not set")
        return {"sent": 0, "reason": "push not configured"}
    if not VAPID_SUBJECT:
        log.warning("Reminders skipped: VAPID_SUBJECT missing or not a mailto: link")
        return {"sent": 0, "reason": "VAPID_SUBJECT must be a mailto: link"}

    now = get_ist_now()
    hour = now.hour
    attempted_failures = 0

    hours = reminder_hours()
    if not force:
        if hour not in hours:
            return {"sent": 0, "reason": f"no round due at {hour:02d}:00 IST",
                    "reminder_hours": hours}
        if not claim_round(now.date(), hour):
            return {"sent": 0, "reason": f"round {hour:02d}:00 already sent",
                    "reminder_hours": hours}

    # The last round of the day asks about tomorrow: by then the earlier meals
    # are shut and only tomorrow's count is still worth collecting. Everything
    # before it asks about today.
    last_round = max(hours) if hours else 18
    target = now.date() if hour < last_round else now.date() + timedelta(days=1)

    serving, reason = day_status(target)
    if not serving:
        log.info("Reminders skipped for %s: %s", target, reason)
        return {"sent": 0, "reason": reason, "date": str(target)}

    meals = open_meals_on(target)
    if not meals:
        log.info("Reminders skipped: every meal is past its cutoff")
        return {"sent": 0, "reason": "all meals closed"}

    answered_res = supabase.table("meal_prompt_answers").select("emp_id") \
        .eq("meal_date", str(target)).execute()
    answered = {r["emp_id"] for r in (answered_res.data or [])}

    subs_res = supabase.table("push_subscriptions").select("emp_id, endpoint, p256dh, auth").execute()

    # Don't buzz someone on their day off.
    sub_emps = {r["emp_id"] for r in (subs_res.data or [])}
    offs = weekly_off_map(sub_emps)
    target_weekday = target.weekday()
    resting = {e for e, off in offs.items() if off == target_weekday}

    sent, dropped = 0, 0
    # Say "canteen" out loud. A good number of employees bring a tiffin from
    # home, and "eating in?" is ambiguous to them.
    when = "today" if target == now.date() else "tomorrow"
    if target != now.date():
        title = "Canteen food tomorrow?"
    elif hour == min(hours):
        title = "Canteen food today?"
    else:
        title = "You haven't ordered yet"

    # One lookup per employee, not per device.
    usual_by_emp = {}

    skipped_off = 0
    for s in (subs_res.data or []):
        emp = s["emp_id"]
        if emp in answered:
            continue
        if emp in resting:
            skipped_off += 1
            continue

        if emp not in usual_by_emp:
            u = usual_meal(emp)
            usual_by_emp[emp] = u if (u in meals) else None
        pick = usual_by_emp[emp]

        if pick:
            body = f"Tap to order {pick} at the canteen for {when}."
            action_label = f"Order {pick.capitalize()}"
            offer = [pick]
        else:
            body = f"Open Canteen to order your meal for {when}."
            action_label = None
            offer = []

        payload = json.dumps({
            "title": title,
            "body": body,
            "emp_id": emp,
            "date": str(target),
            "meals": offer,
            "action_label": action_label,
        })
        ok, detail = send_one(s, payload, push_ttl_for(target))
        if ok:
            sent += 1
        elif "gone" in detail:
            dropped += 1
        else:
            attempted_failures += 1
            log.warning("Push failed for %s: %s", emp, detail)

    # If every attempt failed, give the round back so the next cron call can
    # retry. Otherwise a transient outage silently burns the whole round.
    if not force and sent == 0 and attempted_failures > 0:
        try:
            supabase.table("reminder_rounds").delete() \
                .eq("round_date", str(now.date())).eq("round_hour", hour).execute()
            log.warning("Round %02d:00 released for retry — all %s sends failed",
                        hour, attempted_failures)
        except Exception:
            pass

    log.info("Round %02d:00 for %s — sent=%s off=%s dropped=%s failed=%s meals=%s",
             hour, target, sent, skipped_off, dropped, attempted_failures, meals)
    return {"sent": sent, "dropped": dropped, "failed": attempted_failures,
            "weekly_off_skipped": skipped_off, "meals": meals, "date": str(target),
            "round_hour": hour, "reminder_hours": hours}


# ── Dynamic date route — MUST stay last ───────────────────────────────────────
@app.get("/meals/{emp_id}/{meal_date}")
async def get_user_meals(emp_id: str, meal_date: str):
    d_obj = date.fromisoformat(meal_date)
    res = supabase.table("meal_registrations").select("meal_type") \
        .eq("emp_id", emp_id).eq("meal_date", str(d_obj)).execute()
    return {"registered_meals": [r["meal_type"] for r in res.data]}

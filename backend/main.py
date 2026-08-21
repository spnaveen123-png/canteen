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
VAPID_SUBJECT     = os.getenv("VAPID_SUBJECT", "mailto:canteen@example.com")
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
REMINDER_HOURS = [7, 12, 20]          # IST — matches the chips shown in the portal

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


async def check_meal_cutoff(meal_type: str, target_date: date):
    """Only end_time is used as the cutoff — start_time is ignored."""
    ist_now = get_ist_now()
    if target_date < ist_now.date():
        return False, "Registration for past dates can't be changed."
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
            for h in REMINDER_HOURS:
                scheduler.add_job(
                    send_daily_reminders,
                    CronTrigger(hour=h, minute=0, timezone=IST),
                    id=f"reminder-{h}",
                    replace_existing=True,
                )
            scheduler.start()
            log.info("Reminder scheduler started for %s IST", REMINDER_HOURS)
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
    try:
        supabase.table("meal_registrations").insert({
            "emp_id": req.emp_id,
            "meal_date": str(d_obj),
            "meal_type": req.meal_type,
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
async def run_reminders(x_reminder_secret: str = Header(default="")):
    """
    Cron hook. Point cron-job.org / GitHub Actions / Render Cron at this URL for
    07:00, 12:00 and 20:00 IST if you don't want to rely on the in-process
    scheduler (recommended on free hosting, where the app sleeps).
    """
    if REMINDER_SECRET and x_reminder_secret != REMINDER_SECRET:
        raise HTTPException(401, "Bad reminder secret")
    return send_daily_reminders()


def send_daily_reminders():
    """
    Ask everyone who hasn't answered yet today.
    Anyone who already said Yes or No — from the app or from a notification —
    is skipped, so the 12 PM and 8 PM rounds only reach people still undecided.
    """
    if not VAPID_PRIVATE_KEY:
        log.warning("Reminders skipped: VAPID_PRIVATE_KEY not set")
        return {"sent": 0, "reason": "push not configured"}

    from pywebpush import webpush, WebPushException

    today = get_ist_now().date()
    meals = open_meals_now()
    if not meals:
        log.info("Reminders skipped: every meal is past its cutoff")
        return {"sent": 0, "reason": "all meals closed"}

    answered_res = supabase.table("meal_prompt_answers").select("emp_id") \
        .eq("meal_date", str(today)).execute()
    answered = {r["emp_id"] for r in (answered_res.data or [])}

    subs_res = supabase.table("push_subscriptions").select("emp_id, endpoint, p256dh, auth").execute()

    sent, dropped = 0, 0
    hour = get_ist_now().hour
    title = ("Eating in today?" if hour < 11
             else "Still eating in today?" if hour < 17
             else "Last call for today")

    # One lookup per employee, not per device.
    usual_by_emp = {}

    for s in (subs_res.data or []):
        emp = s["emp_id"]
        if emp in answered:
            continue

        if emp not in usual_by_emp:
            u = usual_meal(emp)
            usual_by_emp[emp] = u if (u in meals) else None
        pick = usual_by_emp[emp]

        if pick:
            body = f"Tap to book {pick} — or Not today if you're out."
            action_label = f"Book {pick.capitalize()}"
            offer = [pick]
        else:
            body = "Open Canteen to book your meal."
            action_label = None
            offer = []

        payload = json.dumps({
            "title": title,
            "body": body,
            "emp_id": emp,
            "date": str(today),
            "meals": offer,
            "action_label": action_label,
        })
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=6 * 3600,
            )
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                supabase.table("push_subscriptions").delete().eq("endpoint", s["endpoint"]).execute()
                dropped += 1
            else:
                log.warning("Push failed for %s: %s", emp, e)
        except Exception as e:
            log.warning("Push error for %s: %s", emp, e)

    log.info("Reminders sent=%s dropped=%s meals=%s", sent, dropped, meals)
    return {"sent": sent, "dropped": dropped, "meals": meals, "date": str(today)}


# ── Dynamic date route — MUST stay last ───────────────────────────────────────
@app.get("/meals/{emp_id}/{meal_date}")
async def get_user_meals(emp_id: str, meal_date: str):
    d_obj = date.fromisoformat(meal_date)
    res = supabase.table("meal_registrations").select("meal_type") \
        .eq("emp_id", emp_id).eq("meal_date", str(d_obj)).execute()
    return {"registered_meals": [r["meal_type"] for r in res.data]}

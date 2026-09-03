# Canteen Portal — setup

---

## The logo

Earlier attempts tried to encode "don't waste food" into the glyph itself — a leaf-tick, a gauge, a measured portion. None of them read, and they were never going to. **An icon communicates a category, not a policy.** At 40px on a home screen, next to WhatsApp and the camera, the only question a mark can answer is "what is this app?"

So the mark is now a **hot plate of food**. Unmistakably the canteen, legible down to 22px, no explanation needed.

The concept lives where a concept can actually be read — in the **lockup**:

```
[plate]  Canteen
         COOK TO THE COUNT
```

That sits in the header of every screen, so the idea is in front of employees each time they book. Two more lines carry it further:

- Welcome screen — *"Tell the kitchen what you'll eat. They cook that many plates — no more, no less."*
- Footer — *"Cooked to the count, so nothing goes in the bin."*

Files replaced: `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, `favicon-64.png`. The maskable version sits inside Android's 80% safe zone. The header mark is inline SVG, so it follows the theme in dark mode.

If you want a printed version for canteen notice boards, the same lockup scales — mark, wordmark, tagline, nothing else.

---

## The two gaps in today+tomorrow, now closed

**Reminder timings now live in Supabase.** They were hardcoded. There's a new `app_settings` table:

| key | value |
|---|---|
| `reminder_hours` | `6,8,18` |

Edit that one row and the change takes effect within a minute — no redeploy, and **no editing the cron schedule**, because the cron now calls in every 30 minutes and the server decides whether a round is due. The portal reads the same setting for the chips on the reminders card, so what employees see always matches what actually fires.

Set to your requested 6 AM / 8 AM / 6 PM.

**The last round of the day asks about tomorrow.** Whichever hour is last in `reminder_hours` — 6 PM now — asks *"Eating in tomorrow?"*. The earlier rounds ask about today. Previously the evening round asked about today, which was near useless once lunch had closed, and tomorrow never got a prompt at all.

**The last round's chip follows tomorrow, not today.** Ordering today's dinner used to grey out all three chips, including the 6 PM one — but 6 PM asks about *tomorrow*, so that round was still due. The chip is now labelled `6 PM tomorrow` and only settles once tomorrow is answered. The server was always doing this correctly; only the display was wrong.

Same fix in the on-device fallback reminder, which was prompting for today at 6 PM instead of tomorrow.

**No duplicate buzzes.** The cron calls every 30 minutes and the in-process sweep runs hourly, so a new `reminder_rounds` table claims each `(date, hour)` once. A second call is a no-op instead of a second notification in someone's pocket.

**Per-employee weekly off.** The plant runs continuously and the canteen serves 365 days, so there is no company-wide closed day — `CANTEEN_CLOSED_WEEKDAYS` now defaults to empty and `canteen_holidays` should normally stay empty too. Rest days are individual, so they're stored per employee in `employee_settings`.

The portal asks once, the first time someone signs in: *"When's your weekly off?"* — seven buttons, plus "I don't have a fixed off". After that:

- **The header carries it** — an amber `Off Sun` chip sits next to their name on every screen. Tapping it changes the day.
- **The day tabs mark it** — "Tomorrow / 23 Aug · Off".
- **The status card warns** — *"This is your weekly off. Only book if you're coming in for audit or overtime."* If they already have a meal booked on that day, the warning flips to *"Cancel it if you're not coming in."*
- **Reminders stay silent** for that employee on that weekday only. Everyone else still gets theirs.

Crucially it's **a hint, never a block.** People come in for audits and overtime, and when they do they still need feeding — every meal stays bookable on an off day.

Set it in bulk if you already know: there's an insert at the bottom of `sql/schema.sql` that sets everyone to Sunday, which employees can then correct themselves.

**One source of truth.** The portal used to guess whether a meal was open by comparing clock times in the browser. It now asks the server: `GET /day/2026-08-23` returns whether the canteen serves that date, why not if it doesn't, and which meals are still open. Cutoffs, holidays and weekly offs are decided in one place, so a phone with the wrong clock or timezone can't book something it shouldn't.

**On the model itself:** today+tomorrow means nothing is ever stale, so there is no "how long did I register for" to forget. The cost is that everyone must act daily — which is exactly what the three reminder rounds are for. If turnout ever shows people are missing meals because they forgot to book, the standing-order model is still the fallback, and the `usual-meal` data the reminders already use would seed it.

---

## Keeping Render awake

Render's free tier spins a service down after ~15 minutes with no inbound request, and the cold start that follows takes 30–60 seconds. Two fixes, and **you want both**.

### The new `/ping` endpoint

```
GET /ping  →  {"ok": true, "ist": "2026-08-21 07:00:03"}
```

No database call, no auth. Point warm-up traffic here rather than `/health`, which queries Supabase on every hit.

### 1. Self-ping from inside the service

The service calls its own `/ping` on a timer. A request it makes to itself still counts as inbound traffic, so it never goes idle.

```
KEEPALIVE_URL       = https://canteen-portal-api.onrender.com
KEEPALIVE_MINUTES   = 12
KEEPALIVE_FROM_HOUR = 6      # IST
KEEPALIVE_TO_HOUR   = 22     # IST
```

**Why the window matters.** The free plan gives 750 instance-hours a month. Awake 24/7 is about 730 hours — it fits, but only barely, and only if this is your *only* free service. Add anything else on the free plan and you blow the cap and everything stops. 06:00–22:00 IST is roughly 490 hours, comfortably inside the limit, and covers every hour anyone actually books a meal. Static Sites don't consume instance-hours, so the frontend is free either way.

**The catch:** a sleeping process can't wake itself. If the service does go down — a deploy, a crash, a Render restart, or the first morning after the overnight window — nothing inside it can bring it back. That's what the second half is for.

### 2. External cron — the reliable half

`.github/workflows/canteen-cron.yml` is included and ready to commit. It warms the service every 10 minutes during canteen hours and fires the reminder rounds, retrying five times so a cold start never drops a round.

Add two repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `API_BASE` | `https://canteen-portal-api.onrender.com` |
| `REMINDER_SECRET` | same value as on the Render service |

Free for public repos; 2,000 minutes/month on private ones, and this uses a few seconds per run.

Prefer a hosted monitor? cron-job.org and UptimeRobot both work — point them at `/ping` every 10 minutes, and add three POSTs to `/push/run-reminders` with the `X-Reminder-Secret` header at 07:00, 12:00 and 20:00 IST.

**Two things worth being straight about.** GitHub's scheduler runs on UTC and can lag 5–15 minutes when their queue is busy, so a 7:00 reminder may land at 7:10 — the warm-up runs early to absorb this, but don't expect second-precision. And keeping a free instance permanently awake is working around the spin-down rather than paying for it; if the whole plant ends up depending on this, Render's paid Starter tier removes the problem and costs less than the time you'll spend babysitting it.

---

## Notifications not arriving — work through this

### Does the phone get a notification if it was offline?

**Yes.** Web push is queued by Google's FCM servers and delivered when the device comes back online. Nothing is lost by being on a train or having data off.

The one limit is the message's TTL. It's now set to expire at the last meal cutoff for the date being asked about, rather than a flat 6 hours — a phone that's been offline all morning shouldn't buzz at 4 PM asking about a lunch that closed at 3. If the phone is still offline past that point, the message is dropped rather than delivered late and useless.

### Finding out why nothing arrives

Start with the server, which now tells you directly:

```
GET https://canteen-api-service.onrender.com/push/diagnose
     -H "X-Reminder-Secret: <yours>"        (header only needed if you set one)
```

It reports whether VAPID is configured, how many subscriptions exist, which employees are subscribed, which rounds already went out today, whether a round is due right now — and a `problems` list in plain English.

Then send yourself a real one:

```
POST /push/test    body: {"emp_id":"4424"}
     -H "X-Reminder-Secret: <yours>"
```

This ignores rounds, answers and weekly offs and pushes immediately, returning the provider's actual response per device. What you get back tells you which of these it is:

| Result | What it means |
|---|---|
| `sent` | Push works. If the 6 AM round still doesn't fire, the problem is the cron, not push. |
| `No subscription stored for …` | Nobody enabled reminders on that phone. Open the portal, sign in, tap **Turn on reminders**, tap **Allow**. |
| `rejected (403) — VAPID key mismatch` | The phone subscribed with a different public key than the server signs with. See below. |
| `gone (404/410)` | Stale subscription; it's been deleted automatically. Re-enable on the phone. |
| `VAPID_PRIVATE_KEY is not set` | The keys are on the wrong Render service. They belong on the **Web Service**. |

### "Missing 'sub' from claims"

`VAPID_SUBJECT` is blank, or it's an email address without the `mailto:` prefix. Both produce this identical message, which makes a missing prefix look like a missing variable.

```
VAPID_SUBJECT = mailto:canteen@gcpl.live     ✅
VAPID_SUBJECT = canteen@gcpl.live            ❌ same error as blank
VAPID_SUBJECT = GCPL Canteen                 ❌ not an address at all
```

The server now repairs a bare email automatically and refuses to burn a reminder round when the value is unusable — `/push/diagnose` names it, `/push/test` returns 503 with the reason, and `/health` reports `vapid_subject_valid`. But set it correctly on the Render **Web Service** regardless; relying on the repair is one more thing to remember later.

### The 403 mismatch

A push subscription is permanently bound to the VAPID public key it was created with. If anyone enabled reminders **before** you set the keys, or you regenerated them at any point, every push to that device is rejected with 403 — and the browser gives no hint at all, it just goes silent. That matches "not receiving the browser notification" exactly.

The portal now detects this: it compares the stored subscription's key against the server's current one and silently re-subscribes when they differ. So the fix is to **deploy the new frontend and have people open the portal once**. No action needed from them beyond opening it.

### On the phone itself

The reminders card now has **Send a test notification** once reminders are on. It proves permission, service worker and display on that handset. If that works but the server test doesn't, the problem is between the server and FCM, not the phone.

### Still nothing?

- Chrome must be allowed to show notifications for the site — check Chrome → site settings.
- On Xiaomi, Oppo, Vivo and OnePlus, exempt Chrome from battery optimisation.
- On iPhone the page must be added to the Home Screen first; Safari will not deliver push otherwise.
- After deploying a new `sw.js`, close every tab of the site once so the old worker is replaced.

---

## Why the tick used to lag or come back on

Three bugs, all fixed:

**A stale response overwrote your tap.** `refresh()` assigned `regs[d] = new Set(server_answer)` unconditionally. If a meal-list request was already in flight when you cancelled a meal, that request's answer — fetched *before* the cancel — landed afterwards and put the tick straight back on. This is the "unregister, text says cancelled, tick still there, goes off later" symptom exactly. Refreshes are now stamped with a mutation counter and discarded if you changed anything after they were issued.

**A double tap on a slow server unticked a saved meal.** `/meals/register` returned HTTP 409 "Already registered", and the client treated every error as failure and rolled the tick back — while the booking existed in the database. That's the "shows unchecked, refresh shows checked" symptom. On a cold Render instance a request can take most of a minute, so tapping twice was the natural thing to do. The endpoint is now idempotent (200, `already_registered`), and the client treats an already-in-that-state answer as success either way, so an older backend is handled too.

**The row dimmed to 60% opacity while saving,** which made a successful tap look like it hadn't registered. The state you chose now stays fully readable; if the server takes more than 1.2 seconds the checkbox pulses instead.

Rapid taps also coalesce now — tap on/off/on quickly and only the final intent is sent, rather than racing requests whose completion order decides the outcome.

---

## Why there's no home-screen popup

Those old carrier prompts — flash SMS, USSD menus, SIM Toolkit — run below the operating system, on the SIM and baseband. That's why they could interrupt anything on screen. Nothing in a browser can reach that layer, and it isn't a browser limitation you can work around: even a **native** Android app can't do it any more, since Android 14 restricted full-screen intents to calling and alarm apps.

The notification shade is the ceiling. What we do inside it:

- **One-tap ordering without opening anything.** The notification carries an **Order Lunch** button — naming whichever meal that employee books most often — plus **Not today**. Android Chrome allows a maximum of two action buttons (`Notification.maxActions`), so it can't be a four-meal menu.
- **Tapping the notification body opens the meal picker directly**, on the correct date, with every meal as a large button. Roughly a second from tap to ordered, no navigating.
- **Sticky and buzzing** — `requireInteraction`, vibration, and a tag that replaces the earlier round rather than stacking.

If a genuinely interruptive prompt is essential, the realistic route isn't technical — it's the biometric canteen machine or a shop-floor display, not the phone.

---

## Where each variable goes

There are **three separate places**, and they never see each other. Render environment variables are invisible to GitHub Actions; static-site variables are invisible to the backend.

| Variable | Goes on | Why |
|---|---|---|
| `VITE_API_URL` | **Static Site** (canteen) | Baked into `env.js` at build time so the browser knows where the API is |
| `VAPID_PUBLIC_KEY` | **Web Service** (canteen-api-service) | Served to browsers via `/push/public-key` |
| `VAPID_PRIVATE_KEY` | **Web Service** | Signs each push. Never leaves the server |
| `VAPID_SUBJECT` | **Web Service** | Contact address in the push claim |
| `REMINDER_SECRET` | **Web Service** + **GitHub** (Secrets) | Both ends must match |
| `KEEPALIVE_URL` | **Web Service** | The service's own URL, for self-pinging |
| `API_BASE` | **GitHub** (Variables) — optional | Which host the cron pings. Has a default in the workflow |

### Two mistakes worth avoiding

**VAPID keys on the Static Site do nothing.** The static site is HTML and JS files; there's no server there to sign a notification. That's why `/health` reported `"push": false` — the web service never received them. Move all three to the **Web Service**, then redeploy it.

While you're there, **delete `VAPID_PRIVATE_KEY` from the Static Site.** Static-site variables are exposed to the build, so any build step that echoes or writes it would publish your signing key to the web. `build.sh` doesn't, so nothing has leaked — but there's no reason to leave a private key somewhere it can only cause harm.

**`API_BASE` on Render doesn't reach GitHub Actions.** That's what the cron failure was telling you. It's a GitHub repository variable, and the workflow now defaults to `https://canteen-api-service.onrender.com`, so you can leave it unset entirely unless the backend URL changes.

---

## Deploy checklist

**Static Site (frontend)**

1. Build Command `bash build.sh` · Publish Directory `frontend`
2. Environment variable `VITE_API_URL` = your backend URL
3. Redeploy — Render only injects env vars during a build

**Web Service (backend)**

4. Run `sql/schema.sql` in Supabase (adds `employee_settings`, `app_settings`, `reminder_rounds`)
5. Generate the VAPID keys **on your own machine** — `pip install py-vapid` then `python tools/gen-vapid.py`. Set the three values it prints, plus `REMINDER_SECRET` (any long random string). See "Generating VAPID keys" below.
6. Set `KEEPALIVE_URL` to the service's own URL (leave `CANTEEN_CLOSED_WEEKDAYS` unset — you serve 365 days)
7. Redeploy, then confirm `GET /health` reports `"push": true, "keepalive": true`

**Cron**

8. Commit `.github/workflows/canteen-cron.yml`, add the two secrets, then trigger it by hand from the Actions tab. The manual run uses `?force=true` so it sends immediately instead of waiting for a scheduled hour.

---

## Wording: "order canteen food", not "eating in"

A good share of employees bring a tiffin from home, and *"Are you eating in?"* is ambiguous to them — they are eating in, just not from the canteen. Every prompt now names the canteen explicitly:

| Was | Now |
|---|---|
| Are you eating in? | Order your canteen meal |
| Eating in today? | Canteen food today? |
| Not eating in today | Not ordering today |
| Lunch booked. | Lunch ordered. |
| Book by 3:00 PM | Order by 3:00 PM |
| Book Lunch *(notification button)* | Order Lunch |

The prompt sheet's decline button reads **"Not ordering — bringing my own"**, which is the honest option for tiffin-carriers rather than making them pick something that sounds like they're skipping lunch.

---

## Generating VAPID keys

**Do this once, on your own laptop — not on Render, and don't add `py-vapid` to `requirements.txt`.** The server already has it: `pywebpush` depends on `py-vapid`, so it's installed transitively and can sign notifications without the CLI.

```bash
pip install py-vapid
python tools/gen-vapid.py
```

That prints three single-line values to paste into Render → your API service → Environment.

**Why not `vapid --gen`?** It works, but it writes the private key as a multi-line PEM:

```
-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgoFNPIONn3zAi34Lk
...
-----END PRIVATE KEY-----
```

Multi-line values are awkward to paste into an environment variable and easy to mangle — a stray newline or a missing header line produces a key that fails only at the moment you try to send a notification. `gen-vapid.py` emits the same key as one line of URL-safe base64, which `pywebpush` accepts directly. The public key it prints is byte-identical to what `vapid --applicationServerKey` would give you.

Keep the private key secret — anyone holding it can push notifications to your employees. If it ever leaks, generate a new pair; every employee is prompted to re-enable reminders once, and old subscriptions stop working.

---

## Troubleshooting

### The cron job fails with "attempt N returned timeout"

Check how long the job ran. Five retries with 20s sleeps is ~1m40s of *sleeping*, which means each curl failed instantly rather than timing out — a real cold start burns up to 90 seconds per attempt and takes ~8 minutes. An instant failure means curl never made a request at all.

Almost always: **`API_BASE` is empty or misnamed.** The URL becomes `/ping`, and curl rejects it with `exit=3, No host part in the URL`.

The workflow now checks this before it starts and tells you plainly. It also distinguishes the other cases: exit 6 is a hostname that doesn't resolve, HTTP 404 means the deployed backend predates `/ping` and needs a redeploy.

Tip: set `API_BASE` as a **repository variable**, not a secret. It's a public URL, and secrets are masked in logs — an empty secret and a working one look identical (`***`), which is what makes this failure so confusing to read.

### `/health` says `"push": false`

`VAPID_PRIVATE_KEY` isn't set on the Render service, so `send_daily_reminders()` returns `{"sent": 0, "reason": "push not configured"}` and no notification can ever go out. The cron will look like it's succeeding. Generate the keys and set all three VAPID variables.

### `/health` says `"keepalive": false`

`KEEPALIVE_URL` isn't set, so the service isn't pinging itself. The external cron still keeps it warm, so this is the less important of the two — but set it to the service's own URL for the belt-and-braces version.

---

## Things worth knowing

- **HTTPS is required** for push and service workers, except on `localhost`.
- **Android Chrome** is the good case — notifications arrive with the browser closed.
- **iPhone**: Safari only allows push after the page is added to the Home Screen (iOS 16.4+). Chrome on iOS is Safari underneath, same rule.
- **Battery savers** on Xiaomi, Oppo, Vivo and OnePlus suppress background notifications. Exempt Chrome if reminders go missing.
- **Battery drain from the portal is negligible** — push rides on the connection Android already keeps open for every app, and the service worker wakes for a few hundred milliseconds per notification. No polling, no wake locks, no background service.
- **After deploying, tell testers to close all tabs of the site once** so the old service worker is replaced.
- `canteen_tokens` still only allows breakfast/lunch/dinner, so snacks bookings won't show a collected token until you widen that check constraint — see the note at the bottom of `sql/schema.sql`.

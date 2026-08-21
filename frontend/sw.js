/* ═══════════════════════════════════════════════════════
   Canteen Portal — service worker
   Shows the daily reminder and lets the employee answer
   Yes or No without opening the app.
   ═══════════════════════════════════════════════════════ */

const API   = "https://canteen-portal-api.onrender.com";
const CACHE = "canteen-v2";
const SHELL = ["./", "./index.html", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", event => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* Network first so meal status is always live; cache is the offline safety net.
   API calls are never cached. */
self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;
  if (req.url.indexOf(API) === 0) return;
  if (new URL(req.url).origin !== self.location.origin) return;

  event.respondWith(
    fetch(req)
      .then(res => {
        if (res && res.status === 200 && res.type === "basic") {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req).then(hit => hit || caches.match("./index.html")))
  );
});

/* ── A reminder arrives ─────────────────────────────── */
self.addEventListener("push", event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) {}

  event.waitUntil(
    self.registration.showNotification(d.title || "Eating in today?", {
      body: d.body || "Tap Yes to book breakfast, lunch and dinner.",
      icon: "./icon-192.png",
      badge: "./icon-192.png",
      tag: "canteen-" + (d.date || "prompt"),   // a later reminder replaces the earlier one
      renotify: true,
      requireInteraction: true,                 // stays put until answered
      vibrate: [80, 40, 80],
      data: {
        emp_id: d.emp_id || null,
        date:   d.date   || null,
        meals:  d.meals  || []
      },
      actions: [
        { action: "yes", title: "Yes" },
        { action: "no",  title: "No"  }
      ]
    })
  );
});

/* ── Yes / No tapped ────────────────────────────────── */
self.addEventListener("notificationclick", event => {
  const action = event.action;
  const d = event.notification.data || {};
  event.notification.close();

  if (action !== "yes" && action !== "no") {
    event.waitUntil(openApp());
    return;
  }

  const meals = action === "yes" ? (d.meals || []) : [];

  event.waitUntil(
    fetch(API + "/meals/respond", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ emp_id: d.emp_id, meal_date: d.date, meals: meals })
    })
      .then(res => {
        if (!res.ok) throw new Error("save failed");
        return self.registration.showNotification(
          action === "yes" ? "Meals booked" : "Saved — no meals",
          {
            body: action === "yes"
              ? "You're on the list. Open Canteen to change any meal."
              : "You won't be counted for today.",
            icon: "./icon-192.png",
            badge: "./icon-192.png",
            tag: "canteen-done-" + (d.date || ""),
            silent: true
          }
        );
      })
      .then(() => tellPages(d.date))
      .catch(() =>
        self.registration.showNotification("Couldn't save your answer", {
          body: "Tap here to open Canteen and book from the app.",
          icon: "./icon-192.png",
          badge: "./icon-192.png",
          tag: "canteen-err"
        })
      )
  );
});

/* ── Helpers ────────────────────────────────────────── */
function openApp() {
  return self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (c.url.indexOf(self.registration.scope) === 0 && "focus" in c) return c.focus();
    }
    return self.clients.openWindow("./index.html");
  });
}

function tellPages(date) {
  return self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    list.forEach(c => c.postMessage({ type: "meals-updated", date: date }));
  });
}

/* Chrome rotates push subscriptions occasionally — re-register */
self.addEventListener("pushsubscriptionchange", event => {
  event.waitUntil(
    fetch(API + "/push/public-key")
      .then(r => r.json())
      .then(({ key }) => self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: b64ToBytes(key)
      }))
      .then(sub => {
        const j = sub.toJSON();
        const old = event.oldSubscription;
        return fetch(API + "/push/resubscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            old_endpoint: old ? old.endpoint : null,
            endpoint: sub.endpoint,
            p256dh: j.keys.p256dh,
            auth: j.keys.auth
          })
        });
      })
      .catch(() => {})
  );
});

function b64ToBytes(base64) {
  const pad = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

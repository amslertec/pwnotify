// PwNotify Service Worker — minimal. Zweck: PWA-Installierbarkeit (Chrome/Android
// verlangt einen registrierten SW mit fetch-Handler). Bewusst KEIN aggressives
// Caching — die App zeigt Live-Daten. Nur die App-Shell wird für den Offline-
// Fallback vorgehalten; API-Requests laufen immer frisch übers Netz.
const CACHE = 'pwnotify-shell-v1'

self.addEventListener('install', (event) => {
  self.skipWaiting()
  event.waitUntil(caches.open(CACHE).then((c) => c.add('/')))
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  // Nur Navigationen behandeln: network-first mit App-Shell-Fallback (offline).
  // Alle anderen Requests (Assets, API) laufen im Standard-Netzwerkverhalten.
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match('/')))
  }
})

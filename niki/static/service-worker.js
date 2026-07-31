/*
 * Niki service worker — Web Push receiver (BE-PUSH-003).
 *
 * Registered by the parent-facing web app so a browser can surface
 * safety alerts even when every Niki tab is closed. The worker is
 * deliberately tiny: it renders what the server sent and gets out of
 * the way. Anything that needs app state belongs in the page, not
 * here — a service worker has no DOM and may be killed between
 * events.
 *
 * Served from /static/service-worker.js. Note the scope: a worker
 * served from /static/ only controls /static/ by default, so the page
 * registers it with an explicit `{ scope: '/' }` and the server sends
 * `Service-Worker-Allowed: /` (see push-helper.js).
 */

'use strict';

const DEFAULT_TITLE = 'Niki';
const DEFAULT_ICON = '/static/icons/notification-192.png';
const DEFAULT_BADGE = '/static/icons/badge-72.png';

/**
 * Parse the push payload. The server sends `PushPayload.to_dict()`
 * as JSON; fall back to plain text so a malformed payload still
 * shows *something* rather than silently dropping an alert.
 */
function readPayload(event) {
  if (!event.data) {
    return { title: DEFAULT_TITLE, body: '' };
  }
  try {
    return event.data.json();
  } catch (err) {
    return { title: DEFAULT_TITLE, body: event.data.text() };
  }
}

self.addEventListener('push', (event) => {
  const payload = readPayload(event);
  const title = payload.title || DEFAULT_TITLE;
  const data = payload.data || {};

  const options = {
    body: payload.body || '',
    icon: payload.icon || DEFAULT_ICON,
    badge: payload.badge || DEFAULT_BADGE,
    data: data,
    // `critical` alerts must not be silently coalesced away by the
    // OS: keep them on screen until the parent acts on them.
    requireInteraction: payload.severity === 'critical',
    // The server's dedupe_key doubles as the notification tag, so a
    // repeated alert replaces the old bubble instead of stacking.
    tag: payload.dedupe_key || undefined,
  };

  // waitUntil keeps the worker alive until the notification is shown.
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const data = event.notification.data || {};
  const targetUrl = data.url || '/';

  event.waitUntil(
    self.clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then((clientList) => {
        // Prefer focusing an already-open Niki tab — opening a second
        // one loses whatever the parent was in the middle of.
        for (const client of clientList) {
          if ('focus' in client) {
            if (client.url === targetUrl) {
              return client.focus();
            }
          }
        }
        for (const client of clientList) {
          if ('navigate' in client && 'focus' in client) {
            return client.focus().then((focused) => focused.navigate(targetUrl));
          }
        }
        if (self.clients.openWindow) {
          return self.clients.openWindow(targetUrl);
        }
        return undefined;
      })
  );
});

/**
 * base64url -> Uint8Array, for `applicationServerKey`.
 *
 * Duplicated from push-helper.js on purpose: a service worker can't
 * import from the page's module graph, and re-subscribing after a
 * `pushsubscriptionchange` has to work without any page open.
 */
function urlB64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = self.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

/**
 * Browsers rotate subscriptions (key rotation, storage pressure,
 * push-service migration). When that happens the old endpoint stops
 * working *silently* — so we re-subscribe and re-register with the
 * backend immediately, without waiting for the parent to open a tab.
 */
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(
    fetch('/api/v1/push/vapid-public-key')
      .then((response) => response.json())
      .then((body) =>
        self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlB64ToUint8Array(body.public_key),
        })
      )
      .then((subscription) =>
        fetch('/api/v1/push/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // Credentials so the parent's session cookie rides along —
          // there's no Authorization header available in a worker
          // woken up without a page.
          credentials: 'include',
          body: JSON.stringify({
            device_id: 'web-' + (self.registration.scope || '').slice(-32),
            channel: 'web',
            platform: 'web',
            push_token: JSON.stringify(subscription),
          }),
        })
      )
      .catch((err) => {
        // Nothing to show the parent — a failed re-subscribe surfaces
        // the next time they open the app and push-helper.js runs.
        console.error('[niki-sw] resubscribe failed', err);
      })
  );
});

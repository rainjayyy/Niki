/*
 * Niki Web Push subscription helper (BE-PUSH-003).
 *
 * Reference implementation of the browser half of the Web Push flow.
 * The real parent-facing frontend is out of scope for this ticket —
 * this file exists so a frontend engineer can copy four functions
 * instead of re-deriving RFC 8292 from a blog post.
 *
 * Flow:
 *   1. GET  /api/v1/push/vapid-public-key   → server's public key
 *   2. navigator.serviceWorker.register('/static/service-worker.js')
 *   3. registration.pushManager.subscribe({ applicationServerKey })
 *   4. POST /api/v1/push/register           → store the subscription
 *
 * Usage (as an ES module):
 *
 *     import { enableWebPush } from '/static/push-helper.js';
 *     await enableWebPush({ authToken: accessToken });
 */

'use strict';

export const SERVICE_WORKER_URL = '/static/service-worker.js';
export const VAPID_KEY_URL = '/api/v1/push/vapid-public-key';
export const REGISTER_URL = '/api/v1/push/register';
export const UNREGISTER_URL = '/api/v1/push/unregister';

/**
 * Decode a base64url string into the Uint8Array `pushManager.subscribe`
 * expects for `applicationServerKey`.
 *
 * Two transformations, both required:
 *   - re-pad to a multiple of 4 (base64url drops '=' padding)
 *   - map the URL-safe alphabet ('-', '_') back to standard ('+', '/')
 *
 * @param {string} base64String base64url, unpadded.
 * @returns {Uint8Array} 65 raw bytes (0x04 || X || Y) for a P-256 key.
 */
export function urlB64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

/** True iff this browser can do Web Push at all. */
export function isPushSupported() {
  return (
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window
  );
}

/** Fetch the server's VAPID public key. */
export async function fetchVapidPublicKey() {
  const response = await fetch(VAPID_KEY_URL);
  if (!response.ok) {
    // i18n key: push.web_vapid_key_unavailable
    throw new Error('push.web_vapid_key_unavailable');
  }
  const body = await response.json();
  return body.public_key;
}

/**
 * Register the service worker and create (or reuse) a subscription.
 *
 * Reusing an existing subscription matters: calling subscribe() with
 * a *different* applicationServerKey throws, and creating a second
 * subscription for the same browser would double every alert.
 *
 * @param {string} vapidPublicKey base64url public key from the server.
 * @returns {Promise<PushSubscription>}
 */
export async function subscribeToPush(vapidPublicKey) {
  if (!isPushSupported()) {
    // i18n key: push.web_unsupported_browser
    throw new Error('push.web_unsupported_browser');
  }

  const registration = await navigator.serviceWorker.register(
    SERVICE_WORKER_URL,
    // Explicit scope: a worker served from /static/ would otherwise
    // only control /static/. Requires `Service-Worker-Allowed: /`.
    { scope: '/' }
  );
  await navigator.serviceWorker.ready;

  const existing = await registration.pushManager.getSubscription();
  if (existing) {
    return existing;
  }

  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    // i18n key: push.web_subscription_failed
    throw new Error('push.web_subscription_failed');
  }

  return registration.pushManager.subscribe({
    // Chrome *requires* true: a push must always produce a visible
    // notification. Silent pushes are not an option.
    userVisibleOnly: true,
    applicationServerKey: urlB64ToUint8Array(vapidPublicKey),
  });
}

/**
 * Full opt-in flow: key → subscribe → register with the backend.
 *
 * @param {{ authToken?: string, deviceId?: string }} options
 * @returns {Promise<PushSubscription>}
 */
export async function enableWebPush(options = {}) {
  const vapidPublicKey = await fetchVapidPublicKey();
  const subscription = await subscribeToPush(vapidPublicKey);

  const headers = { 'Content-Type': 'application/json' };
  if (options.authToken) {
    headers.Authorization = `Bearer ${options.authToken}`;
  }

  const response = await fetch(REGISTER_URL, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      // A stable per-browser id; the backend upserts on it, so a
      // re-subscribe replaces the row instead of piling up.
      device_id: options.deviceId || 'web-' + subscription.endpoint.slice(-32),
      channel: 'web',
      platform: 'web',
      // The whole PushSubscription — endpoint plus the p256dh/auth
      // keys the server needs to encrypt the payload.
      push_token: JSON.stringify(subscription),
    }),
  });
  if (!response.ok) {
    // i18n key: push.web_subscription_failed
    throw new Error('push.web_subscription_failed');
  }
  return subscription;
}

/** Tear down the subscription and tell the backend to forget it. */
export async function disableWebPush(options = {}) {
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    return false;
  }

  const headers = { 'Content-Type': 'application/json' };
  if (options.authToken) {
    headers.Authorization = `Bearer ${options.authToken}`;
  }
  const response = await fetch(UNREGISTER_URL, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      device_id: options.deviceId || 'web-' + subscription.endpoint.slice(-32),
    }),
  });
  const unsubscribed = await subscription.unsubscribe();
  if (!response.ok || !unsubscribed) {
    // i18n key: push.web_unsubscribe_failed
    throw new Error('push.web_unsubscribe_failed');
  }
  return true;
}

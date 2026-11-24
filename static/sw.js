/* Service Worker for the Salary-Linked Expense Tracker PWA.
 *
 * Caching strategy (aligned with NFR-05 security and NFR-01 performance):
 *   1. Static shell assets (CSS, JS, manifest, icons) and the /offline
 *      fallback screen are pre-cached on install and served Cache-First with
 *      background refresh.
 *   2. Navigation requests (HTML pages) are fetched Network-First and NEVER
 *      stored in persistent CacheStorage so that authenticated financial views
 *      cannot be read after sign-out or session timeout (section 5.9). When
 *      the server cannot be reached, navigation falls back cleanly to /offline.
 *   3. Non-GET requests (POST forms with CSRF tokens, file uploads) always go
 *      directly to the network.
 */
'use strict';

var CACHE_NAME = 'expense-tracker-shell-v9';
var OFFLINE_URL = '/offline';

var PRECACHE_ASSETS = [
    OFFLINE_URL,
    '/static/css/style.css',
    '/static/js/chart.umd.js',
    '/static/js/charts.js',
    '/static/js/pwa.js',
    '/static/manifest.json',
    '/static/icons/icon.svg',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/static/icons/icon-maskable-192.png',
    '/static/icons/icon-maskable-512.png'
];

// How long a page may take before the offline screen is shown instead. When
// the phone cannot reach the PC at all (the PC asleep, or the phone on mobile
// data), iOS Safari can leave the request open for a minute or more, and the
// installed app shows only its dark background: the "black screen" seen on
// the iPhone 13 and 14 Pro Max (D15). Pages normally load in under a second
// (NFR-01: at most 3 s), so 6 s only ever cuts off a server that is not there.
var NAVIGATION_TIMEOUT_MS = 6000;

function fetchWithTimeout(request, ms) {
    // Known offline: answer at once instead of waiting for the timeout.
    if (self.navigator && self.navigator.onLine === false) {
        return Promise.reject(new Error('offline'));
    }
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer;
    var timeout = new Promise(function (resolve, reject) {
        timer = setTimeout(function () {
            if (controller) controller.abort();
            reject(new Error('timeout'));
        }, ms);
    });
    var network = fetch(request, controller ? { signal: controller.signal } : undefined);
    return Promise.race([network, timeout]).then(function (response) {
        clearTimeout(timer);
        return response;
    }, function (error) {
        clearTimeout(timer);
        throw error;
    });
}

self.addEventListener('install', function (event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function (cache) {
            return cache.addAll(PRECACHE_ASSETS);
        }).then(function () {
            return self.skipWaiting();
        })
    );
});

self.addEventListener('activate', function (event) {
    event.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(
                keys.map(function (key) {
                    if (key !== CACHE_NAME) {
                        return caches.delete(key);
                    }
                    return null;
                })
            );
        }).then(function () {
            return self.clients.claim();
        })
    );
});

self.addEventListener('fetch', function (event) {
    var request = event.request;

    // Only intercept GET requests. POST forms (uploads, recategorisation, login)
    // carry CSRF tokens and must hit the server directly.
    if (request.method !== 'GET') {
        return;
    }

    var url = new URL(request.url);

    // Same-origin static assets: Cache-First with background update.
    if (url.origin === self.location.origin && url.pathname.indexOf('/static/') === 0) {
        event.respondWith(
            caches.match(request).then(function (cached) {
                var networkFetch = fetch(request).then(function (response) {
                    if (response && response.status === 200) {
                        var copy = response.clone();
                        caches.open(CACHE_NAME).then(function (cache) {
                            cache.put(request, copy);
                        });
                    }
                    return response;
                }).catch(function () {
                    return cached;
                });
                return cached || networkFetch;
            })
        );
        return;
    }

    // HTML navigation requests: Network-Only with /offline fallback.
    var accept = request.headers.get('accept') || '';
    if (request.mode === 'navigate' || accept.indexOf('text/html') !== -1) {
        event.respondWith(
            fetchWithTimeout(request, NAVIGATION_TIMEOUT_MS).catch(function () {
                return caches.match(OFFLINE_URL).then(function (cached) {
                    // Last resort if the offline page was never cached, so a
                    // navigation never resolves to an empty/failed response.
                    return cached || new Response(
                        '<!doctype html><meta charset="utf-8">' +
                        '<title>Offline</title><h1>You are offline</h1>' +
                        '<p>Start the Expense Tracker server and try again.</p>',
                        {
                            status: 503,
                            headers: { 'Content-Type': 'text/html; charset=utf-8' }
                        }
                    );
                });
            })
        );
    }
});


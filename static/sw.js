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

var CACHE_NAME = 'expense-tracker-shell-v6';
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
            fetch(request).catch(function () {
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


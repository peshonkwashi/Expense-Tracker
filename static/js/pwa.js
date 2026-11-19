/* Progressive Web App registration and install-prompt helper.
 *
 * Registers the root Service Worker (/sw.js) when running in a secure context
 * (localhost or HTTPS) and surfaces an "Install App" button in the topbar
 * whenever the browser fires `beforeinstallprompt` on mobile or desktop.
 */
(function () {
    'use strict';

    /* Registration outcome, kept so the Settings page can explain it. On a phone
     * the browser console is out of reach, so a failure that is only logged is
     * invisible. This promise records *why* offline mode is or isn't
     * available. */
    var swOutcome = new Promise(function (resolve) {
        if (!('serviceWorker' in navigator)) {
            resolve({ ok: false, reason: 'unsupported' });
            return;
        }
        if (!window.isSecureContext) {
            resolve({ ok: false, reason: 'insecure' });
            return;
        }
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js', { scope: '/' }).then(function (reg) {
                resolve({ ok: true, registration: reg });
            }).catch(function (err) {
                console.warn('Service worker registration skipped:', err);
                resolve({ ok: false, reason: 'failed',
                          error: String((err && err.message) || err) });
            });
        });
    });

    var deferredPrompt = null;

    window.addEventListener('beforeinstallprompt', function (event) {
        event.preventDefault();
        deferredPrompt = event;
        var btn = document.getElementById('install-pwa-btn');
        if (btn) {
            btn.hidden = false;
        }
    });

    window.addEventListener('appinstalled', function () {
        deferredPrompt = null;
        var btn = document.getElementById('install-pwa-btn');
        if (btn) {
            btn.hidden = true;
        }
    });

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('install-pwa-btn');
        if (!btn) {
            return;
        }
        btn.addEventListener('click', function () {
            if (!deferredPrompt) {
                return;
            }
            deferredPrompt.prompt();
            deferredPrompt.userChoice.then(function () {
                deferredPrompt = null;
                btn.hidden = true;
            });
        });
    });

    /* Mobile nav scroll affordance: on narrow screens the menu scrolls
     * horizontally with its scrollbar hidden, so entries past the right edge
     * (Settings, Sign out) give no hint that they exist. Flag the topbar while
     * items sit off-screen; the CSS then fades the nav's right edge and shows a
     * chevron. Cleared once the user scrolls to the end. */
    document.addEventListener('DOMContentLoaded', function () {
        var bar = document.querySelector('.topbar');
        var nav = bar && bar.querySelector('nav');
        if (!bar || !nav) {
            return;
        }
        function update() {
            var overflowing = nav.scrollWidth - nav.clientWidth > 4;
            var atEnd = nav.scrollLeft + nav.clientWidth >= nav.scrollWidth - 4;
            bar.classList.toggle('nav-overflow', overflowing && !atEnd);
        }
        nav.addEventListener('scroll', update, { passive: true });
        // scrollend fires once a swipe/momentum scroll fully settles; it catches
        // the final position even if the last 'scroll' event landed mid-flight.
        nav.addEventListener('scrollend', update, { passive: true });
        window.addEventListener('resize', update);
        update();
    });

    /* Settings → "Offline mode" indicator. Shows, on the device itself, whether
     * the offline fallback is ready and, if not, the reason in plain language.
     * The iOS case in particular cannot be diagnosed any other way without a
     * Mac. Only runs on pages that contain #offline-status. All text is set with
     * textContent, never innerHTML. */
    function isInstalledApp() {
        return window.matchMedia('(display-mode: standalone)').matches ||
               window.navigator.standalone === true;  // iOS home-screen app
    }

    function isIosBrowserTab() {
        // navigator.standalone exists only on iOS Safari; false = a normal tab.
        return typeof window.navigator.standalone === 'boolean' &&
               !window.navigator.standalone;
    }

    document.addEventListener('DOMContentLoaded', function () {
        var badge = document.getElementById('offline-status');
        if (!badge) {
            return;
        }
        var detail = document.getElementById('offline-status-detail');
        var mode = document.getElementById('offline-status-mode');
        if (mode) {
            mode.textContent = isInstalledApp() ? 'Installed app' : 'Browser tab';
        }

        function show(state, label, text) {
            badge.className = 'badge ' + state;
            badge.textContent = label;
            if (detail) {
                var iosNote = (state !== 'ok' && isIosBrowserTab())
                    ? ' On iPhone, also check this screen from the home-screen app: ' +
                      'it sets up offline mode separately from Safari.'
                    : '';
                detail.textContent = text + iosNote;
            }
        }

        var NOT_AVAILABLE = 'Not available';
        swOutcome.then(function (out) {
            if (!out.ok) {
                if (out.reason === 'unsupported') {
                    show('warning', NOT_AVAILABLE,
                         'This browser does not support offline mode.');
                } else if (out.reason === 'insecure') {
                    show('warning', NOT_AVAILABLE,
                         'Offline mode needs a secure connection: open the app at ' +
                         'localhost (or over USB), or over HTTPS with a certificate ' +
                         'this device fully trusts.');
                } else {
                    show('warning', NOT_AVAILABLE,
                         'The browser refused to set up offline mode ("' + out.error +
                         '"). If you connected over HTTPS with a local certificate, ' +
                         'make sure this device fully trusts it. On iPhone: Settings ' +
                         '→ General → About → Certificate Trust Settings.');
                }
                return;
            }
            // Registered: wait (max 10 s) for it to activate and fill its cache.
            var timeout = new Promise(function (resolve) {
                setTimeout(function () { resolve('timeout'); }, 10000);
            });
            return Promise.race([navigator.serviceWorker.ready, timeout]).then(function (r) {
                if (r === 'timeout') {
                    show('warning', NOT_AVAILABLE,
                         'The offline files could not be downloaded. Reload this ' +
                         'page to try again.');
                    return;
                }
                return caches.keys().then(function (keys) {
                    var cached = keys.some(function (k) {
                        return k.indexOf('expense-tracker-shell-') === 0;
                    });
                    if (navigator.serviceWorker.controller && cached) {
                        show('ok', 'Ready',
                             'If the server cannot be reached, this app shows an ' +
                             'offline notice instead of an error. Your financial ' +
                             'pages are never stored on this device.');
                    } else {
                        show('warning', 'Setting up',
                             'Offline mode was just installed. Reload this page once ' +
                             'to finish setting it up.');
                    }
                });
            });
        }).catch(function () {
            show('warning', NOT_AVAILABLE,
                 'Offline mode status could not be checked on this device.');
        });
    });
})();


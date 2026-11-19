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

    /* Theme controller: supports 'system' (default), 'light', and 'dark'.
     * Persists the user's choice in localStorage ('expense-tracker-theme'),
     * updates data-theme on <html>, syncs both the topbar quick-cycle button
     * and the Settings 3-way segmented control, updates <meta name="theme-color">,
     * and dispatches a 'themechange' event so Chart.js redraws its axes. */
    var THEME_KEY = 'expense-tracker-theme';
    var THEME_DESCRIPTIONS = {
        system: "Following your device's system appearance",
        light: 'Always using Light mode on this browser.',
        dark: 'Always using Dark mode on this browser.'
    };
    var darkMedia = window.matchMedia
        ? window.matchMedia('(prefers-color-scheme: dark)')
        : null;

    function getSavedTheme() {
        try {
            var val = localStorage.getItem(THEME_KEY);
            if (val === 'light' || val === 'dark' || val === 'system') {
                return val;
            }
        } catch (e) {}
        return 'system';
    }

    function resolveEffectiveTheme(mode) {
        if (mode === 'light' || mode === 'dark') {
            return mode;
        }
        return (darkMedia && darkMedia.matches) ? 'dark' : 'light';
    }

    function applyTheme(mode, persist) {
        var validMode = (mode === 'light' || mode === 'dark' || mode === 'system')
            ? mode : 'system';
        if (persist) {
            try {
                localStorage.setItem(THEME_KEY, validMode);
            } catch (e) {}
        }
        var effective = resolveEffectiveTheme(validMode);
        document.documentElement.setAttribute('data-theme', validMode);
        document.documentElement.setAttribute('data-resolved-theme', effective);

        var metaTheme = document.querySelector('meta[name="theme-color"]');
        if (metaTheme) {
            metaTheme.setAttribute('content', effective === 'dark' ? '#0f3d21' : '#14532d');
        }

        document.querySelectorAll('[data-theme-set]').forEach(function (btn) {
            var target = btn.getAttribute('data-theme-set');
            btn.setAttribute('aria-pressed', target === validMode ? 'true' : 'false');
        });

        var detail = document.getElementById('theme-status-detail');
        if (detail) {
            if (validMode === 'system') {
                detail.textContent = THEME_DESCRIPTIONS.system +
                    ' (currently ' + (effective === 'dark' ? 'Dark' : 'Light') + ').';
            } else {
                detail.textContent = THEME_DESCRIPTIONS[validMode];
            }
        }

        try {
            window.dispatchEvent(new CustomEvent('themechange', {
                detail: { mode: validMode, effective: effective }
            }));
        } catch (e) {}
    }

    document.addEventListener('DOMContentLoaded', function () {
        applyTheme(getSavedTheme(), false);

        document.querySelectorAll('[data-theme-set]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                applyTheme(btn.getAttribute('data-theme-set'), true);
            });
        });

        if (darkMedia && typeof darkMedia.addEventListener === 'function') {
            darkMedia.addEventListener('change', function () {
                if (getSavedTheme() === 'system') {
                    applyTheme('system', false);
                }
            });
        }
    });

    /* "Show password" checkbox: toggles every password input in the same form
     * between type="password" and type="text" so the user can verify what they
     * typed before submitting. */
    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('input[type="password"]').forEach(function (input) {
            input.setAttribute('data-password-field', '');
        });
        function setVisible(box, visible) {
            var scope = box.form || document;
            box.checked = visible;
            scope.querySelectorAll('input[data-password-field]').forEach(function (field) {
                field.type = visible ? 'text' : 'password';
            });
        }
        var boxes = document.querySelectorAll('input[data-toggle-passwords]');
        boxes.forEach(function (box) {
            box.addEventListener('change', function () { setVisible(box, box.checked); });
            // Hide again before submitting, so the browser's password manager
            // still recognises the field and offers to save the password.
            if (box.form) {
                box.form.addEventListener('submit', function () { setVisible(box, false); });
            }
        });
        // Coming back with the Back button restores the page from cache; never
        // leave a password showing there.
        window.addEventListener('pageshow', function () {
            boxes.forEach(function (box) { setVisible(box, false); });
        });
    });

    /* Settings → "Share backup…": on phones, hand the backup .zip to the share
     * sheet (Drive, iCloud Files, WhatsApp, email…) instead of leaving it in
     * Downloads. Only shown where the browser can share files. */
    document.addEventListener('DOMContentLoaded', function () {
        var button = document.getElementById('backup-share');
        var link = document.getElementById('backup-download');
        var status = document.getElementById('backup-share-status');
        if (!button || !link || !navigator.canShare || !window.File) return;
        try {
            var probe = new File(['x'], 'probe.zip', { type: 'application/zip' });
            if (!navigator.canShare({ files: [probe] })) return;
        } catch (e) { return; }
        var label = button.textContent;
        var prepared = null;   // the backup File, once downloaded

        function share(file) {
            return navigator.share({ files: [file], title: file.name }).then(function () {
                prepared = null;
                button.textContent = label;
                status.textContent = '';
            }, function (error) {
                if (error && error.name === 'AbortError') {
                    status.textContent = '';   // the user closed the share sheet
                } else if (error && error.name === 'NotAllowedError') {
                    // Safari only shares straight from a tap, and preparing the
                    // file took too long. It is ready now, so a second tap works.
                    button.textContent = 'Tap again to share';
                    status.textContent = 'The backup is ready.';
                } else {
                    prepared = null;
                    button.textContent = label;
                    status.textContent = 'Sharing didn’t work here. Use ' +
                        '"Download a backup" instead.';
                }
            });
        }

        button.hidden = false;
        button.addEventListener('click', function () {
            if (prepared) { share(prepared); return; }
            button.disabled = true;
            status.textContent = 'Preparing the backup…';
            fetch(link.href, { credentials: 'same-origin' }).then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                var match = /filename="?([^";]+)"?/.exec(
                    response.headers.get('Content-Disposition') || '');
                var name = match ? match[1] : 'expense_tracker-backup.zip';
                return response.blob().then(function (blob) {
                    prepared = new File([blob], name, { type: 'application/zip' });
                    return share(prepared);
                });
            }).catch(function () {
                status.textContent = 'The backup could not be prepared. Use ' +
                    '"Download a backup" instead.';
            }).then(function () { button.disabled = false; });
        });
    });
})();


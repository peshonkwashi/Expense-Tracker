/* Progressive Web App registration and install-prompt helper.
 *
 * Registers the root Service Worker (/sw.js) when running in a secure context
 * (localhost or HTTPS) and surfaces an "Install App" button in the topbar
 * whenever the browser fires `beforeinstallprompt` on mobile or desktop.
 */
(function () {
    'use strict';

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function (err) {
                console.warn('Service worker registration skipped:', err);
            });
        });
    }

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
})();


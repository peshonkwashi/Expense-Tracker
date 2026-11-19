# Running on a phone (PWA install) — for UAT and demos

The app is a Progressive Web App: on a phone it can be installed to the home
screen and runs full-screen. A phone reaches the app as a client on the same
machine's loopback or local network — the data never leaves your host, so the
NFR-05 "nothing is transmitted" guarantee holds.

A service worker (and therefore PWA install) only works in a **secure context**:
`localhost`, or HTTPS with a certificate the phone **trusts**. That gives two
practical paths.

---

## Quickest for UAT — Android over USB (no certificates)

`adb reverse` maps the phone's `localhost:5000` to this PC's loopback, so the
phone sees `http://localhost:5000` — a secure context — with no certs and nothing
exposed to the network. This is the recommended path for user acceptance testing.

**One-time setup:**
1. Install Android platform-tools: `winget install Google.PlatformTools`
   (open a new terminal afterwards so `adb` is on PATH).
2. On the phone: enable **Developer options → USB debugging**, connect by USB, and
   accept the "Allow USB debugging?" prompt.

**Every session — one command** (one isolated database per participant):

```bat
venv\Scripts\python.exe run_uat_session.py --participant P01 --usb
```

`--usb` runs `adb reverse tcp:5000 tcp:5000` (finding `adb` on PATH or in a
winget install) and starts the server on a fresh, participant-specific throwaway
database — never the real one. The participant starts at the first-launch setup
screen and imports the generated statement printed in the console. On the phone
open **http://localhost:5000** and tap the browser's "Install app" / "Add to Home
screen" to install the PWA.

For a specification-only build (demo extensions hidden), add `--spec-only`.

> **Pre-seeded demo database.** For a ready-made, already-trained demo (no setup
> step; password **uat-demo-1234**), `tools/uat_seed.py` regenerates
> `uat_demo.db` — the fixed dataset (seed 7, 8 months, ZMW 12,000 salary) that
> the measurements in [`test_results.md`](test_results.md) were taken on. Run it
> with `EXPENSE_TRACKER_DB` and `CATEGORISER_PATH` pointing at throwaway paths.

---

## iPhone, or Android untethered — local HTTPS with mkcert

No `adb` on iOS, and Wi-Fi access needs HTTPS the phone trusts. `mkcert` issues a
locally-trusted certificate for this PC's LAN address.

1. `winget install FiloSottile.mkcert` then `mkcert -install`
2. Create the certificate **outside the project folder**. The `-key.pem` file is a
   private key; keeping it out of the repository means it can never be committed
   (the repo is public) or read by tools that index the workspace. The commands
   below also lock the folder to your Windows account:

```bat
mkdir "%USERPROFILE%\.certs\expense-tracker"
icacls "%USERPROFILE%\.certs\expense-tracker" /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:(OI)(CI)F"
cd /d "%USERPROFILE%\.certs\expense-tracker"
mkcert 192.168.x.x <this-pc-name>.local localhost
```

   (Use your PC's LAN IP.) As a second safeguard, `*.pem` is in `.gitignore`.

3. Back in the project folder, run bound to the network with that certificate:

```bat
set SSL_CERT=%USERPROFILE%\.certs\expense-tracker\192.168.x.x+2.pem
set SSL_KEY=%USERPROFILE%\.certs\expense-tracker\192.168.x.x+2-key.pem
venv\Scripts\python.exe run_uat_session.py --participant P01 --mobile
```

(`app.py` honours the same `SSL_CERT`/`SSL_KEY` variables with
`set FLASK_HOST=0.0.0.0` if you want to run without a UAT session.)

4. Copy mkcert's root CA (`mkcert -CAROOT` → `rootCA.pem`) to the phone and
   install it as a trusted certificate (Android: Settings → Security → Install a
   certificate → CA certificate; iOS: install the profile, then Settings →
   General → About → Certificate Trust Settings → enable full trust). Copy only
   `rootCA.pem`, never `rootCA-key.pem`: the CA key can mint certificates your
   devices will trust, so it must stay on the PC.
5. On the phone open `https://192.168.x.x:5000` and install the PWA.
6. **Check offline mode** in the app under **Settings → Offline mode**. It should
   read *Ready*. If it shows *Not available*, it explains why. On iPhone the usual
   cause is step 4's full trust not being enabled. Also open the installed
   home-screen app once while the server is running: iOS keeps the installed app
   separate from Safari, so offline mode has to be set up inside it.

> Binding to `0.0.0.0` exposes the server to your local network for the session;
> keep it to a trusted network. The loopback/USB path above does not expose it.

---

## Not supported, by design

Public tunnels (ngrok, cloudflared) would give instant HTTPS but route financial
data through a third party, which breaks NFR-05. They are deliberately not used.

# Deploy on Hetzner the easy way (Coolify) — beginner, mostly clicking

Coolify is a free "Render/Heroku you run on your own Hetzner server." You get a
web dashboard, deploy with clicks, and your app runs 24/7 with HTTPS and a
password. No terminal, no coding. ~€4/month.

Three short parts: **GitHub → Hetzner → Coolify.** ~25 minutes.

---

## Part 1 — Put the code on GitHub (browser, ~5 min)
Coolify needs to read your code from a Git website. The easiest, no-terminal way:

1. Make a free account at **https://github.com** (skip if you have one).
2. Click the **+** (top-right) → **New repository**.
   - Name: `chart-alerts`
   - Choose **Public**
   - Click **Create repository**.
3. On the new empty repo page, click the link **"uploading an existing file"**.
4. Open **Finder**, go to your `chart-alerts` folder, and **drag these into the
   browser** (you can select multiple):
   - `server.py`, `index.html`, `requirements.txt`, `Dockerfile`, and the
     `deploy` folder.
   - ❌ **Do NOT drag `.env`** (it holds your secret token — you'll type the
     token into Coolify instead). Also skip `levels.json` / `settings.json` if
     present.
5. Click **Commit changes**. Your code is now on GitHub. Copy the repo URL from
   the address bar (looks like `https://github.com/yourname/chart-alerts`).

---

## Part 2 — Create the Hetzner server with Coolify (browser, ~7 min)
1. Go to **https://console.hetzner.cloud** → sign up / log in → **New Project**
   → open it → **Add Server**.
2. Pick:
   - **Location:** nearest you.
   - **Image:** click the **Apps** tab → choose **Coolify**. (This auto-installs
     the dashboard — no terminal.)
   - **Type:** **CX22** (~€3.79/mo).
3. Scroll down, click **Create & Buy now**.
4. Wait **3–5 minutes** while Coolify installs. Note the server's **public IP**.
5. In your browser, open **`http://YOUR_SERVER_IP:8000`** (replace with the IP).
   Coolify's setup page appears → **create your admin account** (email +
   password you choose). You're now in the Coolify dashboard.

---

## Part 3 — Deploy the app in Coolify (browser, ~8 min)
1. In Coolify: **+ New** → **Project** → name it `trading` → open it →
   **+ New Resource**.
2. Choose **Public Repository**. Paste your GitHub repo URL from Part 1 →
   **Continue**. Coolify detects the **Dockerfile** automatically.
3. **Environment Variables** (left menu) → add these three (click "Add"):
   - `TELEGRAM_BOT_TOKEN` = your **new** bot token
   - `TELEGRAM_CHAT_ID` = `1770135190`
   - `APP_PASSWORD` = a password you pick (this protects your app)
4. **Storage / Persistent Storage** → **Add** a volume:
   - Mount path: **`/data`**  (this keeps your alerts when you redeploy)
5. **Network / Ports** → make sure the port is **8000** (the Dockerfile uses it).
6. **Domains** → Coolify shows an auto HTTPS address (a `sslip.io` link). You can
   use that as-is, or set your own domain later.
7. Click **Deploy** (top right). Watch the logs; after ~1–2 min it says running.
8. Open the **Domains** link on your **phone and PC** → you'll see the password
   page → enter your `APP_PASSWORD` → the chart loads. ✅

---

## Part 4 — Confirm alerts work
1. In the app → **🔔** → **Send test Telegram alert** → you should get a Telegram
   message instantly.
2. Add a level near the price with 🔔 on → you'll get pinged on a cross, even
   with your devices off. The server does it all now.

---

## Everyday use
- **Open it:** just visit your Coolify domain on any device, enter your password.
- **It runs forever:** Hetzner + Coolify keep it alive and restart it if needed.
- **Change the code later:** upload the changed file to GitHub (same drag-drop),
  then in Coolify click **Redeploy**. Your alerts are kept (they live on `/data`).
- **Silence everything:** in the app, 🔔 → **Mute all alerts**.

## If something's off
- **Can't open `:8000` in Part 2** → Coolify is still installing; wait a couple
  more minutes and refresh.
- **Test alert says "no creds"** → re-check the two Telegram env vars in Coolify,
  then **Redeploy**.
- **App forgets alerts after redeploy** → the `/data` volume (Part 3 step 4)
  wasn't added; add it and redeploy.
- **Forgot the app password** → change `APP_PASSWORD` in Coolify env vars →
  Redeploy.

That's it — a Render-style dashboard, on your own cheap Hetzner box.

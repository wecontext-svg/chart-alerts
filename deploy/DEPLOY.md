# Deploy chart-alerts to a server — complete beginner guide (Mac)

This walks you through putting the app on a small always-on computer in the cloud
(a "server") so your Telegram alerts work 24/7 even when your Mac is off. No prior
experience needed. Every command is explained. Total time: ~30–45 min.

**What we're building:** a €4/month Hetzner server runs the app forever. You reach
its screen privately from your devices using **Tailscale** (a free, simple private
network). Nothing is open to the public internet, so it's safe.

**You will switch between two places:**
- 🖥️ **Your Mac's Terminal** (commands you run locally)
- ☁️ **The server** (after you "SSH in", your Terminal is typing on the server)

I'll mark every command with 🖥️ or ☁️ so you always know where to run it.

---

## A few words so nothing is confusing
- **Terminal**: a Mac app where you type commands. Open it: press `Cmd+Space`,
  type `Terminal`, hit Enter. To paste, use `Cmd+V`. Commands run when you press
  Enter.
- **SSH**: the secure way to log into the server from your Mac.
- **`root`**: the server's admin user.
- **SERVER_IP**: a number like `91.99.12.34` you'll get from Hetzner. Wherever you
  see `SERVER_IP` below, replace it with your real number (keep the rest).
- When a command "succeeds" it usually prints something or just returns to a new
  prompt with no error. I tell you what to expect.

---

## STEP 0 — Regenerate your Telegram bot token (important, 2 min)
Your current bot token was typed in our chat, so anyone who saw it could use your
bot. Make a new one:

1. Open Telegram, search for **@BotFather**, open the chat.
2. Send: `/revoke`
3. It lists your bots — tap the one you use for alerts.
4. It replies with a **new token** that looks like `8734201374:AAE....`. Copy it
   and keep it somewhere for Step 6. The old token now stops working.

(Your **chat id** stays the same: `1770135190`.)

---

## STEP 1 — Make an SSH key on your Mac (3 min)
This is like a digital house key that lets your Mac into the server with no
password.

🖥️ In Terminal, check if you already have one:
```bash
ls ~/.ssh/id_ed25519.pub
```
- If it prints a path (no "No such file"), you already have a key → skip to Step 2.
- If it says **No such file or directory**, create one:

🖥️
```bash
ssh-keygen -t ed25519
```
It asks 3 questions — just press **Enter** three times (accept defaults, empty
passphrase is fine to start).

🖥️ Now copy your **public** key to the clipboard:
```bash
pbcopy < ~/.ssh/id_ed25519.pub
```
Nothing prints — that's normal. Your key is now copied, ready to paste in Step 2.

---

## STEP 2 — Create the server on Hetzner (8 min)
1. Go to https://www.hetzner.com/cloud → **Sign up**, confirm your email, add a
   payment method.
2. Open the **Cloud Console** → **+ New Project** → name it `trading` → open it.
3. Click **Add Server**. Choose:
   - **Location:** the one nearest you.
   - **Image:** **Ubuntu 24.04**.
   - **Type:** **CX22** (the ~€3.79/mo one). More than enough.
4. Scroll to **SSH keys** → **Add SSH key** → **paste** (`Cmd+V`) the key you
   copied in Step 1 → give it a name like `mac` → Add.
5. Leave everything else default. Click **Create & Buy now**.
6. After ~30 seconds the server appears with a **public IP** (e.g. `91.99.12.34`).
   **This is your SERVER_IP** — copy it.

---

## STEP 3 — Log into the server (2 min)
🖥️ In Terminal (replace `SERVER_IP` with your number):
```bash
ssh root@SERVER_IP
```
- First time it asks: *"Are you sure you want to continue connecting?"* → type
  `yes` and Enter.
- Your prompt changes to something like `root@ubuntu:~#`. **You are now on the
  server.** From here, commands marked ☁️ run here.

☁️ Update the server (copy-paste the whole line):
```bash
apt-get update -y && apt-get upgrade -y
```
This takes a minute and prints a lot of text. When it finishes you get the prompt
back. If it asks any question, press Enter to accept the default.

> Keep this Terminal window open. You'll also open a **second** Terminal window
> next for copying files (`Cmd+N` makes a new window).

---

## STEP 4 — Copy the app to the server (3 min)
🖥️ Open a **new** Terminal window (`Cmd+N`) — this one is your Mac, not the server.
Go to the project folder and upload the app (replace `SERVER_IP`):
```bash
cd /Users/yuliiaolshanska/.gemini/antigravity/scratch/options-flow-alerts
scp -r chart-alerts root@SERVER_IP:/opt/chart-alerts
```
You'll see a list of files with progress bars. When it returns to the prompt, the
app is on the server. (It also copied your old local `.env` — we overwrite it with
the new token next, so that's fine.)

---

## STEP 5 — Put your new secrets on the server (2 min)
Go back to the **server** Terminal window (the one showing `root@...#`).

☁️ Create the secrets file with your **new** token from Step 0. Paste this whole
block, but **replace `PUT_YOUR_NEW_TOKEN_HERE`** with the new token:
```bash
cat > /opt/chart-alerts/.env <<'EOF'
TELEGRAM_BOT_TOKEN=PUT_YOUR_NEW_TOKEN_HERE
TELEGRAM_CHAT_ID=1770135190
EOF
chmod 600 /opt/chart-alerts/.env
```
☁️ Check it looks right:
```bash
cat /opt/chart-alerts/.env
```
You should see your two lines. Make sure the token is the **new** one.

---

## STEP 6 — Install & start the app (one command, 3 min)
☁️ Run the setup script:
```bash
bash /opt/chart-alerts/deploy/setup.sh
```
It installs everything, creates a safe user, starts the app, and turns on the
firewall. At the end it prints **"DONE. App is live on 127.0.0.1:8000"**.

☁️ Confirm it's running:
```bash
systemctl status chart-alerts
```
You should see green **`active (running)`**. Press `q` to exit that view.

🎉 The alert daemon is now running 24/7. But it's private to the server — next we
give *your devices* a secure door in.

---

## STEP 7 — Set up Tailscale (private access, 5 min)
Tailscale is a free app that makes a private network between the server and your
phone/laptop. Only your devices can reach the app.

☁️ **On the server**, install and start it:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```
It prints a **link** (`https://login.tailscale.com/...`). Copy that link, open it
in your Mac's browser, and **sign in** (Google/GitHub/email — pick one, remember
it). This authorizes the server.

**One-time toggle (needed for the next command):** in your browser, go to
https://login.tailscale.com/admin/dns → find **HTTPS Certificates** → click
**Enable**. (This lets Tailscale give your app a private `https://` address.)

☁️ Expose the app on your private network over HTTPS:
```bash
tailscale serve --bg 8000
tailscale serve status
```
If `serve` complains about HTTPS, you skipped the toggle above — enable it, then
run the two commands again.
The status line prints a URL like **`https://ubuntu.tailXXXX.ts.net/`** — that's
your private app address. Copy it.

**On your phone and Mac:** install the **Tailscale** app
(App Store / https://tailscale.com/download) and **sign in with the same account**
you just used. That's it — your devices and the server are now on one private net.

---

## STEP 8 — Open the app and test (3 min)
1. On your Mac or phone (with Tailscale running/signed in), open the
   `https://....ts.net/` URL from Step 7. The chart loads.
2. Tap **🔔** → **Send test Telegram alert** → you should get a Telegram message
   within a second. ✅ If you do, everything works end to end.
3. Make a real alert: pick the **─** tool, click a price near the current one,
   open 🔔, tick its alert. You'll get pinged when price crosses — even with your
   Mac closed.

You're done. The server runs forever; you manage alerts from any of your devices.

---

## Everyday use & upkeep

**See what the daemon is doing live** (☁️ on the server):
```bash
journalctl -u chart-alerts -f
```
(Press `Ctrl+C` to stop watching.)

**Restart the app** (☁️):
```bash
systemctl restart chart-alerts
```

**Update the app after you change code on your Mac** — 🖥️ run on your Mac:
```bash
cd /Users/yuliiaolshanska/.gemini/antigravity/scratch/options-flow-alerts
scp -r chart-alerts/* root@SERVER_IP:/opt/chart-alerts/
ssh root@SERVER_IP systemctl restart chart-alerts
```

**Silence everything:** in the app, 🔔 → **Mute all alerts** (no need to touch the
server).

**Reboot safety:** if the server restarts, the app auto-starts and your alerts
(`levels.json`) and mute setting (`settings.json`) are preserved.

---

## If something goes wrong (troubleshooting)
- **`ssh: connect ... Connection refused / timed out`** → wrong IP, or the server
  is still booting (wait 1 min). Double-check SERVER_IP in the Hetzner console.
- **`Permission denied (publickey)`** → the SSH key wasn't added at server
  creation. Easiest fix: in Hetzner console, delete the server and recreate it,
  this time pasting the key in the SSH keys section (Step 2.4).
- **Test alert says "no creds"** → the `.env` is missing or has the wrong token.
  Re-do Step 5, then ☁️ `systemctl restart chart-alerts`.
- **Test alert says "Telegram error: chat not found"** → wrong chat id, or you
  never messaged the bot. Open Telegram, send your bot any message, try again.
- **App won't load in browser** → make sure Tailscale is **on and signed in** on
  the device you're using, and you're opening the `...ts.net` URL exactly.
- **Check the app status** ☁️: `systemctl status chart-alerts` and
  `journalctl -u chart-alerts -n 50 --no-pager`.

---

## (Optional, advanced) Public web address instead of Tailscale
Only if you want to open the app from a browser **without** the Tailscale app —
e.g. on someone else's computer. This needs a domain name and is more work. See
`Caddyfile` in this folder and ask for the Caddy steps. For personal use,
**Tailscale (above) is simpler and safer — stick with it.**

---

## Security recap (why this is safe)
- New Telegram token (old one revoked).
- The app listens only on the server's localhost — **not reachable from the
  public internet**.
- Access is only through Tailscale's encrypted private network, tied to your
  login.
- Runs as a non-admin user, auto-restarts, firewall on, secrets file locked down.

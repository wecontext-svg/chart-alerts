"""Hyperliquid live-chart + level-alert server.

One process does three things:
  1. serves the chart UI (index.html)
  2. proxies Hyperliquid candle history (avoids browser CORS)
  3. runs a background daemon that polls live prices and fires Telegram
     alerts when price crosses your drawn levels.

The daemon is independent of the browser, so once this is deployed to an
always-on host it alerts 24/7 whether or not the chart tab is open.

Run:  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python3 server.py
Open: http://127.0.0.1:8000
"""
import asyncio, json, os, ssl, time
from pathlib import Path
from aiohttp import web, ClientSession, TCPConnector, ClientTimeout


def _ssl_context():
    """Python builds without a CA bundle can't verify TLS; prefer certifi."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return False  # last resort: disable verification

BASE = Path(__file__).parent
# STATE_DIR keeps alerts/settings on a persistent volume so they survive
# container redeploys (e.g. on Coolify). Defaults to the app folder locally.
STATE_DIR = Path(os.environ.get("STATE_DIR", BASE))
STATE_DIR.mkdir(parents=True, exist_ok=True)
LEVELS_FILE = STATE_DIR / "levels.json"
HL_INFO = "https://api.hyperliquid.xyz/info"


def _load_dotenv():
    """Read KEY=VALUE lines from chart-alerts/.env (gitignored) so secrets
    don't have to live in the shell command or anywhere committed."""
    f = BASE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_SECONDS = int(os.environ.get("ALERT_POLL_SECONDS", "5"))
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")  # 0.0.0.0 = reachable from your phone on the same Wi-Fi
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # if set, the UI requires this password

levels_lock = asyncio.Lock()


def _auth_token():
    import hashlib
    return hashlib.sha256(("hlchart:" + APP_PASSWORD).encode()).hexdigest()[:32]


LOGIN_HTML = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Sign in</title><style>body{background:#0a0c10;color:#e6edf6;font-family:ui-monospace,monospace;
display:flex;height:100vh;align-items:center;justify-content:center;margin:0}form{background:#11151c;
border:1px solid #1b2230;border-radius:12px;padding:24px;width:260px}h3{margin:0 0 14px}input{width:100%;
box-sizing:border-box;background:#0a0c10;border:1px solid #1b2230;color:#e6edf6;border-radius:7px;padding:10px;
font:inherit}button{width:100%;margin-top:10px;background:#1f6feb;border:0;color:#fff;border-radius:7px;
padding:10px;cursor:pointer;font:inherit}.e{color:#ef5350;font-size:12px;margin-top:8px;min-height:14px}</style>
<form method=post action=/login><h3>⚡ HL Chart</h3>
<input name=password type=password placeholder=password autofocus>
<button>Sign in</button><div class=e>__ERR__</div></form>"""


@web.middleware
async def auth_mw(request, handler):
    if not APP_PASSWORD or request.path == "/login":
        return await handler(request)
    if request.cookies.get("auth") == _auth_token():
        return await handler(request)
    raise web.HTTPFound("/login")


async def login_get(request):
    return web.Response(text=LOGIN_HTML.replace("__ERR__", ""), content_type="text/html")


async def login_post(request):
    data = await request.post()
    if data.get("password") == APP_PASSWORD:
        r = web.HTTPFound("/")
        r.set_cookie("auth", _auth_token(), max_age=2592000, httponly=True, samesite="Lax")
        return r
    return web.Response(text=LOGIN_HTML.replace("__ERR__", "Wrong password"),
                        content_type="text/html")


def load_levels():
    if LEVELS_FILE.exists():
        try:
            return json.loads(LEVELS_FILE.read_text())
        except Exception:
            return []
    return []


def save_levels(levels):
    LEVELS_FILE.write_text(json.dumps(levels, indent=2))


LEVELS = load_levels()

SETTINGS_FILE = STATE_DIR / "settings.json"


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return {}
    return {}


SETTINGS = load_settings()


def save_settings():
    SETTINGS_FILE.write_text(json.dumps(SETTINGS))


def muted():
    return bool(SETTINGS.get("muted"))


# INDICATORS are the chart overlays (VWAP/EMA/etc). Stored server-side so the
# same setup shows on every device, instead of per-browser localStorage.
INDICATORS_FILE = STATE_DIR / "indicators.json"


def load_indicators():
    if INDICATORS_FILE.exists():
        try:
            data = json.loads(INDICATORS_FILE.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return None


def save_indicators(inds):
    INDICATORS_FILE.write_text(json.dumps(inds, indent=2))


# OUTBOX is a durable queue of alert messages waiting for Telegram confirmation.
# Messages persist to disk, so a network blip, Telegram outage, or container
# restart can never silently drop an alert — it's retried every poll until
# Telegram replies ok:true.
OUTBOX_FILE = STATE_DIR / "outbox.json"


def load_outbox():
    if OUTBOX_FILE.exists():
        try:
            data = json.loads(OUTBOX_FILE.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


PENDING = load_outbox()


def save_outbox():
    OUTBOX_FILE.write_text(json.dumps(PENDING))


# ---------- HTTP handlers ----------
async def index(request):
    return web.FileResponse(BASE / "index.html")


async def api_candles(request):
    coin = request.query.get("coin", "xyz:MU")
    interval = request.query.get("interval", "4h")
    days = float(request.query.get("days", "90"))
    now = int(time.time() * 1000)
    body = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": now - int(days * 86400 * 1000), "endTime": now}}
    async with request.app["session"].post(HL_INFO, json=body) as r:
        data = await r.json()
    out = []
    if data:
        for c in data:
            out.append({"time": c["t"] // 1000, "open": float(c["o"]),
                        "high": float(c["h"]), "low": float(c["l"]),
                        "close": float(c["c"]), "volume": float(c["v"])})
    return web.json_response(out)


async def api_markets(request):
    """List tradable xyz: markets sorted by 24h notional volume (busiest first)."""
    try:
        async with request.app["session"].post(
                HL_INFO, json={"type": "metaAndAssetCtxs", "dex": "xyz"}) as r:
            m = await r.json()
        uni, ctx = m[0]["universe"], m[1]
        out = []
        for i, a in enumerate(uni):
            if a.get("isDelisted"):
                continue
            try:
                vol = float(ctx[i].get("dayNtlVlm", 0))
            except Exception:
                vol = 0.0
            out.append({"coin": a["name"], "sym": a["name"].split(":", 1)[1], "vol": vol})
        out.sort(key=lambda x: -x["vol"])
        return web.json_response(out)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def api_get_levels(request):
    async with levels_lock:
        return web.json_response(LEVELS)


async def api_create_level(request):
    b = await request.json()
    kind = b.get("kind", "level")          # level (horizontal) | line (diagonal)
    p1, p2 = b.get("p1"), b.get("p2")      # each {"t": unix_seconds, "price": float}
    price = b.get("price")
    if price is None and p1:
        price = p1.get("price")
    async with levels_lock:
        lvl = {
            "id": str(int(time.time() * 1000)),
            "coin": b["coin"],
            "kind": kind,
            "price": float(price) if price is not None else 0.0,  # level/line nominal; n/a for confluence
            "p1": p1, "p2": p2,
            "type": b.get("type", "trend"),  # trend | ray | extended (display only)
            "direction": b.get("direction", "cross"),  # cross | up | down
            "repeat": b.get("repeat", "always"),  # always (every cross) | once (fire then disarm)
            "confirm": b.get("confirm", "close" if kind == "confluence" else "intrabar"),
            "timeframe": b.get("timeframe", "4h"),
            "conditions": b.get("conditions"),  # confluence: list of {type, op, value, ...}
            "note": b.get("note", ""),
            "alert_enabled": bool(b.get("alert_enabled", False)),
            "color": b.get("color", "#2962FF"),
            "ratios": b.get("ratios"),        # fib: list of retracement ratios
            "fib_prices": b.get("fib_prices"),  # fib: parallel list of prices
            "sides": {},                       # per-target arming state
            "last_met": None,                  # confluence arming state
        }
        LEVELS.append(lvl)
        save_levels(LEVELS)
    return web.json_response(lvl)


async def api_update_level(request):
    lid = request.match_info["id"]
    b = await request.json()
    async with levels_lock:
        for l in LEVELS:
            if l["id"] == lid:
                for k in ("price", "direction", "repeat", "confirm", "timeframe",
                          "conditions", "note", "alert_enabled", "color"):
                    if k in b:
                        l[k] = b[k]
                l["sides"] = {}     # re-arm after any edit
                l["last_met"] = None
                save_levels(LEVELS)
                return web.json_response(l)
    return web.json_response({"error": "not found"}, status=404)


async def api_delete_level(request):
    lid = request.match_info["id"]
    async with levels_lock:
        before = len(LEVELS)
        LEVELS[:] = [l for l in LEVELS if l["id"] != lid]
        save_levels(LEVELS)
    return web.json_response({"deleted": before - len(LEVELS)})


async def api_mute(request):
    """GET -> current mute state; POST {muted:bool} -> set it. Muting silences
    all Telegram sends without deleting alerts; arming state keeps updating so
    you don't get a backlog when you unmute."""
    if request.method == "POST":
        b = await request.json()
        SETTINGS["muted"] = bool(b.get("muted"))
        save_settings()
    return web.json_response({"muted": muted()})


async def api_indicators(request):
    """GET -> saved chart indicators (list, or null if none saved yet);
    PUT [..] -> replace the whole list. Shared across all devices."""
    if request.method == "PUT":
        b = await request.json()
        if not isinstance(b, list):
            return web.json_response({"error": "expected a list"}, status=400)
        save_indicators(b)
        return web.json_response(b)
    return web.json_response(load_indicators())


async def api_test(request):
    """Send a one-off Telegram message so you can confirm delivery + creds."""
    if not (TG_TOKEN and TG_CHAT):
        return web.json_response({"ok": False, "detail":
            "Server has no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Restart it with those env vars."})
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    text = ("✅ <b>Test alert</b> — HL Chart\n"
            "Telegram is wired up correctly. Real level/trendline/fib alerts arrive here.")
    try:
        async with request.app["session"].post(
                url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}) as r:
            data = await r.json()
        ok = bool(data.get("ok"))
        return web.json_response({"ok": ok, "detail": "Sent — check Telegram."
                                  if ok else f"Telegram error: {data.get('description')}"})
    except Exception as e:
        return web.json_response({"ok": False, "detail": str(e)})


# ---------- alert daemon ----------
def _f(x):
    try:
        return float(x)
    except Exception:
        return None


async def fetch_ctx(session, coins):
    """Return {coin: {px, funding, oi}} for the given fully-qualified coins."""
    out = {}
    dexes = sorted(set(c.split(":")[0] for c in coins if ":" in c))
    for dex in dexes:
        try:
            async with session.post(HL_INFO, json={"type": "metaAndAssetCtxs", "dex": dex}) as r:
                m = await r.json()
            names = [a["name"] for a in m[0]["universe"]]
            for i, name in enumerate(names):
                if name in coins:
                    c = m[1][i]
                    out[name] = {"px": _f(c.get("markPx")),
                                 "funding": _f(c.get("funding")) or 0.0,
                                 "oi": _f(c.get("openInterest")) or 0.0}
        except Exception as e:
            print("fetch_ctx error", dex, e)
    return out


# candle cache so the daemon doesn't re-fetch every poll (closed candles change per bar)
_candles_cache = {}
_CANDLE_DAYS = {"5m": 8, "1h": 20, "4h": 60, "1d": 220}


async def get_candles(session, coin, interval):
    key = (coin, interval)
    now = time.time()
    ent = _candles_cache.get(key)
    if ent and now - ent[0] < 30:
        return ent[1]
    days = _CANDLE_DAYS.get(interval, 60)
    end = int(now * 1000)
    body = {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                              "startTime": end - days * 86400 * 1000, "endTime": end}}
    candles = []
    try:
        async with session.post(HL_INFO, json=body) as r:
            data = await r.json()
        candles = [{"t": c["t"], "o": float(c["o"]), "h": float(c["h"]),
                    "l": float(c["l"]), "c": float(c["c"]), "v": float(c["v"])}
                   for c in (data or [])]
    except Exception as e:
        print("get_candles error", coin, interval, e)
    _candles_cache[key] = (now, candles)
    return candles


# --- indicator math (mirrors the front-end) ---
def _ema(vals, p):
    if len(vals) < p:
        return None
    k = 2 / (p + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(vals, p=14):
    if len(vals) < p + 1:
        return None
    g = lo = 0.0
    for i in range(1, p + 1):
        ch = vals[i] - vals[i - 1]
        g += max(ch, 0); lo += max(-ch, 0)
    g /= p; lo /= p
    for i in range(p + 1, len(vals)):
        ch = vals[i] - vals[i - 1]
        g = (g * (p - 1) + max(ch, 0)) / p
        lo = (lo * (p - 1) + max(-ch, 0)) / p
    return 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)


def _vwap(candles, anchor):
    import datetime as dt
    def key(t):
        d = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
        if anchor == "session":
            return (d.year, d.month, d.day)
        if anchor == "month":
            return (d.year, d.month)
        iso = d.isocalendar()
        return (iso[0], iso[1])
    pv = v = 0.0
    ck = None
    val = None
    for c in candles:
        k = key(c["t"])
        if k != ck:
            ck = k; pv = v = 0.0
        tp = (c["h"] + c["l"] + c["c"]) / 3
        pv += tp * c["v"]; v += c["v"]
        val = pv / v if v > 0 else c["c"]
    return val


def _vwap_anchored(candles, anchor_s):
    """VWAP accumulated from the bar at/after anchor_s (unix seconds).
    Mirrors the chart's ⚓ anchored VWAP. candle["t"] is in ms."""
    a_ms = anchor_s * 1000
    pv = v = 0.0
    val = None
    for c in candles:
        if c["t"] < a_ms:
            continue
        tp = (c["h"] + c["l"] + c["c"]) / 3
        pv += tp * c["v"]; v += c["v"]
        val = pv / v if v > 0 else c["c"]
    return val


def _pattern(candles, name):
    if len(candles) < 3:
        return False
    c, p, p2 = candles[-1], candles[-2], candles[-3]
    body = lambda x: abs(x["c"] - x["o"])
    rng = lambda x: max(x["h"] - x["l"], 1e-9)
    uw = lambda x: x["h"] - max(x["o"], x["c"])
    lw = lambda x: min(x["o"], x["c"]) - x["l"]
    bull = lambda x: x["c"] >= x["o"]
    if name == "bull_engulf":
        return bull(c) and not bull(p) and c["c"] >= p["o"] and c["o"] <= p["c"] and body(c) > body(p)
    if name == "bear_engulf":
        return (not bull(c)) and bull(p) and c["c"] <= p["o"] and c["o"] >= p["c"] and body(c) > body(p)
    if name == "hammer":
        return body(c) > 0 and lw(c) >= 2 * body(c) and uw(c) <= 0.5 * body(c) and bull(c)
    if name == "shooting_star":
        return body(c) > 0 and uw(c) >= 2 * body(c) and lw(c) <= 0.5 * body(c)
    if name == "doji":
        return body(c) <= 0.1 * rng(c)
    if name == "three_white_soldiers":
        return all(bull(x) for x in (c, p, p2)) and c["c"] > p["c"] > p2["c"]
    if name == "three_black_crows":
        return all(not bull(x) for x in (c, p, p2)) and c["c"] < p["c"] < p2["c"]
    return False


def ctx_from(l, cx, candle_map):
    """Build evaluation context for a confluence alert from prefetched data."""
    tf = l.get("timeframe", "4h")
    confirm = l.get("confirm", "close")
    cdata = candle_map.get((l["coin"], tf), [])
    cc = cdata[:-1] if (confirm == "close" and len(cdata) >= 2) else cdata
    mark = cx.get("px")
    if confirm == "close" and cc:
        price = cc[-1]["c"]
    elif mark is not None:
        price = mark
    else:
        price = cc[-1]["c"] if cc else None
    return {"price": price, "closes": [c["c"] for c in cc], "candles": cc,
            "funding": cx.get("funding") or 0.0, "oi": cx.get("oi") or 0.0}


def eval_condition(cond, ctx):
    t = cond.get("type")
    op = cond.get("op", ">")
    price = ctx["price"]
    cmp = lambda a, b: (a is not None) and (a > b if op == ">" else a < b)
    if price is None:
        return False
    if t == "price":
        return cmp(price, float(cond["value"]))
    if t == "funding":
        return cmp(ctx["funding"], float(cond["value"]))
    if t == "oi":
        return cmp(ctx["oi"], float(cond["value"]))
    if t == "vwap":
        return cmp(price, _vwap(ctx["candles"], cond.get("anchor", "week")))
    if t == "avwap":
        at = cond.get("anchorT")
        if at in (None, ""):
            return False
        return cmp(price, _vwap_anchored(ctx["candles"], int(float(at))))
    if t == "ema":
        return cmp(price, _ema(ctx["closes"], int(cond.get("period", 45))))
    if t == "rsi":
        return cmp(_rsi(ctx["closes"], int(cond.get("period", 14))), float(cond["value"]))
    if t == "pattern":
        return _pattern(ctx["candles"], cond.get("name", ""))
    return False


def cond_text(c):
    t = c.get("type"); op = c.get("op", ">")
    if t == "price":
        return f"price {op} {c.get('value')}"
    if t == "funding":
        return f"funding {op} {c.get('value')}"
    if t == "oi":
        return f"OI {op} {c.get('value')}"
    if t == "vwap":
        return f"price {op} VWAP({c.get('anchor', 'week')})"
    if t == "avwap":
        return f"price {op} anchored VWAP"
    if t == "ema":
        return f"price {op} EMA{c.get('period', 45)}"
    if t == "rsi":
        return f"RSI {op} {c.get('value')}"
    if t == "pattern":
        return f"candle = {c.get('name')}"
    return str(t)


def format_confluence_alert(l, ctx):
    desc = "\n".join("✔ " + cond_text(c) for c in (l.get("conditions") or []))
    extra = f"price {ctx['price']:.6g}"
    if any(c.get("type") == "funding" for c in (l.get("conditions") or [])):
        extra += f" · funding {ctx['funding'] * 100:.4f}%/hr"
    return (f"🔔🔗 <b>{l['coin']}</b> confluence met ({l.get('timeframe', '4h')}"
            f"{', close' if l.get('confirm') == 'close' else ''})\n{desc}\n{extra}"
            + (f"\n📝 {l['note']}" if l.get("note") else ""))


async def send_telegram(session, text):
    """Return True only when Telegram confirms delivery (ok:true). Any network
    error, timeout, or non-ok response returns False so the caller re-queues it."""
    if not (TG_TOKEN and TG_CHAT):
        print("[telegram disabled] " + text)
        return True  # no creds -> undeliverable; don't grow the queue forever
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        async with session.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
                                timeout=ClientTimeout(total=15)) as r:
            data = await r.json()
        if not data.get("ok"):
            print("telegram not ok:", data.get("description"))
        return bool(data.get("ok"))
    except Exception as e:
        print("telegram error", e)
        return False


def format_alert(l, px, prev, side, target=None, label=None):
    arrow = "▲" if side == "above" else "▼"
    kind = l.get("kind", "level")
    if kind == "line":
        what = f"{l.get('type', 'trend')}line @ <b>{target:.6g}</b>"
    elif kind == "fib":
        what = f"fib <b>{label}</b> @ <b>{target:.6g}</b>"
    else:
        what = f"<b>{l['price']:g}</b>"
    return (f"🔔 <b>{l['coin']}</b> crossed {arrow} {what}\n"
            f"price: {px:g}  ({prev} → {side})"
            + (f"\n📝 {l['note']}" if l.get("note") else ""))


def effective_level_price(l, now_s):
    """Horizontal -> fixed price. Line -> price of the line at time `now_s`
    (linear interpolation/extrapolation through its two anchor points)."""
    if l.get("kind") == "line":
        p1, p2 = l.get("p1"), l.get("p2")
        if not p1 or not p2 or p2["t"] == p1["t"]:
            return None
        m = (p2["price"] - p1["price"]) / (p2["t"] - p1["t"])
        return p1["price"] + m * (now_s - p1["t"])
    return l["price"]


def level_targets(l, now_s):
    """One drawing can have several alertable price targets.
    horizontal -> 1, trendline -> 1 (interpolated), fib -> N."""
    kind = l.get("kind", "level")
    if kind == "line":
        tp = effective_level_price(l, now_s)
        return [] if tp is None else [(l.get("type", "trend"), tp)]
    if kind == "fib":
        rs, ps = l.get("ratios") or [], l.get("fib_prices") or []
        return [(f"{r:g}", float(p)) for r, p in zip(rs, ps)]
    return [("level", l["price"])]


def evaluate_cross(prev_side, price, level):
    """Pure crossing decision. Returns (new_side, should_fire)."""
    side = "above" if price >= level["price"] else "below"
    fire = False
    if prev_side is not None and side != prev_side:
        d = level.get("direction", "cross")
        fire = (d == "cross"
                or (d == "up" and side == "above")
                or (d == "down" and side == "below"))
    return side, fire


async def alert_loop(app):
    session = app["session"]
    print(f"alert daemon running (poll {POLL_SECONDS}s, "
          f"telegram {'ON' if TG_TOKEN and TG_CHAT else 'OFF'})")
    while True:
        try:
            async with levels_lock:
                active = [l for l in LEVELS if l.get("alert_enabled")]
            if active:
                coins = sorted(set(l["coin"] for l in active))
                ctxs = await fetch_ctx(session, coins)
                # prefetch candles for anything needing them (confluence / candle-close)
                tf_needs = set()
                for l in active:
                    if l.get("kind") == "confluence" or l.get("confirm") == "close":
                        tf_needs.add((l["coin"], l.get("timeframe", "4h")))
                candle_map = {}
                for cn, tf in tf_needs:
                    candle_map[(cn, tf)] = await get_candles(session, cn, tf)

                outbox, changed = [], False
                for l in active:
                    cx = ctxs.get(l["coin"])
                    if not cx:
                        continue
                    # ---- confluence (N conditions, AND) ----
                    if l.get("kind") == "confluence":
                        conds = l.get("conditions") or []
                        ctx = ctx_from(l, cx, candle_map)
                        met = bool(conds) and all(eval_condition(c, ctx) for c in conds)
                        prev = l.get("last_met")
                        if prev is None:
                            l["last_met"] = met
                            changed = True
                            continue
                        if met != prev:
                            l["last_met"] = met
                            changed = True
                        if met and not prev and not muted():  # edge: all conditions just became true
                            outbox.append(format_confluence_alert(l, ctx))
                            if l.get("repeat", "always") == "once":
                                l["alert_enabled"] = False
                        continue
                    # ---- level / line / fib (optionally candle-close confirmed) ----
                    if l.get("confirm") == "close":
                        cd = candle_map.get((l["coin"], l.get("timeframe", "4h")), [])
                        px = cd[-2]["c"] if len(cd) >= 2 else cx.get("px")
                    else:
                        px = cx.get("px")
                    if px is None:
                        continue
                    sides = l.setdefault("sides", {})
                    for label, target in level_targets(l, time.time()):
                        prev = sides.get(label)
                        side, fire = evaluate_cross(
                            prev, px, {"price": target, "direction": l.get("direction", "cross")})
                        if side != prev:
                            sides[label] = side
                            changed = True
                        if fire and not muted():
                            outbox.append(format_alert(l, px, prev, side, target, label))
                            if l.get("repeat", "always") == "once":
                                l["alert_enabled"] = False
                                changed = True
                                break
                if changed:
                    async with levels_lock:
                        save_levels(LEVELS)
                if outbox:
                    PENDING.extend(outbox)
                    save_outbox()
            # Always flush the durable queue (also retries anything left from a
            # previous failed send / restart). Keep whatever Telegram didn't confirm.
            if PENDING:
                still = []
                for msg in PENDING:
                    if not await send_telegram(session, msg):
                        still.append(msg)
                if len(still) != len(PENDING):
                    PENDING[:] = still
                    save_outbox()
        except Exception as e:
            print("alert_loop error", e)
        await asyncio.sleep(POLL_SECONDS)


# ---------- app wiring ----------
async def on_startup(app):
    app["session"] = ClientSession(connector=TCPConnector(ssl=_ssl_context()))
    app["alert_task"] = asyncio.create_task(alert_loop(app))


async def on_cleanup(app):
    app["alert_task"].cancel()
    await app["session"].close()


def make_app():
    app = web.Application(middlewares=[auth_mw])
    app.add_routes([
        web.get("/", index),
        web.get("/login", login_get),
        web.post("/login", login_post),
        web.get("/api/candles", api_candles),
        web.get("/api/markets", api_markets),
        web.get("/api/levels", api_get_levels),
        web.post("/api/levels", api_create_level),
        web.patch("/api/levels/{id}", api_update_level),
        web.delete("/api/levels/{id}", api_delete_level),
        web.post("/api/test", api_test),
        web.get("/api/mute", api_mute),
        web.post("/api/mute", api_mute),
        web.get("/api/indicators", api_indicators),
        web.put("/api/indicators", api_indicators),
    ])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "<your-LAN-IP>"
    finally:
        s.close()


if __name__ == "__main__":
    print(f"chart-alerts:  http://127.0.0.1:{PORT}   (this Mac)")
    if HOST == "0.0.0.0":
        print(f"               http://{_lan_ip()}:{PORT}   (phone on same Wi-Fi)")
    web.run_app(make_app(), host=HOST, port=PORT)

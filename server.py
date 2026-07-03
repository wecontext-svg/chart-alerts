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
import asyncio, json, math, os, re, ssl, time
import datetime as dt
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

# Symbols auto-tracked for daily option-level history (no button press needed).
# Override with OPT_WATCH="NVDA,AMD,...". Only optionable US names record; the
# rest just error out harmlessly. Pressing 📌 also enrolls any extra symbol.
_DEFAULT_WATCH = ("NVDA AMD AAPL AMZN GOOGL MSFT META TSLA PLTR HOOD ORCL MU COIN "
                  "MSTR NFLX AVGO MRVL INTC QCOM ARM TSM DELL IBM LLY GME RIVN RKLB "
                  "BABA NBIS NOW COST DKNG HIMS WDC SNDK")
OPT_WATCH = [s if ":" in s else f"xyz:{s}" for s in
             os.environ.get("OPT_WATCH", _DEFAULT_WATCH).replace(",", " ").split()]

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


# OPTHIST records one option-level snapshot per coin per day so you can chart how
# max pain / walls / gamma flip drift over time. Builds forward from first use
# (historical OI isn't available for free). { coin: { "YYYY-MM-DD": {...} } }
OPTHIST_FILE = STATE_DIR / "option_history.json"


def load_opthist():
    if OPTHIST_FILE.exists():
        try:
            d = json.loads(OPTHIST_FILE.read_text())
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


OPTHIST = load_opthist()


def save_opthist():
    OPTHIST_FILE.write_text(json.dumps(OPTHIST))


def record_opt_snapshot(coin, res):
    if not coin or not res or res.get("error"):
        return
    h = OPTHIST.setdefault(coin, {})
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    h[day] = {k: res.get(k) for k in
              ("max_pain", "call_wall", "put_wall", "gamma_flip", "spot")}
    # keep the most recent ~180 days per coin
    if len(h) > 180:
        for d in sorted(h)[:-180]:
            h.pop(d, None)
    save_opthist()


# JOURNAL logs every fired alert with its entry price, then fills in where
# price actually went +1h/+4h/+1d later — so you can SEE which setups work
# on your coins instead of guessing. Feeds the 📒 view in the UI.
JOURNAL_FILE = STATE_DIR / "journal.json"
J_HORIZONS = {"1h": 3600, "4h": 14400, "1d": 86400}


def load_journal():
    if JOURNAL_FILE.exists():
        try:
            d = json.loads(JOURNAL_FILE.read_text())
            if isinstance(d, list):
                return d
        except Exception:
            pass
    return []


JOURNAL = load_journal()


def save_journal():
    JOURNAL_FILE.write_text(json.dumps(JOURNAL))


def journal_add(coin, label, price, why=None):
    """why = list of '✔/✘ condition' strings — market state at fire time.
    dd/up = running max drawdown / max rise %, updated for 24h after entry."""
    if price is None:
        return
    JOURNAL.append({"id": str(int(time.time() * 1000)), "ts": int(time.time()),
                    "coin": coin, "label": (label or "alert").strip()[:80],
                    "price": float(price), "out": {}, "why": why or [],
                    "dd": 0.0, "up": 0.0})
    if len(JOURNAL) > 600:
        del JOURNAL[:len(JOURNAL) - 600]
    save_journal()


async def snapshot_why(session, coin, cx):
    """Evaluate the clean-core checklist right now — stored in the journal so
    you can later see WHAT the market looked like when a level was hit."""
    try:
        cm = {(coin, "4h"): await get_candles(session, coin, "4h"),
              (coin, "1d"): await get_candles(session, coin, "1d")}
        fake = {"coin": coin, "timeframe": "4h", "confirm": "close"}
        checks = [
            ({"type": "trend", "dir": "up", "emaLen": 50, "lookback": 50,
              "thresh": 70, "tf": "1d"}, "1d trend up"),
            ({"type": "ema", "op": ">", "period": 200}, "above EMA200"),
            ({"type": "vwap", "op": ">", "anchor": "week"}, "above wkVWAP"),
            ({"type": "volspike", "mult": 1.5, "period": 20, "green": "1"}, "vol≥1.5× green"),
            ({"type": "rsiband", "lo": 40, "hi": 65, "period": 14}, "RSI 40–65"),
            ({"type": "funding", "op": "<", "value": 0.0001}, "funding cool"),
        ]
        base = ctx_from(fake, cx, cm)
        out = []
        for c, lab in checks:
            cctx = ctx_from(fake, cx, cm, tf=c.get("tf")) if c.get("tf") else base
            out.append(("✔ " if eval_condition(c, cctx) else "✘ ") + lab)
        return out
    except Exception:
        return None


def journal_due():
    """(entry, horizon) pairs whose outcome is now measurable."""
    now = time.time()
    return [(e, h) for e in JOURNAL for h, s in J_HORIZONS.items()
            if h not in (e.get("out") or {}) and now >= e["ts"] + s]


# Short-lived caches so score/confluence conditions can use option levels and
# the live order book without hammering the feeds every 5s poll.
_opt_cond_cache = {}
_book_cond_cache = {}


async def get_option_levels_cached(session, coin, ttl=3600, monthly=False):
    key = (coin, "m" if monthly else "w")
    ent = _opt_cond_cache.get(key)
    if ent and time.time() - ent[0] < ttl:
        return ent[1]
    res = await compute_option_levels(session, coin, monthly=monthly)
    _opt_cond_cache[key] = (time.time(), res)
    return res


async def get_book_cached(session, coin, ttl=60):
    ent = _book_cond_cache.get(coin)
    if ent and time.time() - ent[0] < ttl:
        return ent[1]
    res = await compute_orderbook_walls(session, coin, min_notional=0.0,
                                        n_sig=3, per_side=20)
    _book_cond_cache[coin] = (time.time(), res)
    return res


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
            "conditions": b.get("conditions"),  # confluence/score: list of {type, op, value, tf, ...}
            "threshold": b.get("threshold"),   # score: min points to fire
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
                          "conditions", "note", "alert_enabled", "color", "threshold",
                          "kind"):  # kind: only confluence<->score via the ✎ editor
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


# OSI option symbol, e.g. NVDA240920C00120000 -> root, YYMMDD, C/P, strike*1000
_OPT_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _bs_gamma(S, K, sigma, T):
    """Black-Scholes gamma (r=0). Same for calls and puts."""
    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (S * sigma * math.sqrt(T))


async def compute_option_levels(session, coin, monthly=False):
    """Max pain + call/put walls + net GEX / gamma flip from CBOE's free
    delayed feed. Maps HL symbol (xyz:NVDA) -> underlying (NVDA).
    monthly=False -> nearest expiry (weekly); monthly=True -> the standard
    monthly opex (3rd Friday), whose bigger open interest makes its walls
    more structural. Returns a plain dict (with "error" on failure)."""
    sym = coin.split(":")[-1].upper()
    if not sym:
        return {"error": "no symbol"}
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    try:
        async with session.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=ClientTimeout(total=15)) as r:
            if r.status != 200:
                return {"error": f"No options data for {sym}."}
            j = await r.json()
    except Exception as e:
        return {"error": f"Options fetch failed: {e}"}

    data = j.get("data") or {}
    spot = _f(data.get("current_price"))
    today = dt.date.today()
    byexp = {}  # exp_date -> {"C": {strike: oi}, "P": {strike: oi}}
    gex_opts = []  # (strike, sign, oi, iv, T) across ALL expiries, for gamma flip / net GEX
    for o in data.get("options") or []:
        m = _OPT_RE.match(o.get("option", ""))
        if not m:
            continue
        _root, ymd, cp, sraw = m.groups()
        try:
            exp = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
        except Exception:
            continue
        strike = int(sraw) / 1000.0
        oi = o.get("open_interest") or 0
        d = byexp.setdefault(exp, {"C": {}, "P": {}})
        d[cp][strike] = d[cp].get(strike, 0) + oi
        iv = _f(o.get("iv")) or 0.0
        T = max((exp - today).days, 0) / 365.0
        if oi > 0 and iv > 0 and T > 0:
            gex_opts.append((strike, 1 if cp == "C" else -1, oi, iv, T))

    future = sorted(e for e in byexp if e >= today)
    if not future:
        return {"error": f"No upcoming expiry for {sym}."}
    if monthly:
        # standard monthly opex = 3rd Friday; else whatever is closest to ~30d
        m3 = [e for e in future if e.weekday() == 4 and 15 <= e.day <= 21]
        exp = m3[0] if m3 else min(future, key=lambda e: abs((e - today).days - 30))
    else:
        exp = future[0]
    calls, puts = byexp[exp]["C"], byexp[exp]["P"]
    strikes = sorted(set(calls) | set(puts))
    if not strikes:
        return {"error": f"Empty option chain for {sym}."}

    def pain(S):
        tot = 0.0
        for K, oi in calls.items():
            if K < S:
                tot += oi * (S - K)
        for K, oi in puts.items():
            if K > S:
                tot += oi * (K - S)
        return tot

    # net dealer gamma at a trial spot (calls +, puts -); sign convention where
    # below the flip = short gamma (moves amplified), above = long gamma (dampened)
    def net_gamma(S):
        return sum(sign * oi * _bs_gamma(S, K, iv, T) for (K, sign, oi, iv, T) in gex_opts)

    gamma_flip = None
    net_gex = None
    if gex_opts and spot:
        # $ GEX at spot, in millions (per 1% move)
        net_gex = sum(sign * _bs_gamma(spot, K, iv, T) * oi * 100 * spot * spot * 0.01
                      for (K, sign, oi, iv, T) in gex_opts) / 1e6
        lo, hi, steps = spot * 0.6, spot * 1.4, 240
        prev = prev_s = None
        for i in range(steps + 1):
            S = lo + (hi - lo) * i / steps
            g = net_gamma(S)
            if prev is not None and ((prev <= 0 < g) or (prev >= 0 > g)):
                gamma_flip = round(prev_s + (S - prev_s) * (0 - prev) / (g - prev), 2)
                break
            prev, prev_s = g, S

    return {
        "underlying": sym,
        "expiry": exp.isoformat(),
        "spot": spot,
        "max_pain": min(strikes, key=pain),
        "call_wall": max(calls, key=lambda k: calls[k]) if calls else None,
        "put_wall": max(puts, key=lambda k: puts[k]) if puts else None,
        "gamma_flip": gamma_flip,
        "net_gex": round(net_gex, 1) if net_gex is not None else None,
    }


async def api_option_levels(request):
    """Live button: compute levels and record today's snapshot for history."""
    coin = request.query.get("coin", "")
    res = await compute_option_levels(request.app["session"], coin)
    record_opt_snapshot(coin, res)
    return web.json_response(res)


async def api_option_history(request):
    """Daily series of recorded option levels for charting the trend."""
    h = OPTHIST.get(request.query.get("coin", ""), {})
    return web.json_response([dict(date=d, **v) for d, v in sorted(h.items())])


async def compute_orderbook_walls(session, coin, min_notional=1_000_000.0,
                                  n_sig=3, per_side=6):
    """Heaviest COMBINED resting-order zones in the live L2 book. Hyperliquid
    aggregates the book to `n_sig` significant figures, so each 'wall' sums many
    traders' orders sitting in one price band — the `orders` field is how many
    orders are combined (e.g. a 1200 sell wall = $12M across 571 orders), not a
    single person. Bids below price = buy walls (buyers/longs); asks above = sell
    walls (sellers/shorts). Only zones with combined USD notional >= min_notional.
    n_sig: 4 = tight (~±2%), 3 = medium (~±15%), 2 = wide (whole book)."""
    if not coin:
        return {"error": "no coin"}
    body = {"type": "l2Book", "coin": coin, "nSigFigs": n_sig}
    try:
        async with session.post(HL_INFO, json=body) as r:
            j = await r.json()
    except Exception as e:
        return {"error": f"Order book fetch failed: {e}"}
    levels = (j or {}).get("levels") or []
    if len(levels) < 2:
        return {"error": f"No order book for {coin}."}
    bids_raw, asks_raw = levels[0] or [], levels[1] or []

    def clean(side):
        out = []
        for lvl in side:
            px, sz = _f(lvl.get("px")), _f(lvl.get("sz"))
            if px is None or sz is None or px <= 0 or sz <= 0:
                continue
            notl = px * sz
            if notl >= min_notional:
                out.append({"px": px, "sz": sz, "notional": round(notl, 2),
                            "orders": int(lvl.get("n", 0) or 0)})
        out.sort(key=lambda x: -x["notional"])
        return out[:per_side]

    best_bid = _f(bids_raw[0].get("px")) if bids_raw else None
    best_ask = _f(asks_raw[0].get("px")) if asks_raw else None
    mid = ((best_bid + best_ask) / 2) if (best_bid and best_ask) else (best_bid or best_ask)
    return {"coin": coin, "sym": coin.split(":")[-1], "mid": mid, "nSigFigs": n_sig,
            "bids": clean(bids_raw), "asks": clean(asks_raw)}


async def api_orderbook_walls(request):
    """Live button: heaviest COMBINED order-book wall zones for a coin.
    Query: coin, min (min combined $, default 1M), agg (nSigFigs 2-5, default 3)."""
    coin = request.query.get("coin", "")
    try:
        min_n = max(0.0, float(request.query.get("min", 1_000_000.0)))
    except Exception:
        min_n = 1_000_000.0
    try:
        n_sig = int(float(request.query.get("agg", 3)))
    except Exception:
        n_sig = 3
    n_sig = max(2, min(5, n_sig))
    res = await compute_orderbook_walls(request.app["session"], coin, min_n, n_sig)
    return web.json_response(res)


def _rsi_close_for(closes, period, target):
    """Hypothetical next close that would put RSI exactly at `target`.
    RSI is monotonically increasing in the new close, so binary-search it."""
    if len(closes) < period + 1:
        return None
    base = closes[-1]
    lo, hi = base * 0.7, base * 1.3
    rlo, rhi = _rsi(closes + [lo], period), _rsi(closes + [hi], period)
    if rlo is None or rhi is None:
        return None
    if target <= rlo:   # even a -30% close keeps RSI above target
        return lo
    if target >= rhi:   # even a +30% close keeps RSI below target
        return hi
    for _ in range(40):
        mid = (lo + hi) / 2
        if _rsi(closes + [mid], period) < target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 6)


def _cond_price_range(c, ctx, spot):
    """(lo, hi) price band where this condition would be met at the next close;
    None side = unbounded. Returns None for conditions price can't solve
    (trend, volume, funding, patterns...)."""
    t = c.get("type"); op = c.get("op", ">")
    w = (_f(c.get("within")) or 0.5) / 100.0

    def around(v):
        if v is None:
            return None
        if op == "near":
            return (v * (1 - w), v * (1 + w))
        return (v, None) if op == ">" else (None, v)

    try:
        if t == "price":
            return around(float(c.get("value")))
        if t == "ema":
            # next close c > EMA_new(c) simplifies to c > current EMA — exact
            return around(_ema(ctx["closes"], int(c.get("period", 45))))
        if t == "vwap":
            return around(_vwap(ctx["candles"], c.get("anchor", "week")))
        if t == "avwap":
            at = c.get("anchorT")
            if at in (None, ""):
                return None
            return around(_vwap_anchored(ctx["candles"], int(float(at))))
        if t == "opt":
            o = ctx.get("opt") or {}
            return around(_f(o.get(c.get("level", "max_pain"))))
        if t == "optroom":
            o = ctx.get("opt") or {}
            cw = _f(o.get("call_wall"))
            return (None, cw / (1 + float(c.get("value", 3)) / 100.0)) if cw else None
        if t == "rsi":
            cstar = _rsi_close_for(ctx["closes"], int(c.get("period", 14)),
                                   float(c.get("value")))
            if cstar is None:
                return None
            return (cstar, None) if op == ">" else (None, cstar)
        if t == "rsiband":
            p = int(c.get("period", 14))
            return (_rsi_close_for(ctx["closes"], p, float(c.get("lo", 40))),
                    _rsi_close_for(ctx["closes"], p, float(c.get("hi", 65))))
        if t == "buywall":
            bk = ctx.get("book") or {}
            mn = float(c.get("min", 1)) * 1e6
            pct = float(c.get("pct", 1.5)) / 100.0
            bids = [b for b in bk.get("bids") or [] if b["notional"] >= mn]
            if not bids:
                return None
            b = max(bids, key=lambda x: x["px"])  # nearest qualifying wall below
            return (b["px"], b["px"] * (1 + pct))
        if t == "nosellwall":
            bk = ctx.get("book") or {}
            mn = float(c.get("min", 3)) * 1e6
            pct = float(c.get("pct", 1.0)) / 100.0
            asks = [a for a in bk.get("asks") or [] if a["notional"] >= mn]
            if not asks:
                return None  # nothing blocking anywhere -> no price constraint
            a = min(asks, key=lambda x: x["px"])
            return (None, a["px"] / (1 + pct))
    except Exception:
        return None
    return None


async def api_score_zone(request):
    """📍 For a score/confluence alert: the approximate price band where the
    price-solvable conditions would be met, plus met/not-met state for the
    rest. Powers the on-chart 'where should price be' view."""
    lid = request.query.get("id", "")
    async with levels_lock:
        l = next((x for x in LEVELS if x["id"] == lid), None)
    if not l or l.get("kind") not in ("confluence", "score"):
        return web.json_response({"error": "not a confluence/score alert"}, status=404)
    session = request.app["session"]
    coin = l["coin"]
    cx = (await fetch_ctx(session, [coin])).get(coin) or {}
    spot = cx.get("px")
    if spot is None:
        return web.json_response({"error": "no live price"}, status=502)
    conds = l.get("conditions") or []
    tfs = {l.get("timeframe", "4h")} | {c["tf"] for c in conds if c.get("tf")}
    candle_map = {}
    for tf in tfs:
        candle_map[(coin, tf)] = await get_candles(session, coin, tf)
    opt = (await get_option_levels_cached(session, coin)
           if any(c.get("type") in ("opt", "optroom") for c in conds) else None)
    book = (await get_book_cached(session, coin)
            if any(c.get("type") in ("buywall", "nosellwall") for c in conds) else None)
    base_ctx = ctx_from(l, cx, candle_map)
    base_ctx["opt"], base_ctx["book"] = opt, book
    ranges, states = [], []
    lo = hi = None
    for c in conds:
        if c.get("tf") and c["tf"] != l.get("timeframe", "4h"):
            cctx = ctx_from(l, cx, candle_map, tf=c["tf"])
            cctx["opt"], cctx["book"] = opt, book
        else:
            cctx = base_ctx
        met = bool(eval_condition(c, cctx))
        rng = _cond_price_range(c, cctx, spot)
        if rng and not (rng[0] is None and rng[1] is None):
            rlo, rhi = rng
            ranges.append({"text": cond_text(c), "met": met,
                           "lo": round(rlo, 6) if rlo is not None else None,
                           "hi": round(rhi, 6) if rhi is not None else None})
            if rlo is not None:
                lo = rlo if lo is None else max(lo, rlo)
            if rhi is not None:
                hi = rhi if hi is None else min(hi, rhi)
        else:
            states.append({"text": cond_text(c), "met": met})
    zone = None
    if ranges:
        zlo = lo if lo is not None else spot * 0.9   # clamp open sides for display
        zhi = hi if hi is not None else spot * 1.1
        if zlo < zhi:
            zone = {"lo": round(zlo, 6), "hi": round(zhi, 6)}
    return web.json_response({"spot": spot, "ranges": ranges,
                              "states": states, "zone": zone})


def _swing_lows(candles, k=3, count=4):
    """Recent pivot lows: bars whose low is the lowest of k bars on each side."""
    out = []
    for i in range(k, len(candles) - k):
        lo = candles[i]["l"]
        if (all(lo <= candles[i - j]["l"] for j in range(1, k + 1))
                and all(lo <= candles[i + j]["l"] for j in range(1, k + 1))):
            out.append((candles[i]["t"], lo))
    return out[-count:]


def _selloff_anchor(candles, min_drop_pct=8.0, k=5):
    """Bar time (ms) of the swing high that started the most recent big
    decline (pivot high followed by a drop >= min_drop_pct). The VWAP
    anchored there = average price of everyone caught in the sell-off —
    a classic support/reclaim level."""
    best = None
    for i in range(k, len(candles) - k):
        h = candles[i]["h"]
        if (all(h >= candles[i - j]["h"] for j in range(1, k + 1))
                and all(h >= candles[i + j]["h"] for j in range(1, k + 1))):
            after_min = min(c["l"] for c in candles[i:])
            if h > 0 and (h - after_min) / h * 100.0 >= min_drop_pct:
                best = candles[i]["t"]  # keep the LATEST qualifying sell-off
    return best


async def api_long_plan(request):
    """⭐ Best realistic long entry: collect support-type levels (solid tier
    weighted highest), cluster ones within ~1.2%, score clusters by combined
    weight + proximity to spot, return the winner with its member levels as
    the on-chart explanation. mode=pullback (below spot) or reclaim (above)."""
    coin = request.query.get("coin", "")
    session = request.app["session"]
    cx = (await fetch_ctx(session, [coin])).get(coin) or {}
    spot = cx.get("px")
    if not spot:
        return web.json_response({"error": "no live price"}, status=502)
    c4 = await get_candles(session, coin, "4h")
    c1 = await get_candles(session, coin, "1d")
    if len(c4) < 60:
        return web.json_response({"error": "not enough candle history"}, status=502)
    closes4 = [c["c"] for c in c4]
    cands = []  # (price, label, weight)  weight: 3=solid, 2=good, 1=heuristic

    def add(p, label, w):
        if p and p > 0 and spot * 0.80 <= p <= spot * 1.06:  # realistic reach only
            cands.append((float(p), label, w))

    add(_ema(closes4, 200), "EMA200 4h — major dynamic support", 3)
    add(_ema([c["c"] for c in c1], 50), "EMA50 daily — trend support", 3)
    add(_vwap(c4, "week"), "weekly VWAP — this week's average price", 2)
    add(_vwap(c4, "month"), "monthly VWAP — this month's average price", 2)
    for t, lo in _swing_lows(c4, 3, 4):
        d = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
        add(lo, f"swing low {d.strftime('%b %d')} — proven buyer level", 2)
    # AVWAP anchored at the start of the most recent big sell-off:
    # the average price of everyone trapped in the dump (reclaim level)
    at = _selloff_anchor(c4)
    if at:
        d0 = dt.datetime.fromtimestamp(at / 1000, dt.timezone.utc)
        add(_vwap_anchored(c4, at // 1000),
            f"AVWAP from {d0.strftime('%b %d')} sell-off — trapped-seller average", 2)
    # any anchored VWAPs the user drew themselves (⚓) on this coin
    for ind in (load_indicators() or []):
        if ind.get("type") == "vwapa" and (not ind.get("coin") or ind.get("coin") == coin):
            a = (ind.get("params") or {}).get("anchorT")
            if a:
                try:
                    d0 = dt.datetime.fromtimestamp(float(a), dt.timezone.utc)
                    add(_vwap_anchored(c4, int(float(a))),
                        f"your ⚓ AVWAP ({d0.strftime('%b %d')})", 2)
                except Exception:
                    pass
    # options: weekly (nearest expiry) AND monthly opex (bigger OI = more
    # structural, so monthly put wall gets a higher weight)
    overhead = []  # ceilings above spot that argue AGAINST longing into them

    def over(p, label):
        p = _f(p)
        if p and spot < p <= spot * 1.08:
            overhead.append({"price": round(p, 4), "label": label})

    optw = await get_option_levels_cached(session, coin)
    optm = await get_option_levels_cached(session, coin, monthly=True)
    if optw and not optw.get("error"):
        add(optw.get("put_wall"), f"put wall wk {optw.get('expiry', '')} — options support", 1)
        mp = _f(optw.get("max_pain"))
        if mp and mp <= spot:
            add(mp, f"max pain wk {optw.get('expiry', '')} — options magnet", 1)
        over(optw.get("call_wall"), f"call wall wk {optw.get('expiry', '')}")
    if (optm and not optm.get("error")
            and optm.get("expiry") != (optw or {}).get("expiry")):
        add(optm.get("put_wall"), f"put wall MONTHLY {optm.get('expiry', '')} — big-OI support", 2)
        mp = _f(optm.get("max_pain"))
        if mp and mp <= spot:
            add(mp, f"max pain MONTHLY {optm.get('expiry', '')} — options magnet", 1)
        over(optm.get("call_wall"), f"call wall MONTHLY {optm.get('expiry', '')}")
    # live order book: up to 3 combined buy walls below as support evidence,
    # nearest big sell wall above as an overhead ceiling
    book = await get_book_cached(session, coin)
    if book and not book.get("error"):
        bids = sorted((b for b in book.get("bids") or []
                       if b["notional"] >= 5e5 and b["px"] < spot),
                      key=lambda x: -x["notional"])[:3]
        for b in bids:
            add(b["px"], f"buy wall ${b['notional']/1e6:.1f}M — live orders (can move)", 1)
        asks = [a for a in book.get("asks") or []
                if a["notional"] >= 1e6 and a["px"] > spot]
        if asks:
            a = min(asks, key=lambda x: x["px"])
            over(a["px"], f"sell wall ${a['notional']/1e6:.1f}M — live orders (can move)")
    overhead.sort(key=lambda o: o["price"])
    if not cands:
        return web.json_response({"error": "no support levels in realistic range"})

    # cluster levels within 1.2% of each other (greedy, top-down by price)
    cands.sort(key=lambda x: -x[0])
    clusters = []
    for p, label, w in cands:
        placed = False
        for cl in clusters:
            if abs(p - cl["ref"]) / cl["ref"] * 100.0 <= 1.2:
                cl["members"].append({"price": round(p, 4), "label": label, "w": w})
                placed = True
                break
        if not placed:
            clusters.append({"ref": p, "members": [{"price": round(p, 4), "label": label, "w": w}]})
    for cl in clusters:
        ps = [m["price"] for m in cl["members"]]
        ws = [m["w"] for m in cl["members"]]
        cl["entry"] = round(sum(p * w for p, w in zip(ps, ws)) / sum(ws), 4)
        dist = abs(spot - cl["entry"]) / spot * 100.0
        # combined evidence + closer-to-spot bonus; below-spot pullbacks preferred
        cl["score"] = sum(ws) + max(0.0, 2.0 - dist / 3.0) + (1.0 if cl["entry"] <= spot else 0.0)
        # a ceiling (call wall / big sell wall) within 1.5% above the entry
        # caps the upside — punish that entry
        for ov in overhead:
            gap = (ov["price"] - cl["entry"]) / cl["entry"] * 100.0
            if 0 < gap <= 1.5:
                cl["score"] -= 1.5
    clusters.sort(key=lambda c: -c["score"])
    best = clusters[0]
    return web.json_response({
        "spot": spot,
        "entry": best["entry"],
        "mode": "pullback" if best["entry"] <= spot else "reclaim",
        "members": [{k: m[k] for k in ("price", "label", "w")} for m in best["members"]],
        "overhead": overhead,
        "alternatives": [{"entry": c["entry"], "n": len(c["members"])} for c in clusters[1:4]],
    })


async def api_journal(request):
    """Signal journal: recent fired alerts + per-setup outcome stats, so you
    can see which setups actually make money on your coins."""
    groups = {}
    for e in JOURNAL:
        groups.setdefault((e["coin"], e["label"]), []).append(e)
    stats = []
    for (coin_, label), es in groups.items():
        row = {"coin": coin_, "label": label, "n": len(es)}
        for h in ("1h", "4h", "1d"):
            vals = sorted(e["out"][h] for e in es if h in (e.get("out") or {}))
            if vals:
                row[h] = {"n": len(vals),
                          "win": round(100.0 * sum(1 for v in vals if v > 0) / len(vals)),
                          "med": round(vals[len(vals) // 2], 2)}
        stats.append(row)
    stats.sort(key=lambda r: -r["n"])
    return web.json_response({"entries": JOURNAL[-100:][::-1], "stats": stats})


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


def _ema_series(vals, p):
    """Full EMA series (None during warmup) — mirrors the front-end ema()."""
    k = 2 / (p + 1)
    e = None
    out = []
    for i, x in enumerate(vals):
        e = x if e is None else x * k + e * (1 - k)
        out.append(None if i < p - 1 else e)
    return out


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


def _yz_sigma(candles, n):
    """Yang-Zhang realized volatility over the last n bars (candles: o/h/l/c)."""
    m = len(candles)
    if m < n + 1:
        n = m - 1
    if n < 2:
        return 1e-10
    OR = []; CO = []; RS = []
    for i in range(m - n, m):
        o, h, l, c = candles[i]["o"], candles[i]["h"], candles[i]["l"], candles[i]["c"]
        pc = candles[i - 1]["c"]
        if min(o, h, l, c, pc) <= 0:
            continue
        OR.append(math.log(o / pc)); CO.append(math.log(c / o))
        RS.append(math.log(h / o) * math.log(h / c) + math.log(l / o) * math.log(l / c))
    if len(OR) < 2:
        return 1e-10
    mean = lambda a: sum(a) / len(a)
    var = lambda a: (lambda mu: sum((x - mu) ** 2 for x in a) / len(a))(mean(a))
    k = 0.34 / (1.34 + (n + 1) / max(n - 1, 1))
    sq = var(OR) + k * var(CO) + (1 - k) * mean(RS)
    return max(math.sqrt(max(sq, 0.0)), 1e-10)


def _ics_channel(candles, period, groups, sig_len, thresh):
    """Reconstructed ST-EP06 core: σ-normalized block-trend channel. Returns
    {dir, angle, upper, lower} at the last bar (fit excludes the last bar so a
    fresh breakout can be detected), or None if not enough data."""
    N = period * groups
    m = len(candles)
    if m < N + sig_len + 2:
        return None
    last = m - 1
    gm = []; cx = []
    for i in range(groups):
        end = last - i * period; start = end - period + 1
        if start < 0:
            break
        seg = candles[start:end + 1]
        hi = max(c["h"] for c in seg); lo = min(c["l"] for c in seg)
        if hi <= 0 or lo <= 0:
            continue
        gm.append(math.exp((math.log(hi) + math.log(lo)) / 2)); cx.append(end - period // 2)
    gm.reverse(); cx.reverse()
    if len(gm) < 2:
        return None
    bs = be = cs = ce = 0; cdir = 0
    for i in range(1, len(gm)):
        d = (gm[i] > gm[i - 1]) - (gm[i] < gm[i - 1])
        if d != 0 and d == cdir:
            ce = i
        else:
            cs = i - 1; ce = i; cdir = d
        if ce - cs > be - bs:
            bs, be = cs, ce
    sig = _yz_sigma(candles, sig_len)
    slope = ((math.log(gm[be]) - math.log(gm[bs])) / (cx[be] - cx[bs])
             if cx[be] != cx[bs] and gm[bs] > 0 and gm[be] > 0 else 0.0)
    angle = math.atan(slope / sig) * 180 / math.pi
    direction = 1 if angle > thresh else -1 if angle < -thresh else 0
    first = last - N + 1
    up = -1e18; lo = 1e18
    for j in range(first, last):  # exclude last bar -> lets a new bar break out
        r = j - first
        rh = math.log(candles[j]["h"]) - slope * r
        rl = math.log(candles[j]["l"]) - slope * r
        if rh > up:
            up = rh
        if rl < lo:
            lo = rl
    rl2 = last - first
    return {"dir": direction, "angle": angle,
            "upper": math.exp(slope * rl2 + up), "lower": math.exp(slope * rl2 + lo)}


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


def ctx_from(l, cx, candle_map, tf=None):
    """Build evaluation context for a confluence/score alert from prefetched
    data. tf overrides the alert's timeframe for per-condition multi-TF."""
    tf = tf or l.get("timeframe", "4h")
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
    # Must NEVER raise — an exception here aborts the whole alert loop and
    # silently blocks every alert. Missing data => condition simply not met.
    try:
        t = cond.get("type")
        op = cond.get("op", ">")
        price = ctx["price"]

        def cmp(a, b):  # null-safe: missing value => not met (no None comparison)
            if a is None or b is None:
                return False
            if op == "near":  # within ±X% of the target (location, not direction)
                # `or 0.5` also covers null/NaN from a cleared UI field —
                # otherwise the condition would be silently never-met
                return b != 0 and abs(a - b) / abs(b) * 100.0 <= float(_f(cond.get("within")) or 0.5)
            return a > b if op == ">" else a < b

        def val():
            return float(cond.get("value"))

        if price is None:
            return False
        if t == "price":
            return cmp(price, val())
        if t == "funding":
            return cmp(ctx["funding"], val())
        if t == "oi":
            return cmp(ctx["oi"], val())
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
            return cmp(_rsi(ctx["closes"], int(cond.get("period", 14))), val())
        if t == "rsiband":
            # RSI inside a sane band — e.g. 40..65 = pullback zone, not chasing
            r = _rsi(ctx["closes"], int(cond.get("period", 14)))
            return (r is not None
                    and float(cond.get("lo", 40)) <= r <= float(cond.get("hi", 65)))
        if t == "volspike":
            # last closed candle's volume >= mult × average of the prior N bars
            cc = ctx["candles"]
            p = int(cond.get("period", 20))
            if len(cc) < p + 1:
                return False
            last = cc[-1]
            avg = sum(c["v"] for c in cc[-p - 1:-1]) / p
            if avg <= 0:
                return False
            if str(cond.get("green", "1")) == "1" and last["c"] < last["o"]:
                return False  # require a green (buying) candle
            return last["v"] >= float(cond.get("mult", 1.5)) * avg
        if t == "opt":
            # price vs an option level (max pain / call wall / put wall / gamma flip)
            o = ctx.get("opt") or {}
            return cmp(price, _f(o.get(cond.get("level", "max_pain"))))
        if t == "optroom":
            # upside room to the call wall — don't buy right under the ceiling
            o = ctx.get("opt") or {}
            cw = _f(o.get("call_wall"))
            if cw is None or not price:
                return False
            return (cw - price) / price * 100.0 >= float(cond.get("value", 3))
        if t == "buywall":
            # combined buy wall of >= $minM within pct% below price (support)
            bk = ctx.get("book") or {}
            mn = float(cond.get("min", 1)) * 1e6
            pct = float(cond.get("pct", 1.5))
            return any(b["px"] < price
                       and (price - b["px"]) / price * 100.0 <= pct
                       and b["notional"] >= mn
                       for b in bk.get("bids") or [])
        if t == "nosellwall":
            # NO combined sell wall of >= $minM within pct% above (clear runway).
            # Missing book data => not met (never award the point blindly).
            bk = ctx.get("book") or {}
            if not bk or bk.get("error") or bk.get("asks") is None:
                return False
            mn = float(cond.get("min", 3)) * 1e6
            pct = float(cond.get("pct", 1.0))
            return not any(a["px"] > price
                           and (a["px"] - price) / price * 100.0 <= pct
                           and a["notional"] >= mn
                           for a in bk.get("asks") or [])
        if t == "pattern":
            return _pattern(ctx["candles"], cond.get("name", ""))
        if t == "notrend":
            # fires when the last `bars` closed candles are ALL in the neutral
            # zone — neither >thresh% above EMA (uptrend) nor >thresh% below.
            closes = ctx["closes"]
            n = len(closes)
            bars = int(cond.get("bars", 5))
            ema_len = int(cond.get("emaLen", 50))
            lookback = int(cond.get("lookback", 50))
            th = float(cond.get("thresh", 70)) / 100
            # need full lookback windows past EMA warmup, else counts are diluted
            if n < ema_len + lookback + bars:
                return False
            ev = _ema_series(closes, ema_len)
            for i in range(n - bars, n):
                a = b = 0
                for j in range(lookback):
                    e2 = ev[i - j]
                    if e2 is None:
                        continue
                    if closes[i - j] > e2:
                        a += 1
                    elif closes[i - j] < e2:
                        b += 1
                if (a / lookback > th) or (b / lookback > th):
                    return False  # this bar is trending -> not "no-trend for N"
            return True
        if t == "trend":
            # true while the latest closed bar is in an up/down trend; edge-fires
            # exactly when the trend turns on. dir = up | down | any.
            closes = ctx["closes"]
            n = len(closes)
            ema_len = int(cond.get("emaLen", 50))
            lookback = int(cond.get("lookback", 50))
            th = float(cond.get("thresh", 70)) / 100
            if n < ema_len + lookback + 1:
                return False
            ev = _ema_series(closes, ema_len)
            i = n - 1
            a = b = 0
            for j in range(lookback):
                e2 = ev[i - j]
                if e2 is None:
                    continue
                if closes[i - j] > e2:
                    a += 1
                elif closes[i - j] < e2:
                    b += 1
            up, dn = a / lookback > th, b / lookback > th
            d = cond.get("dir", "any")
            return up if d == "up" else dn if d == "down" else (up or dn)
        if t == "ics":
            ch = _ics_channel(ctx["candles"], int(cond.get("period", 13)),
                              int(cond.get("groups", 5)), int(cond.get("sig", 20)),
                              float(cond.get("thresh", 0.5)))
            if not ch:
                return False
            up_break = price > ch["upper"]
            dn_break = price < ch["lower"]
            d = cond.get("dir", "any")
            return up_break if d == "up" else dn_break if d == "down" else (up_break or dn_break)
        return False
    except Exception as e:
        print("eval_condition error", cond, e)
        return False


_OPT_NAMES = {"max_pain": "max pain", "call_wall": "call wall",
              "put_wall": "put wall", "gamma_flip": "gamma flip"}


def cond_text(c):
    t = c.get("type"); op = c.get("op", ">")
    if op == "near":
        op = f"≈ (±{c.get('within', 0.5)}%)"
    tf = f" [{c['tf']}]" if c.get("tf") else ""
    if t == "price":
        return f"price {op} {c.get('value')}" + tf
    if t == "funding":
        return f"funding {op} {c.get('value')}" + tf
    if t == "oi":
        return f"OI {op} {c.get('value')}" + tf
    if t == "vwap":
        return f"price {op} VWAP({c.get('anchor', 'week')})" + tf
    if t == "avwap":
        return f"price {op} anchored VWAP" + tf
    if t == "ema":
        return f"price {op} EMA{c.get('period', 45)}" + tf
    if t == "rsi":
        return f"RSI {op} {c.get('value')}" + tf
    if t == "rsiband":
        return f"RSI {c.get('lo', 40)}–{c.get('hi', 65)}" + tf
    if t == "volspike":
        g = " green" if str(c.get("green", "1")) == "1" else ""
        return f"volume ≥{c.get('mult', 1.5)}× avg{g}" + tf
    if t == "opt":
        return f"price {op} {_OPT_NAMES.get(c.get('level'), c.get('level'))}"
    if t == "optroom":
        return f"room to call wall ≥{c.get('value', 3)}%"
    if t == "buywall":
        return f"buy wall ≥${c.get('min', 1)}M within {c.get('pct', 1.5)}%"
    if t == "nosellwall":
        return f"no sell wall ≥${c.get('min', 3)}M within {c.get('pct', 1.0)}%"
    if t == "pattern":
        return f"candle = {c.get('name')}" + tf
    if t == "notrend":
        return f"no-trend {c.get('bars', 5)} bars (vs EMA{c.get('emaLen', 50)})" + tf
    if t == "trend":
        return f"{c.get('dir', 'any')} trend (EMA{c.get('emaLen', 50)})" + tf
    if t == "ics":
        return f"ICS channel breakout ({c.get('dir', 'any')})" + tf
    return str(t)


def _px_text(v):
    """Never let a missing price crash alert formatting — a crash there would
    re-fire every poll and silently block the whole delivery cycle."""
    return f"{v:.6g}" if isinstance(v, (int, float)) else "n/a"


def format_confluence_alert(l, ctx):
    desc = "\n".join("✔ " + cond_text(c) for c in (l.get("conditions") or []))
    extra = f"price {_px_text(ctx.get('price'))}"
    if any(c.get("type") == "funding" for c in (l.get("conditions") or [])):
        extra += f" · funding {ctx['funding'] * 100:.4f}%/hr"
    return (f"🔔🔗 <b>{l['coin']}</b> confluence met ({l.get('timeframe', '4h')}"
            f"{', close' if l.get('confirm') == 'close' else ''})\n{desc}\n{extra}"
            + (f"\n📝 {l['note']}" if l.get("note") else ""))


def format_score_alert(l, conds, results, ctx, thr):
    """Checklist message: which conditions scored and which didn't."""
    score = sum(1 for r in results if r)
    rows = "\n".join(("✔ " if r else "✘ ") + cond_text(c)
                     for c, r in zip(conds, results))
    return (f"🎯 <b>{l['coin']}</b> setup score <b>{score}/{len(conds)}</b> "
            f"(need {thr}, {l.get('timeframe', '4h')}"
            f"{', close' if l.get('confirm') == 'close' else ''})\n{rows}\n"
            f"price {_px_text(ctx.get('price'))}"
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


_JSAVE = [0.0]  # last journal save (throttle for dd/up updates)


async def alert_loop(app):
    session = app["session"]
    print(f"alert daemon running (poll {POLL_SECONDS}s, "
          f"telegram {'ON' if TG_TOKEN and TG_CHAT else 'OFF'})")
    # heartbeat so you KNOW the moment a new deploy is live + how many alerts are armed
    armed = sum(1 for l in LEVELS if l.get("alert_enabled"))
    await send_telegram(session, f"✅ <b>HL Chart</b> daemon online — {armed} alert(s) armed.")
    while True:
        try:
            async with levels_lock:
                active = [l for l in LEVELS if l.get("alert_enabled")]
            # journal entries whose +1h/+4h/+1d outcome is now measurable also
            # need a price, even if that coin has no active alert right now —
            # as do entries still inside the 24h drawdown/uprise window
            due = journal_due()
            now_ts = time.time()
            tracking = [e for e in JOURNAL if now_ts - e["ts"] <= 86400 + POLL_SECONDS]
            coins = sorted(set(l["coin"] for l in active)
                           | set(e["coin"] for e, _ in due)
                           | set(e["coin"] for e in tracking))
            ctxs = await fetch_ctx(session, coins) if coins else {}
            # running max-rise (up) / max-drawdown (dd) per young journal entry;
            # saved throttled so extremes don't hammer the disk every poll
            jdirty = False
            for e in tracking:
                px = (ctxs.get(e["coin"]) or {}).get("px")
                if px and e.get("price"):
                    ch = (px - e["price"]) / e["price"] * 100.0
                    if ch < e.get("dd", 0.0):
                        e["dd"] = round(ch, 3); jdirty = True
                    if ch > e.get("up", 0.0):
                        e["up"] = round(ch, 3); jdirty = True
            if jdirty and now_ts - _JSAVE[0] > 30:
                save_journal(); _JSAVE[0] = now_ts
            if active:
                # prefetch candles (incl. per-condition timeframes) and, where
                # conditions need them, option levels + the live order book
                tf_needs = set()
                opt_coins, book_coins = set(), set()
                for l in active:
                    if l.get("kind") in ("confluence", "score") or l.get("confirm") == "close":
                        tf_needs.add((l["coin"], l.get("timeframe", "4h")))
                    if l.get("kind") in ("confluence", "score"):
                        for c in (l.get("conditions") or []):
                            if c.get("tf"):
                                tf_needs.add((l["coin"], c["tf"]))
                            if c.get("type") in ("opt", "optroom"):
                                opt_coins.add(l["coin"])
                            if c.get("type") in ("buywall", "nosellwall"):
                                book_coins.add(l["coin"])
                candle_map = {}
                for cn, tf in tf_needs:
                    candle_map[(cn, tf)] = await get_candles(session, cn, tf)
                opt_map = {c: await get_option_levels_cached(session, c) for c in opt_coins}
                book_map = {c: await get_book_cached(session, c) for c in book_coins}

                outbox, changed = [], False
                for l in active:
                    cx = ctxs.get(l["coin"])
                    if not cx:
                        continue
                    # ---- confluence (ALL conditions) / score (≥ threshold points) ----
                    if l.get("kind") in ("confluence", "score"):
                        conds = l.get("conditions") or []
                        ctx = ctx_from(l, cx, candle_map)
                        ctx["opt"] = opt_map.get(l["coin"])
                        ctx["book"] = book_map.get(l["coin"])
                        results = []
                        for c in conds:
                            if c.get("tf") and c["tf"] != l.get("timeframe", "4h"):
                                cctx = ctx_from(l, cx, candle_map, tf=c["tf"])
                                cctx["opt"], cctx["book"] = ctx["opt"], ctx["book"]
                            else:
                                cctx = ctx
                            results.append(bool(eval_condition(c, cctx)))
                        if l.get("kind") == "score":
                            try:
                                thr = int(l.get("threshold") or 0)
                            except Exception:
                                thr = 0
                            thr = thr or max(1, len(conds) - 2)
                            met = bool(conds) and sum(results) >= thr
                            msg = format_score_alert(l, conds, results, ctx, thr) if met else None
                            jlabel = l.get("note") or "🎯 score"
                        else:
                            met = bool(conds) and all(results)
                            msg = format_confluence_alert(l, ctx) if met else None
                            jlabel = l.get("note") or "🔗 confluence"
                        jwhy = [("✔ " if r else "✘ ") + cond_text(c)
                                for c, r in zip(conds, results)]
                        prev = l.get("last_met")
                        if prev is None:
                            # first evaluation after create/edit: arm, and if the
                            # condition is ALREADY true, fire once now so a fresh
                            # alert confirms itself instead of sitting silent.
                            l["last_met"] = met
                            changed = True
                            if met and not muted():
                                outbox.append(msg)
                                journal_add(l["coin"], jlabel, ctx["price"], jwhy)
                                if l.get("repeat", "always") == "once":
                                    l["alert_enabled"] = False
                            continue
                        if met != prev:
                            l["last_met"] = met
                            changed = True
                        if met and not prev and not muted():  # edge: conditions/score just became true
                            outbox.append(msg)
                            journal_add(l["coin"], jlabel, ctx["price"], jwhy)
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
                            journal_add(l["coin"], l.get("note")
                                        or f"{'▲' if side == 'above' else '▼'} {label} {target:g}", px,
                                        await snapshot_why(session, l["coin"], cx))
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
            # fill in journal outcomes that just became measurable (+1h/+4h/+1d)
            if due:
                jch = False
                for e, h in due:
                    px = (ctxs.get(e["coin"]) or {}).get("px")
                    if px and e.get("price"):
                        e.setdefault("out", {})[h] = round(
                            (px - e["price"]) / e["price"] * 100.0, 3)
                        jch = True
                if jch:
                    save_journal()
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


# ---------- market scanner (📋) ----------
# Runs the clean-core LONG checklist across the busiest coins so setups find
# YOU — on demand via /api/scan, and once a day as a Telegram digest.
SCAN_HOUR_UTC = os.environ.get("SCAN_HOUR_UTC", "13:00")  # ~pre-US-open
SCAN_TOP_N = int(os.environ.get("SCAN_TOP_N", "20"))


def _scan_minute():
    try:
        h, m = SCAN_HOUR_UTC.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 13 * 60


async def scan_setups(session, top_n=12):
    """Score the clean-core checklist for the top_n coins by 24h volume.
    Returns sorted [(score, coin, px, misses)] or None on market-data failure."""
    try:
        async with session.post(HL_INFO, json={"type": "metaAndAssetCtxs", "dex": "xyz"}) as r:
            m = await r.json()
        uni, ctxarr = m[0]["universe"], m[1]
    except Exception as e:
        print("scan_setups market fetch error", e)
        return None
    rows = []
    for i, a in enumerate(uni):
        if a.get("isDelisted"):
            continue
        try:
            vol = float(ctxarr[i].get("dayNtlVlm", 0))
        except Exception:
            vol = 0.0
        rows.append((vol, a["name"], ctxarr[i]))
    rows.sort(key=lambda x: -x[0])
    out = []
    for _vol, name, c in rows[:top_n]:
        cx = {"px": _f(c.get("markPx")), "funding": _f(c.get("funding")) or 0.0,
              "oi": _f(c.get("openInterest")) or 0.0}
        why = await snapshot_why(session, name, cx)
        if why:
            score = sum(1 for w in why if w.startswith("✔"))
            out.append((score, name, cx["px"],
                        [w[2:] for w in why if w.startswith("✘")]))
        await asyncio.sleep(0.25)  # be gentle on the API
    out.sort(key=lambda x: -x[0])
    return out


def format_scan(results, top_n):
    lines = [f"📋 <b>LONG scan</b> — clean-core checklist, top {top_n} by volume"]
    shown = 0
    for score, name, px, misses in results:
        if score >= 4 and shown < 8:
            icon = "🟢" if score >= 5 else "🟡"
            miss = f" — needs: {', '.join(misses)}" if misses else ""
            lines.append(f"{icon} <b>{name.split(':')[-1]}</b> {score}/6 @ {_px_text(px)}{miss}")
            shown += 1
    if not shown:
        best = results[0] if results else None
        lines.append("nothing ≥4/6 right now"
                     + (f" (best: {best[1].split(':')[-1]} {best[0]}/6)" if best else ""))
    return "\n".join(lines)


async def api_scan(request):
    """📋 on-demand scan for the UI. ?n= how many top-volume coins (3-25)."""
    try:
        n = max(3, min(25, int(request.query.get("n", 12))))
    except Exception:
        n = 12
    results = await scan_setups(request.app["session"], n)
    if results is None:
        return web.json_response({"error": "market data unavailable"}, status=502)
    return web.json_response({"results": [
        {"coin": nm, "score": s, "px": px, "misses": ms}
        for s, nm, px, ms in results]})


async def daily_scan_loop(app):
    """One Telegram digest per day at SCAN_HOUR_UTC (durable via the outbox).
    Marks the day done even when muted so unmuting doesn't flood."""
    session = app["session"]
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            if now.hour * 60 + now.minute >= _scan_minute():
                today = now.date().isoformat()
                if SETTINGS.get("last_scan") != today:
                    results = await scan_setups(session, SCAN_TOP_N)
                    if results is not None:
                        if not muted():
                            PENDING.append(format_scan(results, SCAN_TOP_N))
                            save_outbox()
                        SETTINGS["last_scan"] = today
                        save_settings()
        except Exception as e:
            print("daily_scan_loop error", e)
        await asyncio.sleep(600)


async def option_history_loop(app):
    """Snapshot option levels once per day at ~14:30 UTC (~10:30am ET), just
    after the morning OI publishes — so each dated snapshot is that day's fresh
    walls. Covers your 📌 coins plus the auto-watchlist."""
    session = app["session"]
    SNAP_MIN = 14 * 60 + 30  # 14:30 UTC
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            if now.hour * 60 + now.minute >= SNAP_MIN:
                today = now.date().isoformat()
                for coin in sorted(set(OPTHIST.keys()) | set(OPT_WATCH)):
                    if OPTHIST.get(coin, {}).get(today):
                        continue  # already captured today
                    res = await compute_option_levels(session, coin)
                    record_opt_snapshot(coin, res)
                    await asyncio.sleep(2)  # be gentle on the feed
        except Exception as e:
            print("option_history_loop error", e)
        await asyncio.sleep(900)  # re-check every 15 min so it fires promptly


# ---------- app wiring ----------
async def on_startup(app):
    app["session"] = ClientSession(connector=TCPConnector(ssl=_ssl_context()))
    app["alert_task"] = asyncio.create_task(alert_loop(app))
    app["opthist_task"] = asyncio.create_task(option_history_loop(app))
    app["scan_task"] = asyncio.create_task(daily_scan_loop(app))


async def on_cleanup(app):
    app["alert_task"].cancel()
    app["opthist_task"].cancel()
    app["scan_task"].cancel()
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
        web.get("/api/optionlevels", api_option_levels),
        web.get("/api/optionhistory", api_option_history),
        web.get("/api/orderbookwalls", api_orderbook_walls),
        web.get("/api/journal", api_journal),
        web.get("/api/scorezone", api_score_zone),
        web.get("/api/longplan", api_long_plan),
        web.get("/api/scan", api_scan),
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

"""
Trading Analyzer Bot — Laeuft 24/7 auf Railway
Binance Daten + KI-Analyse + Telegram Alerts
"""
import os, time, logging, requests
from datetime import datetime

# ── KONFIGURATION (aus Railway Umgebungsvariablen) ────────────
TELEGRAM_TOKEN       = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
CONFIDENCE_THRESHOLD = int(os.environ.get("CONFIDENCE_THRESHOLD", "75"))
SCAN_INTERVAL        = int(os.environ.get("SCAN_INTERVAL", "300"))
COOLDOWN_MINUTES     = int(os.environ.get("COOLDOWN_MINUTES", "60"))
RISK_PROFILE         = os.environ.get("RISK_PROFILE", "ausgewogen")  # konservativ / ausgewogen / aggressiv

MARKETS = [
    {"symbol": "BTCUSDT", "interval": "1h", "name": "BTC/USDT"},
    {"symbol": "ETHUSDT", "interval": "1h", "name": "ETH/USDT"},
    {"symbol": "SOLUSDT", "interval": "1h", "name": "SOL/USDT"},
    {"symbol": "BNBUSDT", "interval": "1h", "name": "BNB/USDT"},
]

# ── RISIKOPROFILE ─────────────────────────────────────────────
RISK_PROFILES = {
    "konservativ": {
        "signal_threshold": 0.5,
        "rsi_oversold": 25, "rsi_overbought": 75,
        "weights": {"RSI": 0.30, "MACD": 0.20, "EMA": 0.20, "SMA": 0.20, "SR": 0.10},
        "label": "Konservativ",
    },
    "ausgewogen": {
        "signal_threshold": 0.3,
        "rsi_oversold": 35, "rsi_overbought": 65,
        "weights": {"RSI": 0.25, "MACD": 0.25, "EMA": 0.20, "SMA": 0.15, "SR": 0.15},
        "label": "Ausgewogen",
    },
    "aggressiv": {
        "signal_threshold": 0.15,
        "rsi_oversold": 45, "rsi_overbought": 55,
        "weights": {"RSI": 0.20, "MACD": 0.30, "EMA": 0.25, "SMA": 0.10, "SR": 0.15},
        "label": "Aggressiv",
    },
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── BINANCE API ───────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=150):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        r.raise_for_status()
        return [{"time": k[0], "open": float(k[1]), "high": float(k[2]),
                 "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                for k in r.json()]
    except Exception as e:
        log.error(f"Binance Fehler ({symbol}): {e}")
        return None

# ── INDIKATOREN ───────────────────────────────────────────────
def calc_ema(data, period):
    if len(data) < period:
        return [None] * len(data)
    k = 2 / (period + 1)
    r = [None] * len(data)
    r[period - 1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        r[i] = data[i] * k + r[i - 1] * (1 - k)
    return r

def calc_sma(data, period):
    r = [None] * len(data)
    for i in range(period - 1, len(data)):
        r[i] = sum(data[i - period + 1:i + 1]) / period
    return r

def calc_rsi(closes, period=14):
    r = [None] * len(closes)
    if len(closes) < period + 1:
        return r
    ag = al = 0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0: ag += d
        else: al -= d
    ag /= period; al /= period
    r[period] = 100 - 100 / (1 + (float('inf') if al == 0 else ag / al))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        r[i] = 100 - 100 / (1 + (float('inf') if al == 0 else ag / al))
    return r

def calc_macd(closes, fast=12, slow=26, sig=9):
    ef, es = calc_ema(closes, fast), calc_ema(closes, slow)
    ml = [ef[i] - es[i] if ef[i] is not None and es[i] is not None else None for i in range(len(closes))]
    valid = [v for v in ml if v is not None]
    sr = calc_ema(valid, sig)
    si = 0
    sl = []
    for v in ml:
        if v is not None:
            sl.append(sr[si] if si < len(sr) else None); si += 1
        else:
            sl.append(None)
    hist = [ml[i] - sl[i] if ml[i] is not None and sl[i] is not None else None for i in range(len(closes))]
    return {"ml": ml, "sl": sl, "hist": hist}

def calc_sr(candles, lb=5):
    sup, res = [], []
    for i in range(lb, len(candles) - lb):
        w = candles[i - lb:i + lb + 1]
        if candles[i]["low"]  <= min(c["low"]  for c in w): sup.append(candles[i]["low"])
        if candles[i]["high"] >= max(c["high"] for c in w): res.append(candles[i]["high"])
    avg = lambda lst: sum(lst[-3:]) / len(lst[-3:]) if lst else 0
    return avg(sup), avg(res)

# ── KI-ANALYSE ────────────────────────────────────────────────
def analyze(candles, risk_key="ausgewogen"):
    risk   = RISK_PROFILES.get(risk_key, RISK_PROFILES["ausgewogen"])
    W      = risk["weights"]
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    last   = lambda lst: next((v for v in reversed(lst) if v is not None), None)

    rsi_v  = calc_rsi(closes)
    macd_v = calc_macd(closes)
    ema20  = calc_ema(closes, 20)
    ema50  = calc_ema(closes, 50)
    sma200 = calc_sma(closes, min(200, len(closes)))
    support, resistance = calc_sr(candles)

    scores, expl = [], []

    # RSI
    rv = last(rsi_v)
    os_, ob = risk["rsi_oversold"], risk["rsi_overbought"]
    if rv is not None:
        if   rv < os_ - 10: s = 1.0;  expl.append(f"RSI {rv:.1f} — stark ueberverkauft ▲")
        elif rv < os_:       s = 0.7;  expl.append(f"RSI {rv:.1f} — ueberverkauft ▲")
        elif rv > ob + 10:   s = -1.0; expl.append(f"RSI {rv:.1f} — stark ueberkauft ▼")
        elif rv > ob:        s = -0.7; expl.append(f"RSI {rv:.1f} — ueberkauft ▼")
        elif rv < 48:        s = 0.2;  expl.append(f"RSI {rv:.1f} — leicht schwach")
        elif rv > 52:        s = -0.2; expl.append(f"RSI {rv:.1f} — leicht stark")
        else:                s = 0.0;  expl.append(f"RSI {rv:.1f} — neutral")
        scores.append({"name": "RSI", "score": s, "weight": W["RSI"]})

    # MACD
    mv, sv, hv = last(macd_v["ml"]), last(macd_v["sl"]), last(macd_v["hist"])
    if mv is not None and sv is not None:
        s = 0.8 if mv > sv else -0.8
        if hv is not None and abs(hv) < abs(mv) * 0.05: s *= 0.35
        expl.append(f"MACD {'ueber' if mv > sv else 'unter'} Signal — {'bullish ▲' if mv > sv else 'bearish ▼'}")
        scores.append({"name": "MACD", "score": s, "weight": W["MACD"]})

    # EMA Cross
    e20, e50 = last(ema20), last(ema50)
    if e20 is not None and e50 is not None:
        expl.append(f"EMA20 {'>' if e20 > e50 else '<'} EMA50 — {'Aufwaerts' if e20 > e50 else 'Abwaerts'}trend")
        scores.append({"name": "EMA20/50", "score": 0.7 if e20 > e50 else -0.7, "weight": W["EMA"]})

    # SMA200
    s200 = last(sma200)
    if s200 is not None:
        expl.append(f"Preis {'ueber' if price > s200 else 'unter'} SMA200 — {'bullish ▲' if price > s200 else 'bearish ▼'}")
        scores.append({"name": "SMA200", "score": 0.6 if price > s200 else -0.6, "weight": W["SMA"]})

    # S/R
    if support and resistance and resistance > support:
        pos = (price - support) / (resistance - support)
        s = 0.85 if pos < 0.2 else -0.85 if pos > 0.8 else 0.25 if pos < 0.5 else -0.25
        expl.append("Preis nahe Support ▲" if pos < 0.2 else "Preis nahe Resistance ▼" if pos > 0.8 else "Preis in S/R-Mitte")
        scores.append({"name": "S/R Zone", "score": s, "weight": W["SR"]})

    tw = sum(x["weight"] for x in scores) or 1
    ws = sum(x["score"] * x["weight"] for x in scores) / tw

    rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(max(1, len(closes)-20), len(closes))]
    vol  = (sum(r**2 for r in rets) / len(rets)) ** 0.5 * 100 if rets else 0

    thr = risk["signal_threshold"]
    if   ws >  thr: signal, conf = "KAUFEN",    min(50 + ws * 50, 95)
    elif ws < -thr: signal, conf = "VERKAUFEN", min(50 + abs(ws) * 50, 95)
    else:           signal, conf = "ABWARTEN",  max(38, 50 - abs(ws) * 70)

    return {"signal": signal, "confidence": round(conf), "score": ws,
            "scores": scores, "explanations": expl, "volatility": vol,
            "price": price, "rsi": rv, "risk": risk_key}

# ── TELEGRAM ──────────────────────────────────────────────────
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[KEIN TELEGRAM] {text[:80]}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram Fehler: {e}")
        return False

def format_message(market, res):
    sig    = res["signal"]
    emoji  = "🟢" if sig == "KAUFEN" else "🔴" if sig == "VERKAUFEN" else "🟡"
    arrow  = "▲" if sig == "KAUFEN" else "▼" if sig == "VERKAUFEN" else "◆"
    risk_l = RISK_PROFILES.get(res["risk"], {}).get("label", res["risk"])
    risk_e = "🛡️" if res["risk"] == "konservativ" else "⚖️" if res["risk"] == "ausgewogen" else "⚡"
    vol_l  = "🔴 HOCH" if res["volatility"] > 2 else "🟡 MITTEL" if res["volatility"] > 1 else "🟢 GERING"
    p      = res["price"]
    fmt_p  = f"{p:,.0f}" if p > 100 else f"{p:.5f}"

    lines = [
        f"{emoji} <b>TRADING SIGNAL — {market['name']}</b>",
        "",
        f"📊 <b>Signal:</b>     {arrow} {sig}",
        f"🎯 <b>Konfidenz:</b>  {res['confidence']}%",
        f"💰 <b>Kurs:</b>       {fmt_p}",
        f"⏱  <b>Zeitrahmen:</b> {market['interval']}",
        f"{risk_e} <b>Profil:</b>    {risk_l}",
        "",
        f"📈 <b>Indikatoren:</b>",
    ]
    for s in res["scores"]:
        d   = "▲" if s["score"] > 0 else "▼" if s["score"] < 0 else "—"
        bar = "█" * int(abs(s["score"]) * 5)
        lines.append(f"  • {s['name']:<10} {d} {bar:<5} ({s['score']:+.1f})")
    lines += [
        "",
        "💡 <b>Begruendung:</b>",
        *[f"  • {e}" for e in res["explanations"]],
        "",
        f"⚡ Volatilitaet: {res['volatility']:.2f}% — {vol_l}",
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
        "",
        "<i>Nur zu Bildungszwecken. Keine Finanzberatung.</i>",
    ]
    return "\n".join(lines)

# ── HAUPTSCHLEIFE ─────────────────────────────────────────────
def main():
    risk = RISK_PROFILES.get(RISK_PROFILE, RISK_PROFILES["ausgewogen"])
    log.info("=" * 55)
    log.info("Trading Bot gestartet")
    log.info(f"Risikoprofil:    {risk['label']}")
    log.info(f"Min. Konfidenz:  {CONFIDENCE_THRESHOLD}%")
    log.info(f"Scan-Intervall:  {SCAN_INTERVAL}s ({SCAN_INTERVAL//60} min)")
    log.info(f"Cooldown:        {COOLDOWN_MINUTES} min")
    log.info(f"Maerkte:         {[m['name'] for m in MARKETS]}")
    log.info("=" * 55)

    risk_e = "🛡️" if RISK_PROFILE == "konservativ" else "⚖️" if RISK_PROFILE == "ausgewogen" else "⚡"
    send_telegram(
        f"🤖 <b>Trading Bot gestartet!</b>\n\n"
        f"Ueberwache:\n" +
        "\n".join(f"  • {m['name']} ({m['interval']})" for m in MARKETS) +
        f"\n\n{risk_e} Risikoprofil: <b>{risk['label']}</b>\n"
        f"🎯 Mindest-Konfidenz: <b>{CONFIDENCE_THRESHOLD}%</b>\n"
        f"🔄 Scan alle: <b>{SCAN_INTERVAL // 60} Minuten</b>"
    )

    last_alert = {}

    while True:
        log.info(f"Scan — {datetime.now().strftime('%H:%M:%S')}")

        for market in MARKETS:
            sym = market["symbol"]

            # Cooldown prüfen
            if sym in last_alert:
                elapsed = (datetime.now() - last_alert[sym]).total_seconds() / 60
                if elapsed < COOLDOWN_MINUTES:
                    log.info(f"  {market['name']}: Cooldown {elapsed:.0f}/{COOLDOWN_MINUTES}min")
                    continue

            candles = fetch_candles(sym, market["interval"])
            if not candles:
                continue

            res = analyze(candles, RISK_PROFILE)
            log.info(f"  {market['name']}: {res['signal']} {res['confidence']}% (Score: {res['score']:+.2f})")

            if res["signal"] != "ABWARTEN" and res["confidence"] >= CONFIDENCE_THRESHOLD:
                log.info(f"  >>> SIGNAL! Sende Telegram-Alert")
                if send_telegram(format_message(market, res)):
                    last_alert[sym] = datetime.now()

            time.sleep(2)

        log.info(f"Scan fertig. Naechster in {SCAN_INTERVAL}s\n")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()

"""
Trading Analyzer Bot — Vollstaendige Version
============================================
- Binance Live-Daten
- KI-Analyse mit RSI, MACD, EMA, SMA, S/R, Bollinger Bands, Volumen
- Backtest mit Stop-Loss Simulation
- Telegram Commands: /status, /backtest, /help
- Taegliche Zusammenfassung
"""
import os, time, logging, requests, sqlite3, threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── KONFIGURATION ─────────────────────────────────────────────
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
CONFIDENCE_THRESHOLD  = int(os.environ.get("CONFIDENCE_THRESHOLD", "75"))
SCAN_INTERVAL         = int(os.environ.get("SCAN_INTERVAL", "300"))
COOLDOWN_MINUTES      = int(os.environ.get("COOLDOWN_MINUTES", "60"))
RISK_PROFILE          = os.environ.get("RISK_PROFILE", "ausgewogen")
EVAL_HOURS            = int(os.environ.get("EVAL_HOURS", "4"))
DAILY_SUMMARY_HOUR    = int(os.environ.get("DAILY_SUMMARY_HOUR", "20"))
STOP_LOSS_PCT         = float(os.environ.get("STOP_LOSS_PCT", "1.5"))
TAKE_PROFIT_PCT       = float(os.environ.get("TAKE_PROFIT_PCT", "3.0"))
TRADE_SIZE_USDT       = float(os.environ.get("TRADE_SIZE_USDT", "1000"))
DB_PATH               = "backtest.db"
WEBHOOK_PORT          = int(os.environ.get("PORT", "8080"))

MARKETS = [
    {"symbol": "BTCUSDT", "interval": "1h", "name": "BTC/USDT"},
    {"symbol": "ETHUSDT", "interval": "1h", "name": "ETH/USDT"},
    {"symbol": "SOLUSDT", "interval": "1h", "name": "SOL/USDT"},
    {"symbol": "BNBUSDT", "interval": "1h", "name": "BNB/USDT"},
]

RISK_PROFILES = {
    "konservativ": {
        "signal_threshold": 0.5,
        "rsi_oversold": 25, "rsi_overbought": 75,
        "weights": {"RSI":0.25,"MACD":0.18,"EMA":0.17,"SMA":0.18,"SR":0.12,"BB":0.05,"VOL":0.05},
        "label": "Konservativ",
    },
    "ausgewogen": {
        "signal_threshold": 0.3,
        "rsi_oversold": 35, "rsi_overbought": 65,
        "weights": {"RSI":0.22,"MACD":0.22,"EMA":0.17,"SMA":0.13,"SR":0.13,"BB":0.07,"VOL":0.06},
        "label": "Ausgewogen",
    },
    "aggressiv": {
        "signal_threshold": 0.15,
        "rsi_oversold": 45, "rsi_overbought": 55,
        "weights": {"RSI":0.18,"MACD":0.25,"EMA":0.22,"SMA":0.09,"SR":0.13,"BB":0.07,"VOL":0.06},
        "label": "Aggressiv",
    },
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── DATENBANK ─────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forecasts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT NOT NULL,
            signal        TEXT NOT NULL,
            confidence    INTEGER NOT NULL,
            entry_price   REAL NOT NULL,
            exit_price    REAL,
            risk_profile  TEXT,
            return_pct    REAL,
            outcome       TEXT,
            sl_hit        INTEGER DEFAULT 0,
            tp_hit        INTEGER DEFAULT 0,
            pnl_usdt      REAL,
            created_at    TEXT NOT NULL,
            eval_at       TEXT NOT NULL,
            evaluated     INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            date  TEXT UNIQUE,
            sent  INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_forecast(symbol, signal, confidence, entry_price, risk_profile):
    eval_time = (datetime.now() + timedelta(hours=EVAL_HOURS)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO forecasts
        (symbol,signal,confidence,entry_price,risk_profile,created_at,eval_at)
        VALUES (?,?,?,?,?,?,?)
    """, (symbol, signal, confidence, entry_price, risk_profile,
          datetime.now().isoformat(), eval_time))
    conn.commit()
    conn.close()

def get_pending():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id,symbol,signal,confidence,entry_price,risk_profile,created_at,eval_at
        FROM forecasts WHERE evaluated=0 AND eval_at<=?
        ORDER BY created_at ASC
    """, (datetime.now().isoformat(),)).fetchall()
    conn.close()
    return rows

def mark_evaluated(fid, exit_price, ret, outcome, sl_hit, tp_hit, pnl):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE forecasts
        SET exit_price=?,return_pct=?,outcome=?,sl_hit=?,tp_hit=?,pnl_usdt=?,evaluated=1
        WHERE id=?
    """, (exit_price, ret, outcome, sl_hit, tp_hit, pnl, fid))
    conn.commit()
    conn.close()

def get_stats(days=None):
    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT symbol,signal,confidence,return_pct,outcome,created_at,risk_profile,sl_hit,tp_hit,pnl_usdt
        FROM forecasts WHERE evaluated=1
    """
    if days:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(query + " AND created_at>=? ORDER BY created_at DESC", (since,)).fetchall()
    else:
        rows = conn.execute(query + " ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows

def was_summary_sent(date_str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT sent FROM daily_summary WHERE date=?", (date_str,)).fetchone()
    conn.close()
    return row and row[0] == 1

def mark_summary_sent(date_str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO daily_summary (date,sent) VALUES (?,1)", (date_str,))
    conn.commit()
    conn.close()

# ── BINANCE API ───────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=150):
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
            params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=10)
        r.raise_for_status()
        return [{"time":k[0],"open":float(k[1]),"high":float(k[2]),
                 "low":float(k[3]),"close":float(k[4]),"volume":float(k[5])}
                for k in r.json()]
    except Exception as e:
        log.error(f"Binance Fehler ({symbol}): {e}")
        return None

def fetch_price(symbol):
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
            params={"symbol":symbol}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.error(f"Preis-Fehler ({symbol}): {e}")
        return None

# ── INDIKATOREN ───────────────────────────────────────────────
def calc_ema(data, period):
    if len(data) < period: return [None]*len(data)
    k = 2/(period+1)
    r = [None]*len(data)
    r[period-1] = sum(data[:period])/period
    for i in range(period, len(data)):
        r[i] = data[i]*k + r[i-1]*(1-k)
    return r

def calc_sma(data, period):
    r = [None]*len(data)
    for i in range(period-1, len(data)):
        r[i] = sum(data[i-period+1:i+1])/period
    return r

def calc_rsi(closes, period=14):
    r = [None]*len(closes)
    if len(closes) < period+1: return r
    ag = al = 0
    for i in range(1, period+1):
        d = closes[i]-closes[i-1]
        if d > 0: ag += d
        else: al -= d
    ag /= period; al /= period
    r[period] = 100-100/(1+(float('inf') if al==0 else ag/al))
    for i in range(period+1, len(closes)):
        d = closes[i]-closes[i-1]
        ag = (ag*(period-1)+max(d,0))/period
        al = (al*(period-1)+max(-d,0))/period
        r[i] = 100-100/(1+(float('inf') if al==0 else ag/al))
    return r

def calc_macd(closes, fast=12, slow=26, sig=9):
    ef,es = calc_ema(closes,fast),calc_ema(closes,slow)
    ml = [ef[i]-es[i] if ef[i] is not None and es[i] is not None else None for i in range(len(closes))]
    valid = [v for v in ml if v is not None]
    sr = calc_ema(valid, sig)
    si=0; sl=[]
    for v in ml:
        if v is not None: sl.append(sr[si] if si<len(sr) else None); si+=1
        else: sl.append(None)
    hist = [ml[i]-sl[i] if ml[i] is not None and sl[i] is not None else None for i in range(len(closes))]
    return {"ml":ml,"sl":sl,"hist":hist}

def calc_bollinger(closes, period=20, std_dev=2):
    sma = calc_sma(closes, period)
    r_upper, r_lower, r_pct = [None]*len(closes), [None]*len(closes), [None]*len(closes)
    for i in range(period-1, len(closes)):
        window = closes[i-period+1:i+1]
        mean = sma[i]
        std = (sum((x-mean)**2 for x in window)/period)**0.5
        upper = mean + std_dev*std
        lower = mean - std_dev*std
        r_upper[i] = upper
        r_lower[i] = lower
        band_width = upper - lower
        if band_width > 0:
            r_pct[i] = (closes[i]-lower)/band_width  # 0=unten, 1=oben
    return {"upper":r_upper,"lower":r_lower,"pct":r_pct}

def calc_volume_signal(candles, period=20):
    vols = [c["volume"] for c in candles]
    avg_vol = calc_sma(vols, period)
    result = []
    for i in range(len(candles)):
        if avg_vol[i] is None:
            result.append(None)
            continue
        ratio = vols[i] / avg_vol[i] if avg_vol[i] > 0 else 1
        bull = candles[i]["close"] >= candles[i]["open"]
        # Hohes Volumen in Trendrichtung = starkes Signal
        if ratio > 1.5:
            result.append(0.8 if bull else -0.8)
        elif ratio > 1.2:
            result.append(0.4 if bull else -0.4)
        elif ratio < 0.6:
            result.append(0.0)  # Niedriges Volumen = schwaches Signal
        else:
            result.append(0.2 if bull else -0.2)
    return result

def calc_sr(candles, lb=5):
    sup,res = [],[]
    for i in range(lb, len(candles)-lb):
        w = candles[i-lb:i+lb+1]
        if candles[i]["low"]  <= min(c["low"]  for c in w): sup.append(candles[i]["low"])
        if candles[i]["high"] >= max(c["high"] for c in w): res.append(candles[i]["high"])
    avg = lambda lst: sum(lst[-3:])/len(lst[-3:]) if lst else 0
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
    sma200 = calc_sma(closes, min(200,len(closes)))
    bb     = calc_bollinger(closes)
    vol_s  = calc_volume_signal(candles)
    support, resistance = calc_sr(candles)

    scores, expl = [], []

    # RSI
    rv = last(rsi_v)
    os_, ob = risk["rsi_oversold"], risk["rsi_overbought"]
    if rv is not None:
        if   rv < os_-10: s=1.0;  expl.append(f"RSI {rv:.1f} — stark ueberverkauft ▲")
        elif rv < os_:    s=0.7;  expl.append(f"RSI {rv:.1f} — ueberverkauft ▲")
        elif rv > ob+10:  s=-1.0; expl.append(f"RSI {rv:.1f} — stark ueberkauft ▼")
        elif rv > ob:     s=-0.7; expl.append(f"RSI {rv:.1f} — ueberkauft ▼")
        elif rv < 48:     s=0.2;  expl.append(f"RSI {rv:.1f} — leicht schwach")
        elif rv > 52:     s=-0.2; expl.append(f"RSI {rv:.1f} — leicht stark")
        else:             s=0.0;  expl.append(f"RSI {rv:.1f} — neutral")
        scores.append({"name":"RSI","score":s,"weight":W["RSI"]})

    # MACD
    mv,sv,hv = last(macd_v["ml"]),last(macd_v["sl"]),last(macd_v["hist"])
    if mv is not None and sv is not None:
        s = 0.8 if mv>sv else -0.8
        if hv is not None and abs(hv)<abs(mv)*0.05: s*=0.35
        expl.append(f"MACD {'ueber' if mv>sv else 'unter'} Signal — {'bullish ▲' if mv>sv else 'bearish ▼'}")
        scores.append({"name":"MACD","score":s,"weight":W["MACD"]})

    # EMA Cross
    e20,e50 = last(ema20),last(ema50)
    if e20 is not None and e50 is not None:
        expl.append(f"EMA20 {'>' if e20>e50 else '<'} EMA50 — {'Aufwaerts ▲' if e20>e50 else 'Abwaerts ▼'}")
        scores.append({"name":"EMA20/50","score":0.7 if e20>e50 else -0.7,"weight":W["EMA"]})

    # SMA200
    s200 = last(sma200)
    if s200 is not None:
        expl.append(f"Preis {'ueber' if price>s200 else 'unter'} SMA200 — {'bullish ▲' if price>s200 else 'bearish ▼'}")
        scores.append({"name":"SMA200","score":0.6 if price>s200 else -0.6,"weight":W["SMA"]})

    # S/R
    if support and resistance and resistance>support:
        pos = (price-support)/(resistance-support)
        s = 0.85 if pos<0.2 else -0.85 if pos>0.8 else 0.25 if pos<0.5 else -0.25
        expl.append("Preis nahe Support ▲" if pos<0.2 else "Preis nahe Resistance ▼" if pos>0.8 else "Preis in S/R-Mitte")
        scores.append({"name":"S/R Zone","score":s,"weight":W["SR"]})

    # Bollinger Bands
    bb_pct = last(bb["pct"])
    bb_upper = last(bb["upper"])
    bb_lower = last(bb["lower"])
    if bb_pct is not None:
        if   bb_pct < 0.1: s=0.9;  expl.append(f"Preis am unteren Bollinger Band — ueberverkauft ▲")
        elif bb_pct < 0.2: s=0.5;  expl.append(f"Preis nahe unterem Bollinger Band ▲")
        elif bb_pct > 0.9: s=-0.9; expl.append(f"Preis am oberen Bollinger Band — ueberkauft ▼")
        elif bb_pct > 0.8: s=-0.5; expl.append(f"Preis nahe oberem Bollinger Band ▼")
        else:              s=0.0;  expl.append(f"Preis in Bollinger-Mitte — neutral")
        scores.append({"name":"Bollinger","score":s,"weight":W["BB"]})

    # Volumen
    vs = last(vol_s)
    if vs is not None:
        if   vs >= 0.6:  expl.append(f"Hohes Volumen — Bewegung wird bestaetigt ▲")
        elif vs <= -0.6: expl.append(f"Hohes Volumen — Abwaertsdruck bestaetigt ▼")
        else:            expl.append(f"Normales Volumen")
        scores.append({"name":"Volumen","score":vs,"weight":W["VOL"]})

    tw = sum(x["weight"] for x in scores) or 1
    ws = sum(x["score"]*x["weight"] for x in scores)/tw
    rets = [(closes[i]-closes[i-1])/closes[i-1] for i in range(max(1,len(closes)-20),len(closes))]
    vol  = (sum(r**2 for r in rets)/len(rets))**0.5*100 if rets else 0

    thr = risk["signal_threshold"]
    if   ws >  thr: signal,conf = "KAUFEN",   min(50+ws*50,95)
    elif ws < -thr: signal,conf = "VERKAUFEN",min(50+abs(ws)*50,95)
    else:           signal,conf = "ABWARTEN", max(38,50-abs(ws)*70)

    return {"signal":signal,"confidence":round(conf),"score":ws,
            "scores":scores,"explanations":expl,"volatility":vol,
            "price":price,"rsi":rv,"risk":risk_key,
            "bb_pct":bb_pct,"bb_upper":bb_upper,"bb_lower":bb_lower}

# ── STOP-LOSS / TAKE-PROFIT SIMULATION ───────────────────────
def evaluate_with_sl_tp(fid, symbol, signal, entry_price):
    """
    Holt aktuellen Preis und simuliert SL/TP.
    Berechnet auch den P&L in USDT.
    """
    exit_price = fetch_price(symbol)
    if exit_price is None:
        return None

    ret = (exit_price-entry_price)/entry_price*100

    # Stop-Loss / Take-Profit berechnen
    if signal == "KAUFEN":
        sl_price = entry_price*(1-STOP_LOSS_PCT/100)
        tp_price = entry_price*(1+TAKE_PROFIT_PCT/100)
        sl_hit   = exit_price <= sl_price
        tp_hit   = exit_price >= tp_price
        outcome  = "KORREKT" if ret>0.3 else "FALSCH" if ret<-0.3 else "NEUTRAL"
    elif signal == "VERKAUFEN":
        sl_price = entry_price*(1+STOP_LOSS_PCT/100)
        tp_price = entry_price*(1-TAKE_PROFIT_PCT/100)
        sl_hit   = exit_price >= sl_price
        tp_hit   = exit_price <= tp_price
        outcome  = "KORREKT" if ret<-0.3 else "FALSCH" if ret>0.3 else "NEUTRAL"
    else:
        sl_hit=tp_hit=False
        outcome="NEUTRAL"

    # P&L Berechnung
    if signal == "KAUFEN":
        pnl = TRADE_SIZE_USDT * (ret/100)
    elif signal == "VERKAUFEN":
        pnl = TRADE_SIZE_USDT * (-ret/100)
    else:
        pnl = 0

    # SL/TP begrenzen
    if sl_hit: pnl = -TRADE_SIZE_USDT*(STOP_LOSS_PCT/100)
    if tp_hit: pnl =  TRADE_SIZE_USDT*(TAKE_PROFIT_PCT/100)

    mark_evaluated(fid, exit_price, ret, outcome, int(sl_hit), int(tp_hit), pnl)
    log.info(f"Auswertung: {symbol} {signal} → {outcome} ({ret:+.2f}%) P&L: {pnl:+.2f} USDT")
    return {"outcome":outcome,"ret":ret,"exit_price":exit_price,
            "sl_hit":sl_hit,"tp_hit":tp_hit,"pnl":pnl}

# ── TELEGRAM ──────────────────────────────────────────────────
def send_telegram(text, chat_id=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not cid:
        log.info(f"[KEIN TELEGRAM] {text[:60]}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id":cid,"text":text,"parse_mode":"HTML"},
            timeout=10
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram Fehler: {e}")
        return False

def get_telegram_updates(offset=None):
    try:
        params = {"timeout":10}
        if offset: params["offset"] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params, timeout=15
        )
        r.raise_for_status()
        return r.json().get("result",[])
    except:
        return []

def format_signal_msg(market, res):
    sig   = res["signal"]
    emoji = "🟢" if sig=="KAUFEN" else "🔴"
    arrow = "▲" if sig=="KAUFEN" else "▼"
    risk_l= RISK_PROFILES.get(res["risk"],{}).get("label",res["risk"])
    risk_e= "🛡️" if res["risk"]=="konservativ" else "⚖️" if res["risk"]=="ausgewogen" else "⚡"
    vol_l = "🔴 HOCH" if res["volatility"]>2 else "🟡 MITTEL" if res["volatility"]>1 else "🟢 GERING"
    p     = res["price"]
    fp    = f"{p:,.0f}" if p>100 else f"{p:.5f}"
    sl_p  = p*(1-STOP_LOSS_PCT/100) if sig=="KAUFEN" else p*(1+STOP_LOSS_PCT/100)
    tp_p  = p*(1+TAKE_PROFIT_PCT/100) if sig=="KAUFEN" else p*(1-TAKE_PROFIT_PCT/100)

    lines = [
        f"{emoji} <b>TRADING SIGNAL — {market['name']}</b>",
        f"",
        f"📊 <b>Signal:</b>      {arrow} {sig}",
        f"🎯 <b>Konfidenz:</b>   {res['confidence']}%",
        f"💰 <b>Kurs:</b>        {fp}",
        f"{risk_e} <b>Profil:</b>     {risk_l}",
        f"",
        f"🛡 <b>Stop-Loss:</b>   {sl_p:,.2f} (-{STOP_LOSS_PCT}%)",
        f"🎯 <b>Take-Profit:</b> {tp_p:,.2f} (+{TAKE_PROFIT_PCT}%)",
        f"💵 <b>Sim. Einsatz:</b> {TRADE_SIZE_USDT:,.0f} USDT",
        f"",
        f"📈 <b>Indikatoren:</b>",
        *[f"  • {s['name']:<10} {'▲' if s['score']>0 else '▼' if s['score']<0 else '—'} ({s['score']:+.1f})" for s in res["scores"]],
        f"",
        f"💡 <b>Begruendung:</b>",
        *[f"  • {e}" for e in res["explanations"]],
        f"",
        f"⚡ Volatilitaet: {res['volatility']:.2f}% — {vol_l}",
        f"⏳ Auswertung in {EVAL_HOURS}h",
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"",
        f"<i>Nur zu Bildungszwecken. Keine Finanzberatung.</i>",
    ]
    return "\n".join(lines)

def format_eval_msg(symbol, signal, confidence, entry, exit_p, ret, outcome, sl_hit, tp_hit, pnl, created_at):
    emoji = "✅" if outcome=="KORREKT" else "❌" if outcome=="FALSCH" else "➖"
    arrow = "▲" if signal=="KAUFEN" else "▼"
    pnl_e = "📈" if pnl>0 else "📉"
    created = datetime.fromisoformat(created_at).strftime('%d.%m. %H:%M')
    fp_e = f"{entry:,.0f}" if entry>100 else f"{entry:.5f}"
    fp_x = f"{exit_p:,.0f}" if exit_p>100 else f"{exit_p:.5f}"
    sl_str = "⚠️ Stop-Loss ausgeloest!" if sl_hit else ""
    tp_str = "🎯 Take-Profit erreicht!" if tp_hit else ""

    lines = [
        f"{emoji} <b>BACKTEST AUSWERTUNG — {symbol}</b>",
        f"",
        f"📊 Signal:      {arrow} {signal}",
        f"🎯 Konfidenz:   {confidence}%",
        f"🏁 Ergebnis:    <b>{outcome}</b>",
        f"📊 Rendite:     <b>{ret:+.2f}%</b>",
        f"{pnl_e} Sim. P&L:   <b>{pnl:+.2f} USDT</b>",
    ]
    if sl_str: lines.append(f"⚠️ {sl_str}")
    if tp_str: lines.append(f"🎯 {tp_str}")
    lines += [
        f"",
        f"💰 Einstieg: {fp_e}  ({created})",
        f"💰 Ausstieg: {fp_x}  (jetzt)",
        f"",
        f"<i>Naechstes Signal wird automatisch erkannt.</i>",
    ]
    return "\n".join(lines)

def format_status_msg():
    lines = [f"📡 <b>AKTUELLER MARKTSTATUS</b>",
             f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",f""]
    for market in MARKETS:
        candles = fetch_candles(market["symbol"], market["interval"], limit=150)
        if not candles:
            lines.append(f"❓ {market['name']}: Keine Daten")
            continue
        res = analyze(candles, RISK_PROFILE)
        sig   = res["signal"]
        emoji = "🟢" if sig=="KAUFEN" else "🔴" if sig=="VERKAUFEN" else "🟡"
        arrow = "▲" if sig=="KAUFEN" else "▼" if sig=="VERKAUFEN" else "◆"
        p     = res["price"]
        fp    = f"{p:,.0f}" if p>100 else f"{p:.5f}"
        lines.append(f"{emoji} <b>{market['name']}</b>: {arrow} {sig} {res['confidence']}% | {fp}")
        time.sleep(1)
    lines += [f"", f"Mindest-Konfidenz: {CONFIDENCE_THRESHOLD}% | Profil: {RISK_PROFILES[RISK_PROFILE]['label']}"]
    return "\n".join(lines)

def format_backtest_msg():
    all_s  = get_stats()
    week_s = get_stats(days=7)
    day_s  = get_stats(days=1)

    def calc(rows):
        if not rows: return None
        total   = len(rows)
        correct = sum(1 for r in rows if r[4]=="KORREKT")
        wrong   = sum(1 for r in rows if r[4]=="FALSCH")
        avg_ret = sum(r[3] for r in rows if r[3] is not None)/total
        total_pnl = sum(r[9] for r in rows if r[9] is not None)
        rate    = round(correct/total*100) if total else 0
        sl_hits = sum(1 for r in rows if r[7])
        tp_hits = sum(1 for r in rows if r[8])
        return {"total":total,"correct":correct,"wrong":wrong,
                "rate":rate,"avg_ret":avg_ret,"pnl":total_pnl,
                "sl":sl_hits,"tp":tp_hits}

    lines = [f"📊 <b>BACKTEST ERGEBNISSE</b>",
             f"💵 Sim. Einsatz: {TRADE_SIZE_USDT:,.0f} USDT | SL: {STOP_LOSS_PCT}% | TP: {TAKE_PROFIT_PCT}%",f""]

    for label,rows in [("Heute",day_s),("7 Tage",week_s),("Gesamt",all_s)]:
        s = calc(rows)
        if s:
            re = "🟢" if s["rate"]>=60 else "🟡" if s["rate"]>=40 else "🔴"
            pe = "📈" if s["pnl"]>0 else "📉"
            lines += [
                f"<b>{label}:</b>",
                f"  {re} Trefferquote: <b>{s['rate']}%</b> ({s['correct']}/{s['total']})",
                f"  {pe} Sim. P&L: <b>{s['pnl']:+.2f} USDT</b>",
                f"  📊 Ø Rendite: {s['avg_ret']:+.2f}%",
                f"  🎯 Take-Profits: {s['tp']} | ⚠️ Stop-Losses: {s['sl']}",f"",
            ]
        else:
            lines += [f"<b>{label}:</b> Noch keine Daten",f""]

    if all_s:
        lines.append("<b>Letzte 5 Signale:</b>")
        for row in all_s[:5]:
            sym,sig,conf,ret,outcome = row[0],row[1],row[2],row[3],row[4]
            pnl = row[9] or 0
            created = datetime.fromisoformat(row[5]).strftime('%d.%m %H:%M')
            e = "✅" if outcome=="KORREKT" else "❌" if outcome=="FALSCH" else "➖"
            a = "▲" if sig=="KAUFEN" else "▼"
            lines.append(f"  {e} {sym} {a} {conf}% → {ret:+.2f}% ({pnl:+.0f}$) {created}")

    return "\n".join(lines)

def format_daily_summary():
    all_s  = get_stats()
    week_s = get_stats(days=7)
    day_s  = get_stats(days=1)

    def calc(rows):
        if not rows: return None
        total   = len(rows)
        correct = sum(1 for r in rows if r[4]=="KORREKT")
        wrong   = sum(1 for r in rows if r[4]=="FALSCH")
        neutral = total-correct-wrong
        avg_ret = sum(r[3] for r in rows if r[3] is not None)/total
        total_pnl = sum(r[9] for r in rows if r[9] is not None)
        rate    = round(correct/total*100) if total else 0
        return {"total":total,"correct":correct,"wrong":wrong,
                "neutral":neutral,"rate":rate,"avg_ret":avg_ret,"pnl":total_pnl}

    lines = [
        f"📊 <b>TAEGLICHE ZUSAMMENFASSUNG</b>",
        f"📅 {datetime.now().strftime('%d.%m.%Y')}",
        f"💵 Sim. Einsatz: {TRADE_SIZE_USDT:,.0f} USDT",f"",
    ]
    for label,rows in [("Heute",day_s),("7 Tage",week_s),("Gesamt",all_s)]:
        s = calc(rows)
        if s:
            re = "🟢" if s["rate"]>=60 else "🟡" if s["rate"]>=40 else "🔴"
            pe = "📈" if s["pnl"]>0 else "📉"
            lines += [
                f"<b>{label}:</b>",
                f"  {re} Trefferquote: <b>{s['rate']}%</b> ({s['correct']}/{s['total']})",
                f"  {pe} Sim. P&L: <b>{s['pnl']:+.2f} USDT</b>",
                f"  ✅ {s['correct']} Korrekt | ❌ {s['wrong']} Falsch | ➖ {s['neutral']} Neutral",f"",
            ]
        else:
            lines += [f"<b>{label}:</b> Noch keine Daten",f""]

    lines.append("<i>Bot laeuft weiter. 🤖</i>")
    return "\n".join(lines)

# ── TELEGRAM COMMAND LISTENER ─────────────────────────────────
def command_listener():
    log.info("Command Listener gestartet")
    offset = None
    while True:
        try:
            updates = get_telegram_updates(offset)
            for upd in updates:
                offset = upd["update_id"]+1
                msg = upd.get("message",{})
                text = msg.get("text","").strip().lower()
                chat_id = str(msg.get("chat",{}).get("id",""))

                if text == "/status":
                    log.info(f"Command /status von {chat_id}")
                    send_telegram("⏳ Analysiere alle Maerkte...", chat_id)
                    send_telegram(format_status_msg(), chat_id)

                elif text == "/backtest":
                    log.info(f"Command /backtest von {chat_id}")
                    send_telegram(format_backtest_msg(), chat_id)

                elif text == "/help":
                    send_telegram(
                        "🤖 <b>Trading Bot Commands:</b>\n\n"
                        "/status — Aktuelles Signal aller Maerkte\n"
                        "/backtest — Trefferquote und P&L Uebersicht\n"
                        "/help — Diese Hilfe",
                        chat_id
                    )
        except Exception as e:
            log.error(f"Command Listener Fehler: {e}")
        time.sleep(3)

# ── HEALTH CHECK SERVER (fuer Railway) ────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), HealthHandler)
        log.info(f"Health-Check Server auf Port {WEBHOOK_PORT}")
        server.serve_forever()
    except Exception as e:
        log.error(f"Health-Server Fehler: {e}")

# ── HAUPTSCHLEIFE ─────────────────────────────────────────────
def main():
    init_db()

    # Health-Check und Command-Listener in Hintergrund-Threads
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=command_listener, daemon=True).start()

    risk = RISK_PROFILES.get(RISK_PROFILE, RISK_PROFILES["ausgewogen"])
    log.info("="*55)
    log.info("Trading Bot mit Backtest + SL/TP gestartet")
    log.info(f"Risikoprofil:    {risk['label']}")
    log.info(f"Min. Konfidenz:  {CONFIDENCE_THRESHOLD}%")
    log.info(f"Stop-Loss:       {STOP_LOSS_PCT}%")
    log.info(f"Take-Profit:     {TAKE_PROFIT_PCT}%")
    log.info(f"Sim. Einsatz:    {TRADE_SIZE_USDT} USDT")
    log.info(f"Auswertung nach: {EVAL_HOURS}h")
    log.info("="*55)

    risk_e = "🛡️" if RISK_PROFILE=="konservativ" else "⚖️" if RISK_PROFILE=="ausgewogen" else "⚡"
    send_telegram(
        f"🤖 <b>Trading Bot gestartet!</b>\n\n"
        f"Ueberwache:\n"+
        "\n".join(f"  • {m['name']} ({m['interval']})" for m in MARKETS)+
        f"\n\n{risk_e} Risikoprofil: <b>{risk['label']}</b>\n"
        f"🎯 Konfidenz: <b>{CONFIDENCE_THRESHOLD}%</b>\n"
        f"🛡 Stop-Loss: <b>{STOP_LOSS_PCT}%</b>\n"
        f"🎯 Take-Profit: <b>{TAKE_PROFIT_PCT}%</b>\n"
        f"💵 Sim. Einsatz: <b>{TRADE_SIZE_USDT:,.0f} USDT</b>\n"
        f"⏳ Auswertung nach: <b>{EVAL_HOURS}h</b>\n\n"
        f"Commands: /status | /backtest | /help"
    )

    last_alert = {}

    while True:
        now = datetime.now()

        # Pending auswerten
        for row in get_pending():
            fid,symbol,signal,conf,entry,risk_p,created_at,eval_at = row
            result = evaluate_with_sl_tp(fid, symbol, signal, entry)
            if result:
                msg = format_eval_msg(symbol, signal, conf, entry,
                    result["exit_price"], result["ret"], result["outcome"],
                    result["sl_hit"], result["tp_hit"], result["pnl"], created_at)
                send_telegram(msg)
            time.sleep(2)

        # Taegliche Zusammenfassung
        if (now.hour==DAILY_SUMMARY_HOUR and now.minute<6 and
                not was_summary_sent(now.strftime("%Y-%m-%d"))):
            if send_telegram(format_daily_summary()):
                mark_summary_sent(now.strftime("%Y-%m-%d"))

        # Neue Signale scannen
        log.info(f"Scan — {now.strftime('%H:%M:%S')}")
        for market in MARKETS:
            sym = market["symbol"]
            if sym in last_alert:
                elapsed = (now-last_alert[sym]).total_seconds()/60
                if elapsed < COOLDOWN_MINUTES:
                    log.info(f"  {market['name']}: Cooldown {elapsed:.0f}/{COOLDOWN_MINUTES}min")
                    continue

            candles = fetch_candles(sym, market["interval"])
            if not candles: continue

            res = analyze(candles, RISK_PROFILE)
            log.info(f"  {market['name']}: {res['signal']} {res['confidence']}% (Score: {res['score']:+.2f})")

            if res["signal"]!="ABWARTEN" and res["confidence"]>=CONFIDENCE_THRESHOLD:
                log.info(f"  >>> SIGNAL! Speichere & sende...")
                save_forecast(sym, res["signal"], res["confidence"], res["price"], RISK_PROFILE)
                send_telegram(format_signal_msg(market, res))
                last_alert[sym] = now

            time.sleep(2)

        log.info(f"Scan fertig. Naechster in {SCAN_INTERVAL}s\n")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()

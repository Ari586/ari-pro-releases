"""Trading complet de l'app Mac, repris d'ARI Pro : choix de l'actif, écran Marchés, graphique en bougies,
journal, analyse complète, « lancer maintenant » et pont vers le courtier.

Tout est calculé par le cœur (agentic_ari) ; ici on ne fait qu'assembler les réponses pour l'app. Le pont
courtier (agentic_ari.bridge) part en mode simulation ; un ordre réel n'est envoyé que si vous avez réglé un
courtier réel, et l'app demande toujours votre confirmation avant chaque ordre.
"""
from agentic_ari.config import Config

# actifs favoris (mêmes que dans ARI Pro) : (nom affiché, code des données)
SYMBOLS = [("XAUUSD", "GC=F"), ("BTC", "BTC-USD"), ("ETH", "ETH-USD"), ("EURUSD", "EURUSD=X"),
           ("GBPUSD", "GBPUSD=X"), ("NASDAQ", "^NDX"), ("S&P 500", "^GSPC"), ("Pétrole", "CL=F")]


def num(x, what):
    """Nombre reçu de l'app (« 4100 », « 4 100,5 ») ; sinon ValueError lisible."""
    try:
        v = float(str(x).replace(",", ".").replace(" ", "").replace(" ", "")) if isinstance(x, str) else float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{what} : nombre attendu") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{what} : nombre attendu")
    return v


def resolve(symbol):
    """Nom ou code reçu de l'app → code de données (« XAUUSD » → GC=F, « bitcoin » → BTC-USD)."""
    from agentic_ari import markets
    symbol = str(symbol or "").strip()[:40]
    if not symbol:
        return "GC=F"
    for name, code in SYMBOLS + markets.flat():
        if symbol == code or symbol.lower() == name.lower():
            return code
    if any(ch in symbol for ch in "=^-.") or symbol.isupper():
        return symbol   # code de données déjà donné (résultat de la recherche)
    from agentic_ari.watch import find_symbol, symbol_of
    return find_symbol(symbol) or symbol_of(symbol)


def symbols():
    from agentic_ari import markets
    return {**markets.catalog(), "favorites": [{"name": n, "code": c} for n, c in SYMBOLS]}


def search(q):
    from agentic_ari import markets
    return {"items": markets.search(str(q or "")[:60])}


def candles(symbol, tf="15m", limit=150):
    from . import markets_overview as mo
    if tf not in mo.CANDLE_TFS:
        raise ValueError(f"unité de temps inconnue : {tf} ({', '.join(mo.CANDLE_TFS)})")
    return mo.series(resolve(symbol), tf, int(limit or 150))


def overview(watches, lang="fr", symbols_csv=""):
    """Signaux des actifs suivis (ou demandés), bande des indices et actualités."""
    from . import markets_overview as mo
    watched = []
    for w in watches or []:
        code = resolve(w.get("symbol")) if w.get("symbol") else None
        if w.get("active") and code and code not in watched:
            watched.append(code)
    asked = [c.strip() for c in str(symbols_csv or "").split(",") if c.strip()][:mo.MAX_SYMBOLS]
    if asked:
        codes = []
        for c in asked:
            c = resolve(c)
            if c not in codes:
                codes.append(c)
    else:
        codes = list(watched)
        for _, c in SYMBOLS:
            if len(codes) >= 6:
                break
            if c not in codes:
                codes.append(c)
        codes = codes[:6]
    data = mo.collect(codes, str(lang or "fr")[:2])
    data["watched"] = watched
    return data


# ---------- journal ----------
def _journal():
    from agentic_ari.journal import Journal
    return Journal(Config())


def journal():
    j = _journal()
    trades = j.trades(closed_only=False)
    return {"stats": j.stats(), "today": j.today(), "trades": list(reversed(trades))[:200]}


def journal_open(b):
    side = "achat" if str(b.get("side", "")).lower() in ("achat", "buy", "long") else "vente"

    def f(x, what):
        return None if x in (None, "") else num(x, what)
    entry = f(b.get("entry"), "prix d'entrée")
    if entry is None:
        raise ValueError("Prix d'entrée requis")
    tid = _journal().open(resolve(b.get("symbol")), side, entry, f(b.get("stop"), "stop"), f(b.get("target"), "objectif"),
                          f(b.get("lots"), "lots") or 1.0, str(b.get("note") or "")[:300])
    return {"id": int(tid)}


def journal_close(b):
    tid = int(b.get("id"))
    exit_price = num(b.get("exit"), "prix de sortie")
    j = _journal()
    if not j.db.execute("SELECT 1 FROM trades WHERE id=?", (tid,)).fetchone():
        raise LookupError("trade introuvable")
    j.close(exit_price, tid)
    return {"ok": True}


# ---------- analyse ----------
def analyse(text):
    from agentic_ari.watch import trade_answer
    out = trade_answer(Config(), str(text or "")[:500])
    if not out:
        raise ValueError("Demande non reconnue (ex. « analyse et entrée sur l'or en M15 »)")
    return {"text": out}


def run_watches():
    """« Lancer maintenant » : analyse tout de suite chaque surveillance active, sans attendre son tour."""
    from agentic_ari.watch import Watches, mac_alert
    w = Watches(Config())
    ids = [r[0] for r in w.db.execute("SELECT id FROM watches WHERE active=1")]
    new = []
    for wid in ids:
        new += list(w.check(wid, notify=mac_alert) or [])
    return {"checked": len(ids), "new": len(new), "messages": new[:10]}


# ---------- pont courtier ----------
def _bm():
    from agentic_ari.bridge import BridgeManager
    return BridgeManager(Config())


def bridge_status():
    bm = _bm()
    return {"config": bm.get_config(), "account": bm.get_account_info()}


def bridge_config(data):
    return _bm().save_config(data)


def bridge_test():
    return _bm().get_account_info()


def bridge_positions():
    return {"items": _bm().get_positions()}


def bridge_order(body):
    def f(k, what):
        return None if body.get(k) in (None, "") else num(body.get(k), what)
    return _bm().execute_order(symbol=resolve(body.get("symbol") or body.get("code")), side=str(body.get("side") or "buy"),
                               entry=f("entry", "entrée"), sl=f("sl", "stop"), tp=f("tp", "objectif"), lots=f("lots", "lots"),
                               comment=str(body.get("comment") or "Agentic Ari")[:60])


def bridge_close(body):
    ticket = str(body.get("ticket") or "")
    if not ticket:
        raise ValueError("Numéro de ticket requis")
    return _bm().close_position(ticket)

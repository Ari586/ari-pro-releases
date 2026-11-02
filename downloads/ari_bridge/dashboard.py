"""Tableau de bord trading de l'app : tout est calculé par le cœur (agentic_ari), ici on ne fait qu'assembler.

- chaque section est indépendante : si l'une échoue, les autres s'affichent quand même (« error » dans la section)
- cache de 30 s (les bougies du cœur ont déjà leur propre cache de 45 s)
- analyse seulement : aucun ordre n'est jamais passé
"""
import threading
import time

from agentic_ari.config import Config

SYMBOL = "GC=F"
TTL = 30
_lock = threading.Lock()
_cache = {}


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": f"{type(e).__name__} : {e}"}


def _dist(sym, entry, stop):
    """Distance du stop selon le cœur (« à 16,00 $, 160 pips, ≈ 160 $ de risque pour 0,1 lot »), virgule décimale."""
    if entry is None or stop is None:
        return None
    try:
        import re
        from agentic_ari.watch import dist_txt
        return re.sub(r"(\d)\.(\d)", r"\1,\2", dist_txt(sym, float(entry), float(stop)))
    except Exception:
        return None


def _tps(sym, entry, tps):
    """TP1/TP2/TP3 du cœur : prix, R, et distance depuis l'entrée ($ et pips ; or : 1 pip = 0,10 $)."""
    out = []
    for t in tps or []:
        try:
            d = abs(float(t["price"]) - float(entry))
        except (TypeError, ValueError, KeyError):
            continue
        out.append({"name": t.get("name"), "price": _r(t["price"]), "r": _r(t.get("r"), 2), "dist": _r(d), "why": t.get("why"),
                    "pips": round(d * 10) if sym == "GC=F" else None})
    return out


def _r(x, n=None):
    """Arrondi selon l'ordre de grandeur (forex 1,08534 ; action 188,14 ; or 4153,16)."""
    if x is None:
        return None
    x = float(x)
    if n is None:
        a = abs(x)
        n = 5 if a < 10 else 4 if a < 100 else 2
    return round(x, n)


# ---------- prix ----------
def _price(sym):
    from agentic_ari import watch
    rows = watch.candles(sym, "15m")
    last = rows[-1]
    day = [r for r in rows if r[0] >= last[0] - 24 * 3600]
    prev = [r for r in rows if r[0] <= last[0] - 24 * 3600]
    ref = prev[-1][4] if prev else day[0][1]
    try:
        from agentic_ari.tvfeed import source
        src = source(sym)
    except Exception:
        src = "Yahoo Finance"
    try:
        from agentic_ari.predict import current_session
        session = current_session()
    except Exception:
        session = None
    spark = [_r(r[4]) for r in rows[-96:]]
    return {"price": _r(last[4]), "time": last[0], "high": _r(max(r[2] for r in day)), "low": _r(min(r[3] for r in day)),
            "change": _r(last[4] - ref), "change_pct": _r((last[4] - ref) / ref * 100), "source": src,
            "session": session, "spark": spark}


# ---------- biais ----------
def _bias(sym):
    from agentic_ari import watch
    rows = watch.candles(sym, "1h")
    try:
        s = watch.trade_setup(rows)
        return {"verdict": s["verdict"], "price": _r(s["price"]), "stop": _r(s["sl"]), "target": _r(s["tp"]),
                "dist": _dist(sym, s["price"], s["sl"]) if s.get("sl") else None,
                "atr": _r(s["atr"]), "reasons": s["reasons"], "from": "analyse du cœur (H1)"}
    except Exception as e:
        core_err = f"{type(e).__name__} : {e}"
    # repli tant que l'analyse du cœur échoue : mêmes briques (MM20/50, RSI, Ichimoku, ADX), mêmes seuils
    from agentic_ari import indicators as ind
    closes = [r[4] for r in rows]
    price, s20, s50, rsi = closes[-1], watch.sma(closes, 20), watch.sma(closes, 50), watch.rsi(closes)
    a = watch.atr(rows)
    score, reasons = 0, []
    if s20 > s50 and price > s20:
        score += 1
        reasons.append("Tendance haussière (MM20 > MM50)")
    elif s20 < s50 and price < s20:
        score -= 1
        reasons.append("Tendance baissière (MM20 < MM50)")
    rv = ind.candles_v(sym, "1h")
    ichi = ind.ichimoku(rv) or {}
    pos = ichi.get("pos", "") if isinstance(ichi, dict) else str(ichi)
    if "au-dessus" in pos or "haussi" in pos:
        score += 1
        reasons.append("Prix au-dessus du nuage Ichimoku")
    elif "sous" in pos or "baissi" in pos:
        score -= 1
        reasons.append("Prix sous le nuage Ichimoku")
    d = sum(ind.delta(b) for b in rv[-20:])
    score += 1 if d > 0 else -1 if d < 0 else 0
    adx = ind.adx(rv)
    if adx is not None and adx < 20:
        reasons.append(f"Pas de tendance nette (ADX {adx:.0f})")
    if score >= 2 and 40 <= rsi < 75:
        v, sl, tp = "ACHAT", price - 1.5 * a, price + 3 * a
    elif score <= -2 and 25 < rsi <= 60:
        v, sl, tp = "VENTE", price + 1.5 * a, price - 3 * a
    else:
        v, sl, tp = "ATTENDRE", None, None
    if not reasons:
        reasons.append("Signaux mitigés")
    return {"verdict": v, "price": _r(price), "stop": _r(sl), "target": _r(tp), "atr": _r(a), "rsi": _r(rsi, 0),
            "reasons": reasons, "from": "H1 (calcul de secours)", "core_error": core_err}


# ---------- zones d'entrée (même calcul que l'analyse et les alertes : H4, watch.entry_zones) ----------
def _zones(sym):
    from agentic_ari import watch
    rows = watch.candles(sym, "4h")
    st = watch.trade_setup(rows)
    z = watch.entry_zones(rows, st)

    def side(lo_hi, sl, tp, kind, tps):
        mid = sum(lo_hi) / 2
        risk = abs(mid - sl)
        return {"low": _r(lo_hi[0]), "high": _r(lo_hi[1]), "stop": _r(sl), "target": _r(tp), "kind": kind,
                "dist": _dist(sym, mid, sl), "tps": _tps(sym, mid, tps),
                "rr": _r(abs(tp - mid) / risk, 1) if risk else None}
    price = st["price"]
    buy = side(z["buy"], z["buy_sl"], z["buy_tp"], z["buy_kind"], z.get("buy_tps"))
    sell = side(z["sell"], z["sell_sl"], z["sell_tp"], z["sell_kind"], z.get("sell_tps"))
    for d in (buy, sell):
        d["inside"] = d["low"] <= price <= d["high"]
        d["distance"] = _r(0 if d["inside"] else min(abs(price - d["low"]), abs(price - d["high"])))
    return {"tf": "H4", "verdict": st["verdict"], "price": _r(price), "atr": _r(st["atr"]), "buy": buy, "sell": sell,
            "nearest": z["nearest"], "brk_up": _r(z["brk_up"]), "brk_dn": _r(z["brk_dn"]), "reasons": st.get("reasons", [])}


# ---------- entrées par style (watch.styles du cœur) ----------
def _styles(sym):
    import re
    from agentic_ari import watch
    out = []
    for st in watch.styles(symbol=sym):
        if st.get("error"):
            out.append({"style": st.get("style"), "error": st["error"]})
            continue
        z = st.get("zone") or [None, None]
        mid = sum(z) / 2 if None not in z else st.get("price")
        out.append({"style": st["style"], "htf": st.get("htf"), "ltfs": st.get("ltfs", []), "note": st.get("note"),
                    "verdict": st.get("verdict"), "side": st.get("side"), "zone": [_r(z[0]), _r(z[1])],
                    "stop": _r(st.get("stop")), "target": _r(st.get("target")), "rr": st.get("rr"),
                    "confirmation": re.sub(r"(\d)\.(\d)", r"\1,\2", st.get("confirmation") or ""),
                    "dist": _dist(sym, mid, st.get("stop")), "tps": _tps(sym, mid, st.get("tps"))})
    return out


# ---------- indicateurs ----------
def _indicators(sym):
    from agentic_ari import indicators as ind
    rv = ind.candles_v(sym, "1h")
    r15 = ind.candles_v(sym, "15m")
    ichi = ind.ichimoku(rv)
    d20 = sum(ind.delta(b) for b in r15[-20:])
    vol20 = sum(b[5] for b in r15[-20:]) or 1
    events = [e[1] if isinstance(e, (list, tuple)) else str(e) for e in (ind.order_flow(r15) or [])]
    out = {"adx": _r(ind.adx(rv), 0), "ichimoku": ichi if isinstance(ichi, dict) else {"pos": str(ichi)},
           "flow": {"delta_pct": _r(d20 / vol20 * 100, 0),
                    "side": "acheteurs" if d20 > 0 else "vendeurs" if d20 < 0 else "équilibre", "events": events[-3:]},
           "divergence": ind.divergence(rv), "sweep": ind.sweep_reclaim(rv)}
    try:
        from agentic_ari.predict import expected_range
        er = expected_range(sym)
        out["range24"] = {"low": _r(er["low"]), "high": _r(er["high"]), "coverage": er.get("coverage")}
    except Exception:
        pass
    return out


# ---------- stratégies (API du cœur : strategy_search.catalog / status / set_active / activate_all) ----------
def _strategies(sym):
    from agentic_ari import strategy_search as ss
    live = {}
    for st in ss.status(sym) or []:
        if st.get("id"):
            live[st["id"]] = st
    partial = _partial(sym)
    out = []
    for c in ss.catalog(sym):
        item = {k: c.get(k) for k in ("id", "rule", "name", "tf", "sl", "rr", "note", "n", "eligible", "why_not", "label",
                                         "variant", "verdict")}
        item["recommended"] = bool(c.get("recommended", c.get("eligible")))
        item.update(active=bool(c.get("active")), exp=_r(c.get("exp")), win=_r(c.get("win")), pf=_r(c.get("pf")),
                    dd=_r(c.get("dd"), 1))
        L = live.get(c.get("id"))
        if item["active"] and L:
            if L.get("error"):
                item["live"] = {"error": L["error"]}
            elif L.get("signal"):
                lo, hi = sorted(L["entry"])
                item["live"] = {"side": L.get("side"), "entry": [_r(lo), _r(hi)], "stop": _r(L.get("stop")),
                                "target": _r(L.get("target")), "dist": _dist(sym, (lo + hi) / 2, L.get("stop")),
                                "tps": _tps(sym, (lo + hi) / 2, L.get("tps"))}
            else:
                b, v = L.get("buy_plan"), L.get("sell_plan")
                item["live"] = {"side": None, "buy": [_r(b[0]), _r(b[2]), _r(b[3])], "sell": [_r(v[0]), _r(v[2]), _r(v[3])],
                                "buy_dist": _dist(sym, b[0], b[2]), "sell_dist": _dist(sym, v[0], v[2]),
                                "buy_tps": _tps(sym, b[0], L.get("buy_plan_tps")), "sell_tps": _tps(sym, v[0], L.get("sell_plan_tps"))}
        if item["active"] and item["id"] in partial:
            item["partial"] = partial[item["id"]]
        out.append(item)
    out.sort(key=lambda i: (-i["active"], -i["recommended"], -(i.get("note") or 0)))
    return out


_GANN = {}


def _gann(sym):
    """Méthode Gann testée avec ses propres stops et objectifs (cœur : gann_native), cache 10 min."""
    hit = _GANN.get(sym)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    from agentic_ari import strategy_search as ss
    out = []
    for g in ss.gann_native(sym) or []:
        i, o = g.get("in") or {}, g.get("out") or {}
        out.append({"tf": g.get("tf"), "target": g.get("target"), "n": o.get("n"), "win": _r(o.get("win")),
                    "exp": _r(o.get("exp")), "pf": _r(o.get("pf")), "in_exp": _r(i.get("exp")), "in_n": i.get("n")})
    _GANN[sym] = (time.time(), out)
    return out


_PARTIAL = {}


def _partial(sym):
    """Sortie en tiers (TP1/TP2/TP3) comparée à l'objectif unique, pour chaque stratégie active (cœur : partial_compare)."""
    hit = _PARTIAL.get(sym)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    res = {}
    try:
        from agentic_ari import strategy_search as ss
        for x in ss.partial_compare(sym) or []:
            a, b, c = x.get("single") or {}, x.get("partial") or {}, x.get("strategic") or {}
            if a.get("n") and b.get("n"):
                res[x["id"]] = {"single_exp": _r(a.get("exp")), "partial_exp": _r(b.get("exp")),
                                "single_win": _r(a.get("win")), "partial_win": _r(b.get("win")),
                                "strategic_exp": _r(c.get("exp")) if c.get("n") else None,
                                "strategic_win": _r(c.get("win")) if c.get("n") else None}
    except Exception:
        pass
    _PARTIAL[sym] = (time.time(), res)
    return res


def can_toggle():
    from agentic_ari import strategy_search as ss
    return callable(getattr(ss, "set_active", None))


def set_strategy(sid, on, sym=SYMBOL):
    from agentic_ari import strategy_search as ss
    ss.set_active(sym, sid, bool(on))   # ValueError si l'identifiant est inconnu
    invalidate()


def activate_all(sym=SYMBOL, on=True):
    """« Activer les recommandées » (cœur : activate_all) ou « tout désactiver » (chaque active, une par une)."""
    from agentic_ari import strategy_search as ss
    if on:
        act, _ = ss.activate_all(sym)
        res = {"on": len(act or [])}
    else:
        ids = [a["id"] for a in ss.active() if a.get("symbol") == sym]
        for i in ids:
            ss.set_active(sym, i, False)
        res = {"off": len(ids)}
    invalidate()
    return res


# ---------- gestion du risque (règles de vos robots) ----------
def _risk():
    from agentic_ari import watch
    r = dict(getattr(watch, "RISK_RULES", {}) or {})
    rp = r.get("risk_pct")
    warn = list(watch.session_warnings() or []) if callable(getattr(watch, "session_warnings", None)) else []
    return {"risk_pct": list(rp) if isinstance(rp, (list, tuple)) else rp, "be_at": r.get("be_at"), "trail_atr": r.get("trail_atr"),
            "max_trades_day": r.get("max_trades_day"), "max_loss_day_pct": r.get("max_loss_day_pct"), "warnings": warn}


# ---------- précision des signaux envoyés ----------
def _accuracy(sym):
    from agentic_ari import strategy_search as ss
    a = ss.signal_accuracy(sym) or {}
    rows = [{"created": x.get("created"), "side": x.get("side"), "strategies": x.get("strategies"), "entry": _r(x.get("entry")),
             "stop": _r(x.get("stop")), "result": x.get("result")} for x in (a.get("rows") or [])[:8]]
    return {k: a.get(k) for k in ("n", "tp1", "tp2", "tp3", "stop", "pending", "tp1_rate")} | {"rows": rows}


# ---------- Freqtrade (crypto) : lecture seule ----------
def _freqtrade():
    from agentic_ari import freqtrade_link as fl
    return fl.status()


# ---------- OctoBot (crypto) : lecture seule ----------
def _octobot():
    """État du vrai OctoBot local (conteneur « octobot-ari-simulation ») : lecture seule, aucune valeur inventée."""
    from agentic_ari import octobot_link as ol
    st = ol.status() or {}
    keys = ("container", "api_reachable", "configured", "app_url", "simulated", "profile", "exchanges", "starting_portfolio",
            "exchange", "pairs", "mode", "simulated_orders", "real_trader_enabled", "has_exchange_keys")
    return {k: st.get(k) for k in keys}


# ---------- Kraken (crypto CCXT) : lecture seule ----------
def _kraken():
    from agentic_ari import crypto_data as cd
    return cd.kraken_account()



# ---------- checklist 7 blocs (idée AriAlgo) et tableau multi-unités ----------
def _blocks(sym, side):
    """7 vérifications pass/fail pour le sens donné (+1 achat, -1 vente), sur la dernière bougie H1 clôturée."""
    from agentic_ari import indicators as ind
    from agentic_ari import watch
    rv = ind.candles_v(sym, "1h")[:-1]
    c = [r[4] for r in rv]
    s20, s50, s200 = watch.sma(c, 20), watch.sma(c, 50), watch.sma(c, 200)
    r14 = watch.rsi(c)
    a14, a50 = watch.atr([r[:5] for r in rv[-15:]]), watch.atr([r[:5] for r in rv[-51:]], 50)
    vol, vavg = rv[-1][5], sum(r[5] for r in rv[-21:-1]) / 20
    hh = max(r[2] for r in rv[-10:]) > max(r[2] for r in rv[-20:-10])
    hl = min(r[3] for r in rv[-10:]) > min(r[3] for r in rv[-20:-10])
    lh = max(r[2] for r in rv[-10:]) < max(r[2] for r in rv[-20:-10])
    ll = min(r[3] for r in rv[-10:]) < min(r[3] for r in rv[-20:-10])
    _, o, h, l, cl, _v = rv[-1]
    po, pc = rv[-2][1], rv[-2][4]
    rng = (h - l) or 1e-9
    up = side > 0
    engulf = (cl > o and pc < po and cl >= po and o <= pc) if up else (cl < o and pc > po and cl <= po and o >= pc)
    pin = ((min(o, cl) - l) / rng > 0.6) if up else ((h - max(o, cl)) / rng > 0.6)
    ratio = (a14 / a50) if a14 and a50 else None
    return [
        {"name": "Tendance", "ok": bool(s200 and ((s20 > s50 and cl > s200) if up else (s20 < s50 and cl < s200))),
         "detail": f"MM20 {'>' if s20 > s50 else '<'} MM50, prix {'>' if s200 and cl > s200 else '<'} MM200"},
        {"name": "Momentum", "ok": bool(r14 and ((50 < r14 < 75) if up else (25 < r14 < 50))), "detail": f"RSI {r14:.0f}" if r14 else "—"},
        {"name": "Volume", "ok": vol > vavg, "detail": f"{vol / vavg:.1f}× la moyenne" if vavg else "—"},
        {"name": "Volatilité", "ok": bool(ratio and 0.7 <= ratio <= 1.8), "detail": f"ATR {ratio:.2f}× la normale" if ratio else "—"},
        {"name": "Structure", "ok": (hh and hl) if up else (lh and ll),
         "detail": ("sommets et creux montants" if hh and hl else "sommets et creux descendants" if lh and ll else "pas de structure nette")},
        {"name": "Confirmation", "ok": (cl > o and cl > rv[-2][2]) if up else (cl < o and cl < rv[-2][3]),
         "detail": "clôture au-delà de la bougie précédente" if ((cl > rv[-2][2]) if up else (cl < rv[-2][3])) else "pas encore"},
        {"name": "Figures", "ok": engulf or pin, "detail": "englobante" if engulf else "pin bar" if pin else "aucune"},
    ]


def _checklist(sym):
    from agentic_ari import watch
    st = watch.trade_setup(watch.candles(sym, "1h"))
    side = 1 if st["verdict"] == "ACHAT" else -1 if st["verdict"] == "VENTE" else None
    if side is None:   # pas de biais : on regarde les deux sens
        b, v = _blocks(sym, 1), _blocks(sym, -1)
        side, blocks = (1, b) if sum(x["ok"] for x in b) >= sum(x["ok"] for x in v) else (-1, v)
    else:
        blocks = _blocks(sym, side)
    return {"side": "achat" if side > 0 else "vente", "verdict": st["verdict"], "blocks": blocks,
            "score": sum(1 for x in blocks if x["ok"])}


def _mtf(sym):
    from agentic_ari import indicators as ind
    from agentic_ari import watch
    out = []
    for tf, name in (("15m", "M15"), ("1h", "H1"), ("4h", "H4"), ("1d", "D1")):
        try:
            rows = watch.candles(sym, tf)
            c = [r[4] for r in rows]
            st = watch.trade_setup(rows)
            out.append({"tf": name, "trend": "haussière" if watch.sma(c, 20) > watch.sma(c, 50) else "baissière",
                        "rsi": _r(watch.rsi(c), 0), "adx": _r(ind.adx(rows), 0), "verdict": st["verdict"]})
        except Exception as e:
            out.append({"tf": name, "error": f"{type(e).__name__}"})
    return out


# ---------- Binance (lecture seule, clé dans le Trousseau) ----------
_BINANCE = {}


def _binance():
    from agentic_ari import binance_link as bl
    if not bl.connected():
        return {"connected": False}
    hit = _BINANCE.get("acc")
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    try:
        acc = bl.account() or {}
    except Exception as e:   # clé enregistrée mais refusée (IP, droits…) : on le dit clairement, sans planter le volet
        msg = str(e)
        hint = ("Binance refuse cette clé (401) : elle est peut-être restreinte à une autre adresse IP, ou « Enable Reading » "
                "n'est pas coché. Remplacez-la par une clé en lecture seule.") if "401" in msg or "-2015" in msg else msg
        out = {"connected": True, "key_error": hint}
        _BINANCE["acc"] = (time.time(), out)
        return out
    out = {"connected": True, "total_usdt": _r(acc.get("total_usdt")), "open_orders": acc.get("open_orders"),
           "can_trade": bool(acc.get("can_trade")), "can_withdraw": bool(acc.get("can_withdraw")),
           "balances": [{"asset": b.get("asset"), "qty": b.get("qty"), "usdt": _r(b.get("usdt"))}
                        for b in (acc.get("balances") or [])][:12],
           "trades": [{k: t.get(k) for k in ("symbol", "side", "price", "amount", "date")} for t in (acc.get("trades") or [])][:6]}
    _BINANCE["acc"] = (time.time(), out)
    return out


def invalidate_kraken():
    k = _safe(_kraken)
    with _lock:
        for sym, data in list(_cache.items()):
            _cache[sym] = dict(data, kraken=k)


def invalidate_binance():
    _BINANCE.clear()
    b = _safe(_binance)   # hors du verrou : appel réseau
    with _lock:
        for sym, data in list(_cache.items()):
            _cache[sym] = dict(data, binance=b)


# ---------- calendrier / journal ----------
def _calendar(sym):
    from agentic_ari import market_context as mc
    c = mc.get(sym)
    corr = [{"label": v.get("label"), "up": v.get("up"), "change4h": v.get("change4h"), "sign": v.get("sign")}
            for v in (c.get("corr") or {}).values()]
    return {"events": (c.get("calendar") or [])[:4], "corr": corr, "news": (c.get("news") or [])[:3]}


def _journal():
    from agentic_ari.journal import Journal
    j = Journal(Config())
    s = j.stats()
    open_ = [x for x in j.trades(closed_only=False) if x.get("exit") is None]
    out = {"open": len(open_)}
    if s:
        out.update(n=s["n"], pnl=_r(s["pnl"]), win=_r(s["win"]), avg_r=_r(s["avg_r"]), dd=_r(s["dd"]),
                   worst_streak=s["worst_streak"], no_stop=s["no_stop"])
    return out


def build(sym=SYMBOL, alerts=None):
    from agentic_ari.spot import display
    return {"symbol": sym, "name": display(sym) or sym, "at": time.time(), "can_toggle": _safe(can_toggle) is True,
            "price": _safe(lambda: _price(sym)), "bias": _safe(lambda: _bias(sym)), "zones": _safe(lambda: _zones(sym)), "styles": _safe(lambda: _styles(sym)), "gann": _safe(lambda: _gann(sym)),
            "risk": _safe(_risk), "accuracy": _safe(lambda: _accuracy(sym)), "freqtrade": _safe(_freqtrade),
            "kraken": _safe(_kraken),
            "octobot": _safe(_octobot), "checklist": _safe(lambda: _checklist(sym)), "mtf": _safe(lambda: _mtf(sym)), "binance": _safe(_binance),
            "indicators": _safe(lambda: _indicators(sym)), "strategies": _safe(lambda: _strategies(sym)),
            "calendar": _safe(lambda: _calendar(sym)), "journal": _safe(_journal),
            "alerts": _safe(lambda: alerts() if alerts else [])}


_refreshing = set()


def _refresh(sym, alerts):
    try:
        data = build(sym, alerts)
        with _lock:
            _cache[sym] = data
    finally:
        _refreshing.discard(sym)


def get(sym=SYMBOL, alerts=None, fresh=False):
    """Réponse immédiate avec la dernière version ; si elle a vieilli, recalcul en arrière-plan (~15 s, tout le tableau)."""
    with _lock:
        hit = _cache.get(sym)
    if hit and not fresh:
        if time.time() - hit["at"] >= TTL and sym not in _refreshing:
            _refreshing.add(sym)
            threading.Thread(target=_refresh, args=(sym, alerts), daemon=True).start()
        return hit
    data = build(sym, alerts)   # première fois, ou « actualiser » : on attend
    with _lock:
        _cache[sym] = data
    return data


def invalidate():
    """Après un interrupteur : seules les stratégies sont recalculées (rapide), le reste du tableau reste en cache."""
    with _lock:
        cached = dict(_cache)
    for sym, data in cached.items():
        data = dict(data, strategies=_safe(lambda: _strategies(sym)))
        with _lock:
            _cache[sym] = data

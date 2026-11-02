"""Écran « Marchés » de l'app Mac (repris d'ARI Pro) : signaux des actifs suivis et actualités du monde, en lecture seule.

Les signaux reprennent les mêmes calculs que le tableau de bord du Trading (biais H1, zones H4), en version
légère (sans backtests) et mis en cache quelques minutes ; le taux de réussite n'est affiché que s'il vient d'un
vrai backtest déjà calculé. Les actualités viennent des rubriques de Google News (RSS public), dans la langue
de l'utilisateur. Rien n'est inventé : une donnée absente est simplement omise.

Pour les graphiques de l'écran, chaque signal porte aussi ses bougies 15 min des 24 dernières heures
(`m15`, [t, o, h, l, c]), ses bougies H1 des 5 derniers jours (`h1`), le plus haut / bas des 24 h, le RSI(14)
H1, la tendance M15/H1/H4 (`mtf`) et la distance du prix à la zone d'entrée. La bande `tickers` résume les
indices et marchés principaux (prix, variation, mini-courbe). Trois caches : cotation 25 s, indicateurs 2 min,
analyse 5 min — l'écran se rafraîchit toutes les 30 s (12 s si la réponse est partielle).

Le grand graphique charge à la demande l'unité de temps choisie (GET /api/markets/candles : M1, M5, M10, M15,
M30, H1, H4) ; M10 n'existe pas chez le fournisseur, il est construit en regroupant des bougies M5. Cache court
(20 s en M1, davantage pour les unités plus longues). Chaque signal porte aussi les zones d'achat / de vente des
stratégies déjà calculées par le tableau de bord du Trading (`strategies`), pour les afficher sur le graphique.
"""
from __future__ import annotations

import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from typing import Any, Callable


TTL = 300         # analyse (biais H1, zones H4, taux de réussite) : calculs plus lourds
TECH_TTL = 300    # indicateurs (RSI, tendances M15/H1/H4, bougies H1) : 5 minutes pour éviter la latence
QUOTE_TTL = 8     # cotation (dernier prix, courbe 15 min) : rafraîchissement rapide en 8 s
NEWS_TTL = 1800
_cache: dict[str, tuple[float, dict[str, Any]]] = {}    # analyse par symbole
_tech: dict[str, tuple[float, dict[str, Any]]] = {}
_quotes: dict[str, tuple[float, dict[str, Any]]] = {}
_last: dict[str, dict[str, Any]] = {}                   # dernier signal complet (secours si le délai est dépassé)
_news: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_series: dict[str, tuple[float, dict[str, Any]]] = {}   # bougies par (symbole, unité de temps)
_lock = threading.Lock()

# rubrique Google News → catégorie affichée
TOPICS = ("BUSINESS", "WORLD", "TECHNOLOGY")
EDITIONS = {"fr": ("fr", "FR", "FR:fr"), "en": ("en-US", "US", "US:en"), "de": ("de", "DE", "DE:de")}

# bande « indices et marchés » : (clé de libellé côté app, code de données)
TICKERS = (("spx", "^GSPC"), ("ndx", "^NDX"), ("gold", "GC=F"), ("oil", "CL=F"), ("eurusd", "EURUSD=X"), ("btc", "BTC-USD"))
DAY = 24 * 3600
H1_DAYS = 5
H1_MAX = 120


def _r(x: Any, n: int | None = None) -> float | None:
    from .dashboard import _r as r
    return r(x, n)


def _memo(store: dict[str, tuple[float, dict[str, Any]]], key: str, ttl: float, fn: Callable[[], dict[str, Any] | None]
          ) -> dict[str, Any] | None:
    """Valeur en cache si elle est récente ; sinon recalcul (et, en cas d'échec, la dernière valeur connue)."""
    with _lock:
        hit = store.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        val = None
    if val is None:
        return hit[1] if hit else None
    with _lock:
        store[key] = (time.time(), val)
    return val


def _rows(rows: list[Any]) -> list[list[float | None]]:
    """Bougies compactées [t, o, h, l, c] (prix arrondis selon leur ordre de grandeur)."""
    return [[int(r[0]), _r(r[1]), _r(r[2]), _r(r[3]), _r(r[4])] for r in rows]


# unité de temps du graphique → (unité source, regroupement en secondes ou 0, durée du cache en secondes)
CANDLE_TFS: dict[str, tuple[str, int, int]] = {
    "1m": ("1m", 0, 8), "5m": ("5m", 0, 12), "10m": ("5m", 600, 15), "15m": ("15m", 0, 20),
    "30m": ("30m", 0, 30), "1h": ("1h", 0, 45), "4h": ("4h", 0, 120), "1d": ("1d", 0, 300)}
CANDLE_MAX = 500


def aggregate(rows: list[Any], secs: int) -> list[list[float]]:
    """Regroupe des bougies en bougies de `secs` secondes alignées sur l'horloge (M5 → M10). Le premier
    groupe est écarté s'il est incomplet (la série commence au milieu d'une bougie)."""
    out: list[list[float]] = []
    start = 0
    while start < len(rows) and int(rows[start][0]) % secs:
        start += 1
    for r in rows[start:]:
        t = int(r[0]) - int(r[0]) % secs
        if out and out[-1][0] == t:
            k = out[-1]
            k[2], k[3], k[4] = max(k[2], r[2]), min(k[3], r[3]), r[4]
        else:
            out.append([t, r[1], r[2], r[3], r[4]])
    return out


def series(sym: str, tf: str, limit: int = 150) -> dict[str, Any]:
    """Bougies [t, o, h, l, c] de l'unité `tf` (les `limit` dernières) ; liste vide si la source ne répond pas."""
    base, group, ttl = CANDLE_TFS[tf]

    def calc() -> dict[str, Any] | None:
        from agentic_ari import watch
        rows = watch.candles(sym, base, max_age=ttl)
        if not rows:
            return None
        if group:
            rows = aggregate(rows, group)
        return {"rows": _rows(rows[-CANDLE_MAX:]), "at": time.time()}
    hit = _memo(_series, f"{sym}|{tf}", ttl, calc) or {"rows": [], "at": time.time()}
    limit = max(10, min(int(limit), CANDLE_MAX))
    return {"symbol": sym, "tf": tf, "candles": hit["rows"][-limit:], "at": hit["at"], "refresh": ttl}


def _quote(sym: str) -> dict[str, Any] | None:
    """Dernier prix, variation sur 24 h, plus haut/bas des 24 h et bougies 15 min des 24 dernières heures
    (mêmes calculs que le prix du tableau de bord du Trading)."""
    def calc() -> dict[str, Any] | None:
        from agentic_ari import watch
        rows = watch.candles(sym, "15m", max_age=QUOTE_TTL)
        if not rows:
            return None
        last = rows[-1]
        day = [r for r in rows if r[0] >= last[0] - DAY]
        prev = [r for r in rows if r[0] <= last[0] - DAY]
        ref = prev[-1][4] if prev else day[0][1]
        return {"price": _r(last[4]), "time": int(last[0]), "high": _r(max(r[2] for r in day)),
                "low": _r(min(r[3] for r in day)), "change": _r(last[4] - ref),
                "change_pct": _r((last[4] - ref) / ref * 100, 2) if ref else None, "m15": _rows(rows[-96:])}
    return _memo(_quotes, sym, QUOTE_TTL, calc)


def _technicals(sym: str) -> dict[str, Any] | None:
    """RSI(14) H1, tendance par unité (MM20 / MM50) et bougies H1 des 5 derniers jours."""
    def calc() -> dict[str, Any] | None:
        from agentic_ari import watch
        out: dict[str, Any] = {}
        mtf = []
        for tf, name in (("15m", "M15"), ("1h", "H1"), ("4h", "H4"), ("1d", "D1")):
            try:
                rows = watch.candles(sym, tf)
            except Exception:
                continue
            c = [r[4] for r in rows]
            s20, s50 = watch.sma(c, 20), watch.sma(c, 50)
            if s20 is None or s50 is None:
                continue
            item: dict[str, Any] = {"tf": name, "trend": "up" if s20 > s50 else "down" if s20 < s50 else "flat"}
            r = watch.rsi(c)
            if r is not None:
                item["rsi"] = _r(r, 0)
            mtf.append(item)
            if tf == "1h":
                if r is not None:
                    out["rsi"] = _r(r, 1)
                    out["rsi_tf"] = "H1"
                if rows:
                    end = rows[-1][0]
                    out["h1"] = _rows([x for x in rows if x[0] > end - H1_DAYS * DAY][-H1_MAX:])
        if mtf:
            out["mtf"] = mtf
        return out or None
    return _memo(_tech, sym, TECH_TTL, calc)


def _analysis(sym: str) -> dict[str, Any] | None:
    """Verdict (biais H1), zone d'entrée H4 et taux de réussite d'un backtest déjà calculé."""
    def calc() -> dict[str, Any] | None:
        from . import dashboard
        bias = dashboard._safe(lambda: dashboard._bias(sym))
        zones = dashboard._safe(lambda: dashboard._zones(sym))
        verdict = (bias or {}).get("verdict") if isinstance(bias, dict) else None
        out: dict[str, Any] = {"verdict": verdict or "ATTENDRE"}
        if isinstance(zones, dict) and not zones.get("error"):
            side = {"ACHAT": "buy", "VENTE": "sell"}.get(out["verdict"]) or (
                "buy" if (zones.get("buy") or {}).get("distance", 1e18) <= (zones.get("sell") or {}).get("distance", 1e18) else "sell")
            z = zones.get(side) or {}
            if z.get("low") is not None:
                out.update(side=side, entry=[z.get("low"), z.get("high")], target=z.get("target"), stop=z.get("stop"))
                if z.get("rr") is not None:
                    out["rr"] = z["rr"]
            if zones.get("atr") is not None:
                out["atr"] = _r(zones["atr"])
        elif isinstance(bias, dict) and bias.get("atr") is not None:
            out["atr"] = _r(bias["atr"])
        # taux de réussite : seulement celui d'un backtest déjà calculé par le tableau de bord
        with dashboard._lock:
            full = dashboard._cache.get(sym)
        strategies = (full or {}).get("strategies")
        if isinstance(strategies, list):
            best = [s for s in strategies if s.get("win") is not None and (s.get("active") or s.get("recommended"))]
            if best:
                b = max(best, key=lambda s: s.get("note") or 0)
                out["win"] = b.get("win")
                out["win_n"] = b.get("n")
            zones = [{"id": s.get("id"), "name": s.get("name"), "tf": s.get("tf"), "active": bool(s.get("active")),
                      "live": s["live"]}
                     for s in strategies if (s.get("active") or s.get("recommended")) and isinstance(s.get("live"), dict)
                     and not s["live"].get("error") and s["live"].get("state") != "none"]
            if zones:
                out["strategies"] = zones[:8]
        return out
    return _memo(_cache, sym, TTL, calc)


def _entry_dist(price: float, entry: list[Any]) -> dict[str, Any] | None:
    try:
        lo, hi = sorted(float(x) for x in entry)
    except (TypeError, ValueError):
        return None
    inside = lo <= price <= hi
    d = 0.0 if inside else min(abs(price - lo), abs(price - hi))
    return {"entry_dist": _r(d), "entry_dist_pct": _r(d / price * 100, 2) if price else None, "in_zone": inside}


def _signal(sym: str) -> dict[str, Any] | None:
    from agentic_ari.spot import display
    q = _quote(sym)
    if not q or q.get("price") is None:
        return None
    m15 = q["m15"]
    out: dict[str, Any] = {"code": sym, "name": display(sym) or sym,
                           **{k: q.get(k) for k in ("price", "time", "change", "change_pct", "high", "low")},
                           "spark": [r[4] for r in m15[-48:]], "m15": m15}
    out.update(_technicals(sym) or {})
    a = _analysis(sym) or {"verdict": "ATTENDRE"}
    out.update(a)
    if a.get("entry"):
        out.update(_entry_dist(float(q["price"]), a["entry"]) or {})
    out = {k: v for k, v in out.items() if v is not None}
    with _lock:
        _last[sym] = out
    return out


def _ticker_of(key: str, sym: str, q: dict[str, Any] | None) -> dict[str, Any] | None:
    if not q or q.get("price") is None:
        return None
    closes = [r[4] for r in q["m15"]]
    spark = closes[::2] if len(closes) > 48 else closes   # pas de 30 min : assez pour une mini-courbe
    if closes and spark[-1] != closes[-1]:
        spark = [*spark, closes[-1]]
    out = {"key": key, "code": sym, "price": q["price"], "change_pct": q.get("change_pct"), "time": q.get("time"),
           "spark": spark}
    return {k: v for k, v in out.items() if v is not None}


def _ticker(key: str, sym: str) -> dict[str, Any] | None:
    return _ticker_of(key, sym, _quote(sym))


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<.*?>", "", s or "")).strip()


def _topic(topic: str, lang: str) -> list[dict[str, Any]]:
    from agentic_ari.datasources import fetch
    hl, gl, ceid = EDITIONS.get(lang, EDITIONS["fr"])
    body, _ = fetch(f"https://news.google.com/rss/headlines/section/topic/{topic}?hl={hl}&gl={gl}&ceid={ceid}",
                    NEWS_TTL, check_robots=False)
    items = []
    for raw in re.findall(r"<item>(.*?)</item>", body, re.S)[:4]:
        def tag(name: str, raw: str = raw) -> str:
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", raw, re.S)
            return _clean(m.group(1)) if m else ""
        title, source = tag("title"), tag("source")
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        try:
            at = parsedate_to_datetime(tag("pubDate")).timestamp()
        except Exception:
            at = None
        if title:
            items.append({"topic": topic.lower(), "title": title[:180], "source": source[:60], "url": tag("link"), "at": at})
    return items


def news(lang: str) -> list[dict[str, Any]]:
    lang = lang if lang in EDITIONS else "fr"
    with _lock:
        hit = _news.get(lang)
    if hit and time.time() - hit[0] < NEWS_TTL:
        return hit[1]
    futs = [_pool.submit(_topic, topic, lang) for topic in TOPICS]
    rows: list[dict[str, Any]] = []
    for f in futs:
        try:
            rows.append(f.result(timeout=2.0))
        except Exception:
            rows.append([])
    # une de chaque rubrique à tour de rôle
    out = [r[i] for i in range(4) for r in rows if i < len(r)][:9]
    if out:
        with _lock:
            _news[lang] = (time.time(), out)
    return out or (hit[1] if hit else [])


DEADLINE = 1.8   # secondes : l'écran reste ultra-réactif sans bloquer l'UI
MAX_SYMBOLS = 8
_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="ari-overview")


def collect(codes: list[str], lang: str, deadline: float = DEADLINE, tickers: bool = True) -> dict[str, Any]:
    """Signaux + bande des indices + actualités. Une source lente (réseau qui traîne, flux TradingView,
    Yahoo) ne bloque pas l'écran : après `deadline`, on renvoie ce qui est prêt (ou la dernière valeur
    connue) et `partial` = true ; les calculs en cours remplissent le cache pour l'appel suivant."""
    from concurrent.futures import wait
    futs = [_pool.submit(_signal, c) for c in codes]
    tks = [_pool.submit(_ticker, k, c) for k, c in TICKERS] if tickers else []
    nw = _pool.submit(news, lang)
    wait([*futs, *tks, nw], timeout=deadline)
    partial = False
    signals: list[dict[str, Any]] = []
    for code, f in zip(codes, futs):
        if f.done():
            try:
                s = f.result()
            except Exception:
                s = None
        else:
            partial = True
            with _lock:
                s = _last.get(code)
        if s:
            signals.append(s)
    strip: list[dict[str, Any]] = []
    for (key, code), f in zip(TICKERS if tickers else (), tks):
        t = None
        if f.done():
            try:
                t = f.result()
            except Exception:
                t = None
        else:
            with _lock:
                hit = _quotes.get(code)
            t = _ticker_of(key, code, hit[1] if hit else None)
        if t:
            strip.append(t)
    if nw.done():
        try:
            rows = nw.result()
        except Exception:
            rows = []
    else:
        partial = True
        with _lock:
            hit_n = _news.get(lang if lang in EDITIONS else "fr")
        rows = hit_n[1] if hit_n else []
    out: dict[str, Any] = {"at": time.time(), "signals": signals, "tickers": strip, "news": rows,
                           "refresh": 5 if partial else 8}
    if partial:
        out["partial"] = True
    return out

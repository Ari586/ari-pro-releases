"""Surveillances de graphiques (agentic_ari.watch) exécutées par le pont tant que l'app est ouverte.

- toutes les 30 s : Service().watch_due() (analyse seulement, notification macOS si un signal est nouveau)
- jamais deux fois en même temps : verrou de fichier partagé, et pause si le service `./ari service run` tourne
- chaque nouvelle alerte est envoyée en direct à l'interface (événement « alert »)
"""
import fcntl
import json
import os
import subprocess
import threading
import time

from agentic_ari.config import ROOT, Config

LOCK = ROOT / ".ari" / "watch.lock"
EVERY = 30


def service_running():
    try:
        out = subprocess.run(["pgrep", "-f", "agentic_ari.*service run|ari service run"], capture_output=True, text=True).stdout
        return bool(out.strip())
    except Exception:
        return False


def _disp(sym):
    """Nom lisible du symbole (GC=F → XAUUSD), selon le cœur."""
    try:
        from agentic_ari.spot import display
        return display(sym) or sym
    except Exception:
        return sym


def _disp_text(text, sym):
    return (text or "").replace(sym, _disp(sym)) if sym else (text or "")


class WatchRunner:
    def __init__(self, bus, projects, busy=lambda: False):
        self.bus, self.projects, self.busy = bus, projects, busy
        self.last_alert = None
        self._keys = set()
        self.error = None
        if projects.state.get("watch_voice"):
            os.environ["ARI_WATCH_VOICE"] = "1"
        threading.Thread(target=self._loop, daemon=True, name="watches").start()

    # ---------- données ----------
    @staticmethod
    def _db():
        from agentic_ari.watch import Watches
        return Watches(Config())

    def watches(self):
        rows = self._db().db.execute("SELECT id, symbol, timeframe, every_min, levels, active, next_run, condition FROM watches ORDER BY id").fetchall()
        return [{"id": i, "symbol": _disp(s), "timeframe": tf, "every_min": e, "levels": json.loads(lv or "[]"), "active": bool(a),
                 "next_run": nr, "condition": c or ""} for i, s, tf, e, lv, a, nr, c in rows]

    def alerts(self, limit=50):
        rows = self._db().db.execute(
            "SELECT a.id, a.created, a.signal, a.text, a.price, a.seen, w.symbol FROM alerts a LEFT JOIN watches w ON w.id=a.watch_id "
            "ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()
        out, seen_keys = [], set()
        for i, c, sg, t, p, se, sym in rows:
            if sg and sg.startswith("strat_"):   # même signal de stratégie (même bougie) répété : une seule ligne
                if (sym, sg) in seen_keys:
                    continue
                seen_keys.add((sym, sg))
            out.append({"id": i, "created": c, "signal": sg, "text": _disp_text(t, sym), "price": p, "seen": bool(se),
                        "symbol": _disp(sym)})
        return out

    def mark_seen(self, ids=None):
        w = self._db()
        if ids:
            w.db.executemany("UPDATE alerts SET seen=1 WHERE id=?", [(int(i),) for i in ids])
        else:
            w.db.execute("UPDATE alerts SET seen=1 WHERE seen=0")
        w.db.commit()

    def set_active(self, wid, active):
        self._db().set_active(int(wid), bool(active))

    def remove(self, wid):
        self._db().remove(int(wid))

    def set_voice(self, on):
        if on:
            os.environ["ARI_WATCH_VOICE"] = "1"
        else:
            os.environ.pop("ARI_WATCH_VOICE", None)
        self.projects.state["watch_voice"] = bool(on)
        self.projects._save()

    def status(self):
        unseen = sum(1 for a in self.alerts(100) if not a["seen"])
        return {"unseen": unseen, "voice": os.getenv("ARI_WATCH_VOICE") == "1", "service": service_running(),
                "error": self.error}

    # ---------- boucle ----------
    def _loop(self):
        time.sleep(5)
        try:
            self.last_alert = max([a["id"] for a in self.alerts(1)] or [0])
            self._keys = {(a["symbol"], a["signal"]) for a in self.alerts(200)}
        except Exception:
            self.last_alert, self._keys = 0, set()
        while True:
            try:
                self._tick()
                self.error = None
            except Exception as e:
                self.error = f"{type(e).__name__} : {e}"
            time.sleep(EVERY)

    def _tick(self):
        if service_running():   # le service s'en charge déjà
            return
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(LOCK, "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            from agentic_ari.service import Service
            svc = Service()
            svc.watch_due()
            # comme le service de fond : tâches planifiées / rappels et évolution, mais jamais pendant
            # une demande en cours dans l'app (les modèles locaux partagent le même processeur)
            if not self.busy():
                for step in ("run_due", "evolve_if_due"):
                    try:
                        getattr(svc, step, lambda: None)()
                    except Exception as e:
                        self.error = f"{step} : {type(e).__name__} : {e}"
        for a in reversed(self.alerts(20)):
            if a["id"] > (self.last_alert or 0):
                self.last_alert = a["id"]
                key = (a["symbol"], a["signal"])
                if (a["signal"] or "").startswith("strat_") and key in self._keys:
                    continue   # même signal déjà annoncé : pas de nouvelle cloche dans l'app
                self._keys.add(key)
                self.bus.emit("alert", **a)

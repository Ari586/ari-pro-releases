"""Bus d'événements : diffuse le déroulé des tâches aux interfaces connectées et attend leurs réponses.

- `emit` envoie un événement à tous les clients (journal, réponses, demandes de confirmation…)
- `request` envoie une demande et attend la réponse d'un client (confirmation, action sur l'onglet)
- un tampon garde l'historique récent pour qu'une interface ouverte en cours de route le rattrape
"""
import itertools
import queue
import threading
import time
import uuid

REPLAY_LIMIT = 600
LIVE_ONLY = {"tab", "listen", "term", "wake", "wake_error", "stt", "delta", "delta_reset", "health"}   # jamais rejoué : une action ne doit s'exécuter qu'une fois (et le brouillon en direct est remplacé par « answer »)


class Cancelled(BaseException):
    """Arrêt demandé par l'utilisateur. BaseException pour traverser les `except Exception` du cœur."""


class Client:
    def __init__(self, client_id, caps):
        self.id = client_id
        self.caps = set(caps)
        self.queue = queue.Queue(maxsize=2000)
        self.connected_at = time.time()


class Bus:
    def __init__(self):
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._clients = {}
        self._history = []
        self._pending = {}   # id -> {"event": threading.Event, "value": ...}

    # ---------- clients ----------
    def connect(self, client_id, caps, since=0):
        client = Client(client_id, caps)
        with self._lock:
            old = self._clients.get(client_id)
            if old:
                old.queue.put(None)  # ferme l'ancien flux du même client
            self._clients[client_id] = client
            for ev in self._history:
                if ev["seq"] > since:
                    client.queue.put(ev)
        return client

    def disconnect(self, client):
        with self._lock:
            if self._clients.get(client.id) is client:
                del self._clients[client.id]

    def clients(self):
        with self._lock:
            return [{"id": c.id, "caps": sorted(c.caps)} for c in self._clients.values()]

    def cap_client(self, cap):
        """Client le plus récent qui sait faire `cap` (ex. « voice » : l'app Mac, pour la dictée)."""
        with self._lock:
            found = [c for c in self._clients.values() if cap in c.caps]
            return max(found, key=lambda c: c.connected_at).id if found else None

    def tab_client(self, preferred=None):
        """Client capable d'agir sur un onglet : celui demandé, sinon le plus récemment connecté."""
        with self._lock:
            tabs = [c for c in self._clients.values() if "tab" in c.caps]
            for c in tabs:
                if c.id == preferred:
                    return c.id
            return max(tabs, key=lambda c: c.connected_at).id if tabs else None

    # ---------- diffusion ----------
    def emit(self, type_, **data):
        ev = {"type": type_, "time": time.time(), **data}
        with self._lock:
            ev["seq"] = next(self._seq)
            if type_ not in LIVE_ONLY:
                self._history.append(ev)
                del self._history[:-REPLAY_LIMIT]
            for c in self._clients.values():
                try:
                    c.queue.put_nowait(ev)
                except queue.Full:
                    pass
        return ev

    def last_seq(self):
        with self._lock:
            return self._history[-1]["seq"] if self._history else 0

    def reset(self):
        with self._lock:
            self._history.clear()
        self.emit("reset")

    # ---------- question / réponse ----------
    def request(self, type_, timeout, cancelled=lambda: False, **data):
        """Émet une demande et attend `answer(id, valeur)`. Renvoie (répondu, valeur)."""
        rid = uuid.uuid4().hex[:12]
        slot = {"event": threading.Event(), "value": None}
        with self._lock:
            self._pending[rid] = slot
        try:
            self.emit(type_, id=rid, **data)
            deadline = time.time() + timeout
            while not slot["event"].wait(0.5):
                if cancelled():
                    raise Cancelled()
                if time.time() > deadline:
                    return False, None
            return True, slot["value"]
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    def answer(self, rid, value):
        with self._lock:
            slot = self._pending.get(rid)
        if not slot or slot["event"].is_set():
            return False
        slot["value"] = value
        slot["event"].set()
        return True

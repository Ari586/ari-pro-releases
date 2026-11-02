"""Conversations enregistrées sur le Mac (.ari/conversations/), pour les retrouver, les rouvrir et les continuer.

Chaque échange (demande, réponse ou erreur) est ajouté à la conversation en cours. « Nouvelle conversation »
en commence une autre ; « Ouvrir » recharge les échanges dans l'interface et redonne le contexte à Ari.
"""
import json
import re
import threading
import time
import uuid

from agentic_ari.config import ROOT

DIR = ROOT / ".ari" / "conversations"


class Conversations:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = None   # id de la conversation en cours

    def _path(self, cid):
        if not re.fullmatch(r"[\w-]{6,40}", str(cid or "")):
            raise ValueError("conversation inconnue")
        return DIR / f"{cid}.json"

    def _load(self, cid):
        return json.loads(self._path(cid).read_text())

    def _save(self, conv):
        DIR.mkdir(parents=True, exist_ok=True)
        self._path(conv["id"]).write_text(json.dumps(conv, ensure_ascii=False))

    def new(self):
        with self.lock:
            self.current = None

    def add_turn(self, question, answer, mode, error=False):
        with self.lock:
            conv = None
            if self.current:
                try:
                    conv = self._load(self.current)
                except (OSError, ValueError):
                    conv = None
            if conv is None:
                cid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
                title = re.sub(r"\s+", " ", question).strip()[:70]
                conv = {"id": cid, "title": title or "Conversation", "created": time.time(), "turns": []}
                self.current = cid
            conv["turns"].append({"q": question, "a": answer, "mode": mode, "error": error, "time": time.time()})
            conv["updated"] = time.time()
            self._save(conv)

    def list(self, query=""):
        if not DIR.exists():
            return []
        q = query.strip().lower()
        out = []
        for p in sorted(DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:300]:
            try:
                c = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if q and q not in c.get("title", "").lower() and not any(
                    q in (t.get("q", "") + " " + t.get("a", "")).lower() for t in c.get("turns", [])):
                continue
            out.append({"id": c["id"], "title": c.get("title", ""), "updated": c.get("updated", c.get("created")),
                        "turns": len(c.get("turns", [])), "current": c["id"] == self.current})
        return out

    def get(self, cid):
        return self._load(cid)

    def delete(self, cid):
        self._path(cid).unlink(missing_ok=True)
        if self.current == cid:
            self.current = None
        return True

    def open(self, cid):
        conv = self._load(cid)
        with self.lock:
            self.current = cid
        return conv

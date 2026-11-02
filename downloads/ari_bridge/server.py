"""Serveur HTTP local du pont.

Sécurité :
- écoute uniquement sur 127.0.0.1 (jamais sur le réseau), SAUF si l'accès mobile est activé à la main
  (`python -m ari_bridge mobile on` → fichier `.ari/bridge_lan`) : le pont écoute alors aussi sur le réseau
  local pour l'app Android, toujours avec la clé obligatoire, et seulement pour des clients hors navigateur
- chaque appel /api exige `Authorization: Bearer <clé>` ; la clé est générée au premier lancement
  dans `.ari/bridge_token` (lisible par vous seul)
- refuse les en-têtes Host étrangers (protection DNS rebinding) et les Origin de sites web : seuls
  l'extension (chrome-extension://) et l'interface servie par ce serveur sont acceptées
- l'interface servie a une CSP stricte et ne peut pas être affichée dans un cadre d'un autre site
"""

import hmac
import threading
import time
import json
import mimetypes
import os
import queue
import secrets
import stat
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

from agentic_ari.config import ROOT, Config

from . import __version__
from .bus import Bus
from .jobs import MODES, Runner
from . import ide
from .stt import LocalSTT
from .tts import PiperTTS
from .watching import WatchRunner
from .wake import SherpaWake
from .projects import Projects

LAN_FLAG = ROOT / ".ari" / "bridge_lan"  # accès depuis l'app Android (désactivé par défaut)
LOCAL = "127.0.0.1"
LAN = LAN_FLAG.exists()
HOST = "0.0.0.0" if LAN else LOCAL
PORT = int(os.getenv("ARI_BRIDGE_PORT", "8765"))
UI_DIR = ROOT / "extension"
UI_FILES = {
    "/": "panel.html",
    "/panel.html": "panel.html",
    "/panel.js": "panel.js",
    "/style.css": "style.css",
    "/markdown.js": "markdown.js",
    "/tab.js": "tab.js",
    "/iris.js": "iris.js",
    "/i18n.js": "i18n.js",
    "/sounds/ari-pop.wav": "sounds/ari-pop.wav",
    "/ide.js": "ide.js",
    "/hub.js": "hub.js",
    "/icons/icon128.png": "icons/icon128.png",
    "/icons/avatar.jpg": "icons/avatar.jpg",
    "/icons/octobot.png": "icons/octobot.png",
}
TOKEN_FILE = ROOT / ".ari" / "bridge_token"
MAX_BODY = 100 * 1024 * 1024  # captures de l'onglet, photos à retoucher, images d'un diaporama
PING_EVERY = 15
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self'; font-src 'self' data:; worker-src 'self' blob:; media-src 'self' blob:; base-uri 'none'; form-action 'none'; "
    "frame-src http://127.0.0.1:8000 http://localhost:8000; frame-ancestors 'none'"
)  # frame-src : OctoBot local (port 8000) affiché dans Ari


def clean_selection(sel, projects):
    """Sélection envoyée par l'interface (code surligné dans l'app, ou texte surligné dans l'onglet).

    App : le fichier doit être dans le projet ouvert (sinon la sélection est ignorée)."""
    if not isinstance(sel, dict) or not str(sel.get("text") or "").strip():
        return None
    text = str(sel["text"])[:60000]
    if sel.get("source") == "app":
        try:
            root, path = projects._inside(str(sel.get("path") or ""))
            start, end = int(sel.get("start_line")), int(sel.get("end_line"))
        except (ValueError, TypeError):
            return None
        if not path.is_file() or start < 1 or end < start:
            return None
        return {"source": "app", "file": str(path), "start_line": start, "end_line": end, "text": text}
    if sel.get("source") == "tab":
        return {"source": "tab", "url": str(sel.get("url") or "")[:2000], "text": text}
    return None


def speakable(md):
    """Texte à lire à voix haute : sans balises Markdown, blocs de code ni liens bruts."""
    import re

    t = re.sub(r"^\s*Commande\s*:.*$", "", md, flags=re.M)  # bloc « Commande » ajouté par le système
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"^\s*([#>*|-]+|\d+[.)])\s*", "", t, flags=re.M)
    t = t.replace("**", "").replace("__", "").replace("|", " ")
    return re.sub(r"\n{2,}", "\n", t).strip()[:4000]


def load_token():
    """Lit ou crée la clé du pont (droits 600), sans l'exporter dans l'extension."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if len(tok) >= 32:
            return tok
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(tok)
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return tok


class Handler(BaseHTTPRequestHandler):
    server_version = f"AgenticAriBridge/{__version__}"
    protocol_version = "HTTP/1.1"

    # ---------- garde-fous ----------
    def _origin_ok(self):
        host = (self.headers.get("Host") or "").lower()
        origin = self.headers.get("Origin")
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            # accès mobile activé : l'app Android (pas de Origin) peut venir du réseau local ;
            # une page web (toujours avec Origin) reste refusée → pas de DNS rebinding
            return LAN and origin is None
        if origin is None:  # requête non issue d'une page web (curl, app Mac)
            return True
        return origin.startswith("chrome-extension://") or origin in (
            f"http://127.0.0.1:{PORT}",
            f"http://localhost:{PORT}",
        )

    def _authed(self):
        auth = self.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else ""
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        extra = dict(extra or {})
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("requête trop volumineuse")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("objet JSON attendu")
        return data

    def log_message(self, fmt, *args):
        if os.getenv("ARI_BRIDGE_DEBUG"):
            sys.stderr.write("[bridge] " + fmt % args + "\n")

    # ---------- routes ----------
    def do_OPTIONS(self):
        if not self._origin_ok():
            return self._send(403, {"error": "origine refusée"})
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {"error": "origine refusée"})
        url = urlparse(self.path)
        if (
            url.path == "/config.js"
        ):  # jamais la clé en HTTP (une page web pourrait l'inclure en <script>) : l'app la reçoit à part
            return self._send(
                200, b"window.ARI_DEFAULT_CONFIG = window.ARI_DEFAULT_CONFIG || {};\n", "text/javascript; charset=utf-8"
            )
        if url.path in UI_FILES:
            return self._static(UI_FILES[url.path])
        if url.path.startswith("/vendor/"):  # bibliothèques embarquées (éditeur Monaco), servies en local
            return self._static(url.path.lstrip("/"))
        if url.path == "/api/health":
            return self._send(200, {"ok": True, "app": "agentic-ari", "version": __version__})
        if not self._authed():
            return self._send(401, {"error": "clé manquante ou invalide"})
        if url.path == "/api/status":
            return self._send(200, self._status())
        if url.path == "/api/stream":
            return self._stream(parse_qs(url.query))
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        projects = self.server.projects
        try:
            if url.path == "/api/project":
                return self._send(200, projects.info())
            if url.path == "/api/memory":  # mémoire à long terme d'Ari (agentic_ari.souvenirs)
                sv = self._souvenirs()
                return self._send(
                    200,
                    json.loads(
                        json.dumps(
                            {
                                "facts": sv.list_kind("fact"),
                                "board": sv.board(),
                                "links": sv.active_links(""),
                                "history": sv.history(),
                                "profile": sv.get_state("profil"),
                            },
                            default=str,
                        )
                    ),
                )
            if url.path == "/api/memory/search":
                return self._send(
                    200,
                    json.loads(json.dumps({"items": self._souvenirs().recall(qs.get("q", ""), limit=12)}, default=str)),
                )

            if url.path == "/api/trading/dashboard":  # tableau de bord trading (ari_bridge/dashboard.py)
                from . import dashboard
                from .trading_more import resolve

                d = dashboard.get(
                    resolve(qs.get("symbol")) if qs.get("symbol") else dashboard.SYMBOL,
                    alerts=lambda: self.server.watching.alerts(8),
                    fresh=qs.get("fresh") == "1",
                )
                return self._send(200, json.loads(json.dumps(d, default=str)))
            if url.path.startswith("/api/markets/") or url.path in (
                "/api/trading/symbols",
                "/api/trading/search",
                "/api/journal",
                "/api/trading/bridge",
                "/api/trading/bridge/positions",
            ):  # trading complet repris d'ARI Pro (ari_bridge/trading_more.py)
                from . import trading_more as tm

                try:
                    if url.path == "/api/trading/symbols":
                        return self._send(200, tm.symbols())
                    if url.path == "/api/trading/search":
                        return self._send(200, tm.search(qs.get("q", "")))
                    if url.path == "/api/markets/candles":
                        return self._send(
                            200, tm.candles(qs.get("symbol"), qs.get("tf", "15m"), int(qs.get("limit") or 150))
                        )
                    if url.path == "/api/markets/overview":
                        return self._send(
                            200,
                            json.loads(
                                json.dumps(
                                    tm.overview(
                                        self.server.watching.watches(), qs.get("lang", "fr"), qs.get("symbols", "")
                                    ),
                                    default=str,
                                )
                            ),
                        )
                    if url.path == "/api/journal":
                        return self._send(200, json.loads(json.dumps(tm.journal(), default=str)))
                    if url.path == "/api/trading/bridge":
                        return self._send(200, tm.bridge_status())
                    if url.path == "/api/trading/bridge/positions":
                        return self._send(200, tm.bridge_positions())
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(502, {"error": f"{type(e).__name__} : {e}"})
            if url.path == "/api/watches":  # surveillances de graphiques + alertes
                w = self.server.watching
                return self._send(
                    200,
                    json.loads(json.dumps({"watches": w.watches(), "alerts": w.alerts(), **w.status()}, default=str)),
                )
            if url.path == "/api/conversations":
                return self._send(200, {"items": self.server.runner.conversations.list(qs.get("q", ""))})
            if url.path == "/api/missions":  # points de restauration du cœur : une mission = un point
                from agentic_ari import checkpoints as cp

                pts = sorted(cp.DIR.glob("*/manifest.json"), reverse=True)[:20]
                items = []
                for i, m in enumerate(pts, 1):
                    man = json.loads(m.read_text())
                    items.append(
                        {
                            "n": i,
                            "id": m.parent.name,
                            "created": man.get("created"),
                            "workspace": man.get("workspace"),
                            "files": list(man.get("files", {})),
                        }
                    )
                return self._send(200, {"items": items})
            if url.path == "/api/checkpoints":
                return self._send(200, {"items": self.server.runner.checkpoints.list()})
            if url.path == "/api/git/status":
                return self._send(200, ide.git_status(projects))
            if url.path == "/api/git/diff":
                return self._send(200, ide.git_diff(projects, qs.get("path", "")))
            if url.path == "/api/omniroute":  # passerelle multi-fournisseurs locale (port 20128), état seulement
                from agentic_ari import omniroute_link as ol

                st = ol.status() or {}
                return self._send(
                    200, {k: st.get(k) for k in ("container", "running", "dashboard", "has_key", "models_count")}
                )
            if url.path == "/api/voices":
                return self._send(200, {"items": self.server.voices.catalog()})
            if url.path == "/api/creations":  # documents, photos, vidéos, images créés par Ari (~/Documents/Ari)
                from . import creations

                return self._send(200, creations.listing())
            if url.path == "/api/creations/file":  # aperçu (images, vidéos, PDF) : nom simple dans le dossier de sortie
                from . import creations

                body, mime = creations.file_bytes(qs.get("name", ""))
                return self._send(200, body, mime)
            if url.path == "/api/cloud":  # Ari Cloud (moteur Tontolo) : état, désactivé par défaut
                from agentic_ari import cloud_gratuit as cg

                st = cg.status()
                return self._send(
                    200, {k: st.get(k) for k in ("name", "enabled", "installed", "running", "models_count")}
                )
            if url.path == "/api/models":  # pour le menu des modèles (appelé à son ouverture)
                return self._send(200, self._models())
            if url.path == "/api/dirs":
                return self._send(200, projects.list_dirs(qs.get("path")))
            if url.path == "/api/tree":
                return self._send(200, projects.tree(qs.get("path", "")))
            if url.path == "/api/file":
                return self._send(200, projects.read(qs.get("path", "")))
            if url.path == "/api/discovery":
                import socket

                hostname = socket.gethostname()
                local_ip = "127.0.0.1"
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    local_ip = s.getsockname()[0]
                    s.close()
                except Exception:
                    pass
                return self._send(
                    200,
                    {
                        "app": "agentic-ari",
                        "version": __version__,
                        "lan": LAN,
                        "host": HOST,
                        "port": PORT,
                        "local_ip": local_ip,
                        "hostname": hostname,
                        "service": "_agenticari._tcp.local.",
                    },
                )
            if url.path == "/api/fireflies/status":
                from agentic_ari import fireflies_link as fl

                return self._send(
                    200, {"configured": fl.is_configured(), "meetings_count": len(fl.search_meetings("", limit=100))}
                )
            if url.path == "/api/fireflies/meetings":
                from agentic_ari import fireflies_link as fl

                return self._send(
                    200, {"meetings": fl.search_meetings(qs.get("q", ""), limit=int(qs.get("limit", 15)))}
                )
            if url.path == "/api/tasks":
                from agentic_ari.memory import Memory

                mem = Memory(Config().memory_path)
                return self._send(
                    200, {"tasks": mem.list_tasks(status=qs.get("status"), limit=int(qs.get("limit", 50)))}
                )
            if url.path in ("/api/tasks/detail", "/api/task"):
                from agentic_ari.memory import Memory

                mem = Memory(Config().memory_path)
                tid = qs.get("id") or qs.get("task_id", "")
                task = mem.get_task(tid)
                if not task:
                    return self._send(404, {"error": "tâche introuvable"})
                return self._send(
                    200, {"task": task, "subtasks": mem.get_subtasks(tid), "logs": mem.get_agent_logs(tid)}
                )
            if url.path == "/api/approvals":
                from agentic_ari.memory import Memory

                mem = Memory(Config().memory_path)
                return self._send(200, {"approvals": mem.get_pending_approvals()})
        except (ValueError, OSError) as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "introuvable"})

    def do_POST(self):
        if not self._origin_ok():
            return self._send(403, {"error": "origine refusée"})
        path = urlparse(self.path).path
        try:
            data = self._json()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": str(e)})

        # Webhook externe Fireflies.ai : exempté de clé Bearer
        if path == "/api/fireflies/webhook":
            from agentic_ari import fireflies_link as fl
            from agentic_ari.config import Config
            from agentic_ari.memory import Memory
            from agentic_ari.knowledge import Knowledge

            cfg = Config()
            try:
                res = fl.handle_webhook(data, memory=Memory(cfg.memory_path), kb=Knowledge(cfg.knowledge_path))
                return self._send(200, res)
            except Exception as e:
                return self._send(400, {"error": str(e)})

        if not self._authed():
            return self._send(401, {"error": "clé manquante ou invalide"})
        runner, bus = self.server.runner, self.server.bus
        if path == "/api/voice/listen":  # dictée pour l'extension : faite par l'app Mac, sur le Mac
            target = bus.cap_client("voice")
            if not target:
                return self._send(
                    409, {"error": "Ouvrez l'app Agentic Ari : c'est elle qui fait la dictée sur le Mac."}
                )
            answered, value = bus.request("listen", 90, target=target)
            value = value if isinstance(value, dict) else {}
            if not answered or value.get("error"):
                return self._send(502, {"error": value.get("error") or "pas de réponse de la dictée"})
            return self._send(200, {"text": str(value.get("text") or "")})
        if path in ("/api/ask", "/api/ask_sync"):
            text = str(data.get("text") or "").strip()
            mode = str(data.get("mode") or "team")
            if not text:
                return self._send(400, {"error": "demande vide"})
            if mode not in MODES:
                return self._send(400, {"error": f"mode inconnu : {mode}"})
            if mode == "tab" and not bus.tab_client(data.get("client")):
                return self._send(
                    409, {"error": "Le mode « Onglet » demande l'extension ouverte dans Chrome ou Comet."}
                )
            page = data.get("page") if isinstance(data.get("page"), dict) else None
            tab_id = data.get("tab_id") if isinstance(data.get("tab_id"), int) else None
            images = data.get("images") if isinstance(data.get("images"), list) else None
            selection = clean_selection(data.get("selection"), self.server.projects)
            job = runner.start(
                text[:20000],
                mode,
                str(data.get("client") or "")[:64] or None,
                bool(data.get("auto")),
                page,
                tab_id,
                images,
                selection,
                str(data.get("lang") or "auto")[:4],
            )
            if not job:
                return self._send(409, {"error": "Une tâche est déjà en cours. Attendez ou arrêtez-la."})
            job.lang = str(data.get("lang") or "auto")[:4]
            if path == "/api/ask":
                return self._send(200, {"job": job.id})
            # Siri / Raccourcis : on attend la fin et on renvoie la réponse en texte simple à lire.
            job.done.wait(25 * 60)
            text = job.answer or job.error or "Ari n'a pas terminé à temps."
            return self._send(200, {"job": job.id, "answer": text, "speech": speakable(text)})
        if path in ("/api/confirm", "/api/tab_result"):
            value = bool(data.get("ok")) if path == "/api/confirm" else data.get("result")
            return self._send(200, {"ok": bus.answer(str(data.get("id")), value)})
        if path in ("/api/project", "/api/model"):
            if runner.current:
                return self._send(
                    409, {"error": "Attendez la fin de la tâche en cours pour changer de projet ou de modèle."}
                )
            try:
                info = (
                    self.server.projects.set_project(data.get("path"))
                    if path == "/api/project"
                    else self.server.projects.set_model(str(data.get("model")))
                )
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            bus.emit("project", **info)
            return self._send(200, info)
        if path == "/api/shutdown":  # l'app Mac reprend la main sur un pont lancé ailleurs
            if runner.current:
                return self._send(409, {"error": "tâche en cours"})
            self._send(200, {"ok": True})
            import threading

            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path == "/api/rate":  # 👍 / 👎 sous une réponse → mémoire d'évolution du cœur
            try:
                from agentic_ari.evolution import Evolution

                rating = 1 if int(data.get("rating", 1)) > 0 else -1
                res = Evolution(Config()).rate(None, rating, str(data.get("note") or "")[:500])
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__} : {e}"})
            return self._send(200, {"ok": True, "result": str(res)[:300]})
        try:  # éditeur, points de restauration, terminal, Git
            if path == "/api/memory/forget":
                return self._send(200, {"ok": self._souvenirs().forget(int(data.get("id")))})
            if path == "/api/memory/pin":
                return self._send(
                    200, {"ok": self._souvenirs().pin(int(data.get("id")), 1 if data.get("pinned", True) else 0)}
                )
            if path == "/api/memory/board":
                fields = {k: str(data.get(k) or "")[:4000] for k in ("but", "plan", "blocage") if k in data}
                self._souvenirs().set_board(**fields)
                return self._send(200, {"ok": True})
            if path in ("/api/creations/reveal", "/api/creations/open"):
                from . import creations

                creations.reveal(data.get("name"), open_it=path.endswith("/open"))
                return self._send(200, {"ok": True})
            if path.startswith("/api/create/"):  # créations via le cœur (agentic_ari.creator)
                from . import creations

                what = path.rsplit("/", 1)[-1]
                try:
                    if what == "document":
                        res = creations.create_document(data.get("kind"), data.get("title"), data.get("content"))
                    elif what == "photo":
                        res = creations.edit_photo(data.get("image"), data.get("operations"))
                    elif what == "video":
                        res = creations.slideshow(data.get("images"), data.get("title"), data.get("seconds"))
                    elif what == "image":
                        res = creations.generate_image(data.get("prompt"))
                    elif what == "ai_video":
                        res = creations.ai_video(data.get("prompt"))
                    elif what == "nobg":
                        res = creations.remove_background(data.get("image"))
                    else:
                        return self._send(404, {"error": "introuvable"})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(500, {"error": f"{type(e).__name__} : {e}"})
                return self._send(200, res)
            if (
                path == "/api/omniroute/key"
            ):  # clé créée dans OmniRoute : vérifiée puis rangée dans le Trousseau, jamais renvoyée
                import httpx as _hx
                from agentic_ari import omniroute_link as ol
                from agentic_ari.memory import keychain_set

                key = str(data.get("key") or "").strip()
                data.clear()
                if not (8 <= len(key) <= 512) or any(c.isspace() for c in key):
                    return self._send(400, {"error": "clé vide ou mal copiée"})
                try:
                    r = _hx.get(f"{ol.API}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
                except Exception as e:
                    return self._send(502, {"error": f"OmniRoute ne répond pas ({type(e).__name__})"})
                if r.status_code != 200:
                    return self._send(400, {"error": f"OmniRoute refuse cette clé ({r.status_code})"})
                keychain_set(ol.SERVICE, "api_key", key)
                del key
                return self._send(200, {"ok": True, "models": len(r.json().get("data", []))})
            if (
                path == "/api/cloud"
            ):  # interrupteur Ari Cloud : seulement à la demande d'Andria, depuis le menu des modèles
                from agentic_ari import cloud_gratuit as cg

                on = bool(data.get("on"))
                msg = cg.set_enabled(on)
                if not on and str(self.server.projects.model).startswith("cloud"):
                    self.server.projects.set_model("auto")  # plus de modèle en ligne : retour au local
                st = cg.status()
                # message court, sans la ligne de crédit (Andria : rien dans l'interface ; LICENSE/NOTICE restent sur le disque)
                msg = (
                    "Ari Cloud activé : vos demandes à ce modèle partent sur Internet."
                    if on and st.get("enabled")
                    else "Ari Cloud désactivé : Ari redevient 100 % local."
                    if not on
                    else msg.split(". ")[0]
                )
                return self._send(
                    200,
                    {
                        "message": msg,
                        "enabled": st.get("enabled"),
                        "running": st.get("running"),
                        "model": self.server.projects.model,
                    },
                )
            if (
                path == "/api/binance/connect"
            ):  # clé LECTURE SEULE : vérifiée par le cœur, rangée dans le Trousseau, jamais renvoyée
                from agentic_ari import binance_link as bl

                key, secret = str(data.get("key") or "").strip(), str(data.get("secret") or "").strip()
                data.clear()
                if not (16 <= len(key) <= 256 and 16 <= len(secret) <= 256) or any(c.isspace() for c in key + secret):
                    return self._send(400, {"ok": False, "error": "clé ou secret mal copié"})
                ok, msg = bl.check_key(key, secret)
                if ok:
                    bl._kc_set("api_key", key)
                    bl._kc_set("api_secret", secret)
                    from . import dashboard

                    dashboard.invalidate_binance()
                del key, secret
                return self._send(200 if ok else 400, {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg})
            if (
                path == "/api/kraken/connect"
            ):  # clé Kraken « Query Funds » seulement : lue une fois pour vérifier, puis Trousseau
                from agentic_ari import crypto_data as cdat
                from agentic_ari.memory import keychain_set

                key, secret = str(data.get("key") or "").strip(), str(data.get("secret") or "").strip()
                data.clear()
                if not (16 <= len(key) <= 256 and 16 <= len(secret) <= 512) or any(c.isspace() for c in key + secret):
                    return self._send(400, {"error": "clé ou clé privée mal copiée"})
                try:
                    cdat.ReadOnly("kraken", key, secret).balance()
                except Exception as e:
                    return self._send(400, {"error": f"Kraken refuse cette clé ({type(e).__name__})"})
                keychain_set("ari-kraken", "api_key", key)
                keychain_set("ari-kraken", "api_secret", secret)
                del key, secret
                from . import dashboard

                dashboard.invalidate_kraken()
                return self._send(200, {"ok": True, "message": "Kraken connecté en lecture seule."})
            if path == "/api/kraken/disconnect":
                import subprocess as _sp

                for acc in ("api_key", "api_secret"):
                    _sp.run(["security", "delete-generic-password", "-s", "ari-kraken", "-a", acc], capture_output=True)
                from . import dashboard

                dashboard.invalidate_kraken()
                return self._send(200, {"ok": True, "message": "Clé Kraken effacée du Trousseau."})
            if (
                path == "/api/doctor/fix"
            ):  # « Réparer » : diagnostic du cœur avec relance de ses services (jamais le robot réel)
                from agentic_ari.doctor import doctor

                items = [{"ok": bool(ok), "name": n, "detail": d} for ok, n, d in doctor(fix=True)]
                self.server.health = items
                self.server.bus.emit("health", items=items)
                return self._send(200, {"items": items})
            if path == "/api/binance/disconnect":
                from agentic_ari import binance_link as bl
                from . import dashboard

                msg = bl.disconnect()
                dashboard.invalidate_binance()
                return self._send(200, {"ok": True, "message": msg})
            if path == "/api/trading/strategy":  # interrupteur d'une stratégie (API du cœur), sur l'actif affiché
                from . import dashboard
                from .trading_more import resolve

                sym = resolve(data.get("symbol")) if data.get("symbol") else dashboard.SYMBOL
                if data.get("all"):
                    return self._send(200, dashboard.activate_all(sym, on=data.get("on", True) is not False))
                dashboard.set_strategy(str(data.get("id") or ""), bool(data.get("on")), sym)
                return self._send(200, {"ok": True})
            if path in (
                "/api/journal",
                "/api/journal/close",
                "/api/trading/analyse",
                "/api/watches/run",
            ) or path.startswith(
                "/api/trading/bridge/"
            ):  # trading complet repris d'ARI Pro (ari_bridge/trading_more.py)
                from . import trading_more as tm

                routes = {
                    "/api/journal": lambda: tm.journal_open(data),
                    "/api/journal/close": lambda: tm.journal_close(data),
                    "/api/trading/analyse": lambda: tm.analyse(data.get("text")),
                    "/api/watches/run": tm.run_watches,
                    "/api/trading/bridge/config": lambda: tm.bridge_config(data),
                    "/api/trading/bridge/test": tm.bridge_test,
                    "/api/trading/bridge/order": lambda: tm.bridge_order(data),
                    "/api/trading/bridge/close": lambda: tm.bridge_close(data),
                }
                if path not in routes:
                    return self._send(404, {"error": "introuvable"})
                try:
                    return self._send(200, json.loads(json.dumps(routes[path](), default=str)))
                except LookupError as e:
                    return self._send(404, {"error": str(e)})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(502, {"error": f"{type(e).__name__} : {e}"})
            if path == "/api/watches/seen":
                self.server.watching.mark_seen(data.get("ids"))
                return self._send(200, {"ok": True})
            if path == "/api/watches/active":
                self.server.watching.set_active(data.get("id"), data.get("active"))
                return self._send(200, {"ok": True})
            if path == "/api/watches/remove":
                self.server.watching.remove(data.get("id"))
                return self._send(200, {"ok": True})
            if path == "/api/watches/voice":
                self.server.watching.set_voice(bool(data.get("on")))
                return self._send(200, {"ok": True})
            if path == "/api/conversations/open":
                if runner.current:
                    return self._send(409, {"error": "Arrêtez la tâche en cours d'abord."})
                return self._send(200, runner.open_conversation(str(data.get("id"))))
            if path == "/api/conversations/delete":
                return self._send(200, {"ok": runner.conversations.delete(str(data.get("id")))})
            if path == "/api/file/save":
                return self._send(
                    200, ide.save_file(self.server.projects, data.get("path", ""), data.get("content", ""))
                )
            if path == "/api/missions/restore":
                if runner.current:
                    return self._send(409, {"error": "Arrêtez la tâche en cours d'abord."})
                from agentic_ari import checkpoints as cp

                return self._send(200, {"result": cp.restore(int(data.get("n")))})
            if path == "/api/checkpoints/restore":
                return self._send(200, runner.checkpoints.restore(str(data.get("id"))))
            if path == "/api/term/run":
                return self._send(200, {"id": self.server.terminal.run(str(data.get("command") or "")[:4000])})
            if path == "/api/term/stop":
                return self._send(200, {"stopped": self.server.terminal.stop()})
            if path == "/api/git/commit":
                return self._send(200, ide.git_commit(self.server.projects, data.get("message")))
            if path == "/api/tts":  # une phrase → WAV, voix choisie selon la langue de la phrase (Piper ou voix du Mac)
                wav, lang, voice = self.server.voices.wav(
                    str(data.get("text") or ""), str(data.get("lang") or "auto"), str(data.get("hint") or "fr")[:2]
                )
                return self._send(
                    200,
                    wav,
                    "audio/wav",
                    {"X-Ari-Lang": lang, "X-Ari-Voice": voice.encode("ascii", "replace").decode()},
                )
            if path == "/api/voices":  # voix choisie par langue (« auto » = choix d'Ari)
                lang, voice = str(data.get("lang") or "")[:3], str(data.get("voice") or "")[:120]
                prefs = dict(self.server.projects.state.get("voices") or {})
                if voice in ("", "auto"):
                    prefs.pop(lang, None)
                else:
                    prefs[lang] = voice
                self.server.projects.state["voices"] = prefs
                self.server.projects._save()
                return self._send(200, {"ok": True})
            if path == "/api/voice/stop":  # arrête l'écoute Whisper en cours (micro libéré)
                self.server.stt.cancel = True
                return self._send(200, {"ok": True})
            if path == "/api/voice/local":  # dictée Whisper hors ligne (quand Apple n'a pas la langue)
                was = self.server.wake.paused
                self.server.wake.pause()
                try:
                    text, spoken = self.server.stt.listen(str(data.get("lang") or "auto")[:4])
                    return self._send(200, {"text": text, "lang": spoken, "engine": "whisper"})
                finally:
                    if not was:
                        self.server.wake.resume()
            if path == "/api/wake":  # « Hey Ari » par sherpa-onnx : on / off / pause / resume
                w, action = self.server.wake, str(data.get("action"))
                if action == "on":
                    w.start()
                elif action == "off":
                    w.stop()
                elif action == "pause":
                    w.pause()
                elif action == "resume":
                    w.resume()
                elif action == "dump":
                    return self._send(200, {"file": w.dump(ROOT / ".ari" / "wake-dump.wav")})
                return self._send(200, w.status())
            if path == "/api/git/init":
                return self._send(200, ide.git_init(self.server.projects))
            if path == "/api/fireflies/key":
                from agentic_ari import fireflies_link as fl

                k = str(data.get("key") or "").strip()
                if not k:
                    return self._send(400, {"error": "clé vide"})
                fl.set_key(k)
                return self._send(200, {"ok": True, "configured": fl.is_configured()})
            if path == "/api/fireflies/ingest":
                from agentic_ari import fireflies_link as fl
                from agentic_ari.config import Config
                from agentic_ari.memory import Memory
                from agentic_ari.knowledge import Knowledge

                cfg = Config()
                mem = Memory(cfg.memory_path)
                kb = Knowledge(cfg.knowledge_path)
                if "transcript" in data and isinstance(data["transcript"], dict):
                    res = fl.ingest_meeting(data["transcript"], memory=mem, kb=kb)
                elif data.get("id") or data.get("transcript_id"):
                    res = fl.ingest_meeting(str(data.get("id") or data.get("transcript_id")), memory=mem, kb=kb)
                else:
                    return self._send(400, {"error": "identifiant ou données de transcription manquants"})
                return self._send(200, res)
            if path == "/api/approvals/resolve":
                from agentic_ari.config import Config
                from agentic_ari.memory import Memory

                aid = str(data.get("id") or data.get("approval_id") or "").strip()
                approved = data.get("approved", True)
                ok = Memory(Config().memory_path).resolve_approval_request(aid, approved=approved)
                return self._send(200, {"ok": ok, "approval_id": aid, "status": "approved" if approved else "rejected"})
            if path == "/api/sync/tontolo":
                from agentic_ari.config import Config
                from agentic_ari.memory import Memory

                res = Memory(Config().memory_path).sync_with_tontolo_db(data.get("path"))
                return self._send(200, res)
        except (ValueError, OSError, KeyError, RuntimeError) as e:
            return self._send(400, {"error": str(e)})
        if path == "/api/stop":
            return self._send(200, {"stopping": runner.stop()})
        if path == "/api/reset":
            if runner.current:
                return self._send(409, {"error": "Arrêtez la tâche en cours d'abord."})
            runner.reset()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "introuvable"})

    # ---------- helpers ----------
    def _static(self, rel):
        path = (UI_DIR / rel).resolve()
        if UI_DIR.resolve() not in path.parents or not path.is_file():
            return self._send(404, {"error": "introuvable"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        extra = {"Cache-Control": "public, max-age=604800"} if rel.startswith("vendor/") else {}
        self._send(
            200,
            path.read_bytes(),
            ctype,
            {**extra, "Content-Security-Policy": CSP, "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"},
        )

    def _models(self):
        """Modèles proposés : Ollama installés + serveurs locaux qui répondent (LM Studio, MLX…)."""
        from concurrent.futures import ThreadPoolExecutor
        from agentic_ari.local_servers import HELP, SERVERS, list_models

        def ollama():
            try:
                tags = httpx.get(f"{Config().ollama_host}/api/tags", timeout=2).json()
                return [m["name"] for m in tags.get("models", []) if "embed" not in m["name"]]
            except Exception:
                return []

        with ThreadPoolExecutor(2) as ex:
            o, srv = ex.submit(ollama), ex.submit(list_models)
            installed, servers = o.result(), srv.result()
        return {
            "current": self.server.projects.model,
            "ollama": installed,
            "servers": servers,
            "offline": {k: HELP[k] for k in SERVERS if k not in servers},
        }

    def _souvenirs(self):
        from agentic_ari.souvenirs import Souvenirs

        return Souvenirs(Config())

    def _status(self):
        cfg = self.server.projects.apply(Config())
        try:
            tags = httpx.get(f"{cfg.ollama_host}/api/tags", timeout=3).json()
            installed = [m["name"] for m in tags.get("models", [])]
            ollama = {"ok": True, "models": installed}
        except Exception as e:
            ollama = {"ok": False, "error": str(e)}
        return {
            **self.server.runner.status(),
            "clients": self.server.bus.clients(),
            "ollama": ollama,
            "models": cfg.models,
            "version": __version__,
            "seq": self.server.bus.last_seq(),
            "project": self.server.projects.info(),
            "health": self.server.health,
        }

    def _stream(self, qs):
        client_id = (qs.get("client") or ["anonyme"])[0][:64]
        caps = [c for c in (qs.get("caps") or [""])[0].split(",") if c in ("tab", "voice")]
        try:
            since = int((qs.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.close_connection = True
        client = self.server.bus.connect(client_id, caps, since)
        try:
            self.wfile.write(b": connecte\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = client.queue.get(timeout=PING_EVERY)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev is None:  # remplacé par une nouvelle connexion du même client
                    return
                if ev["type"] in ("tab", "listen") and ev.get("target") != client.id:
                    continue
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.bus.disconnect(client)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__((HOST, PORT), Handler)
        self.token = load_token()
        self.bus = Bus()
        self.projects = Projects()
        self.runner = Runner(self.bus, self.projects)
        self.terminal = ide.Terminal(self.projects, self.bus)
        self.wake = SherpaWake(self.bus)
        self.stt = LocalSTT(self.bus)
        self.tts = PiperTTS()
        from .tts import Voices

        self.voices = Voices(self.tts, prefs=lambda: self.projects.state.get("voices") or {})
        threading.Thread(target=self.tts.warm, daemon=True).start()
        self.watching = WatchRunner(self.bus, self.projects, busy=lambda: bool(self.runner.current))
        self.health = []  # diagnostic du cœur (agentic_ari.doctor) : au démarrage, puis toutes les 10 min
        threading.Thread(target=self._health_loop, daemon=True, name="doctor").start()

    def _health_loop(self):
        time.sleep(8)  # le pont doit déjà écouter (le diagnostic le vérifie)
        while True:
            try:
                from agentic_ari.doctor import doctor

                self.health = [{"ok": bool(ok), "name": n, "detail": d} for ok, n, d in doctor(fix=False)]
            except Exception as e:
                self.health = [{"ok": False, "name": "Diagnostic", "detail": f"{type(e).__name__} : {e}"}]
            self.bus.emit("health", items=self.health)
            # un problème (réseau coupé au réveil du Mac…) : on revérifie vite pour retirer la pastille dès que ça revient
            time.sleep(60 if any(not h["ok"] for h in self.health) else 600)


def is_running(timeout=1.5):
    """Vrai si un pont répond déjà sur le port (pour ne jamais en lancer un second)."""
    try:
        return httpx.get(f"http://{LOCAL}:{PORT}/api/health", timeout=timeout).json().get("app") == "agentic-ari"
    except Exception:
        return False


def start_in_thread():
    """Démarre le pont dans un fil d'arrière-plan d'un autre processus (ex. le service permanent).

    Renvoie le serveur (appeler `.shutdown()` pour l'arrêter), ou None si un pont tourne déjà.
    """
    import threading

    if is_running():
        return None
    srv = BridgeServer()
    threading.Thread(target=srv.serve_forever, daemon=True, name="ari-bridge").start()
    return srv


def watch_parent():
    """Lancé par l'app Mac (ARI_PARENT_PID) : s'arrête si l'app disparaît, même tuée brutalement,
    pour ne jamais laisser un pont orphelin sans les autorisations macOS de l'app."""
    parent = os.getenv("ARI_PARENT_PID")
    if not parent or not parent.isdigit():
        return
    import threading
    import time

    def run():
        while True:
            time.sleep(2)
            if os.getppid() != int(parent):
                os._exit(0)

    threading.Thread(target=run, daemon=True, name="parent-watch").start()


def _start_mdns():
    """Diffuse le service Bonjour / mDNS sur macOS (_agenticari._tcp) pour la découverte Android."""
    if not LAN or sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(
            ["dns-sd", "-R", "Agentic Ari", "_agenticari._tcp", "local", str(PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None


def serve():
    if is_running():
        print(f"Le pont tourne déjà sur http://{LOCAL}:{PORT}.")
        return 0
    try:
        srv = BridgeServer()
    except OSError as e:
        print(f"Impossible d'écouter sur {HOST}:{PORT} ({e}). Le pont tourne peut-être déjà.", file=sys.stderr)
        return 1
    watch_parent()
    print(f"Agentic Ari — pont local sur http://{LOCAL}:{PORT}  (clé : {TOKEN_FILE})", flush=True)
    mdns_proc = None
    if LAN:
        print(
            "Accès mobile ACTIVÉ : le pont écoute aussi sur le réseau local (clé obligatoire). "
            "Désactiver : python -m ari_bridge mobile off",
            flush=True,
        )
        mdns_proc = _start_mdns()
        if mdns_proc:
            print("Découverte Bonjour / mDNS activée (_agenticari._tcp sur le réseau local).", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if mdns_proc:
            try:
                mdns_proc.terminate()
            except Exception:
                pass
        srv.server_close()
    return 0

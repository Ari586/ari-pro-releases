"""Outils « IDE » du pont : enregistrement de fichiers, points de restauration, terminal et Git.

Tout reste confiné au dossier projet ouvert (même contrôle de chemin que l'explorateur de fichiers).
- Points de restauration : avant chaque modification de fichier acceptée par l'utilisateur, l'ancienne
  version est copiée dans .ari/checkpoints/ ; « Annuler » la remet en place.
- Terminal : commandes tapées par l'utilisateur, exécutées dans le dossier projet, sortie en direct.
- Git : état, différences, commit, initialisation.
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid

from agentic_ari.config import ROOT

CHECKPOINT_DIR = ROOT / ".ari" / "checkpoints"
MAX_SAVE = 5_000_000
TERM_LIMIT = 400_000   # caractères de sortie max par commande envoyés à l'interface


def diff_target(text):
    """Chemin du fichier visé par un diff unifié (« +++ b/chemin »), ou None."""
    m = re.search(r"^\+\+\+ (?:b/)?(.+?)\s*$", text or "", re.M)
    if not m or m.group(1) == "/dev/null":
        return None
    return m.group(1).strip()


class Checkpoints:
    def __init__(self, projects, bus):
        self.projects, self.bus = projects, bus
        self.lock = threading.Lock()

    def _dir(self):
        root = self.projects.current
        if not root:
            return None
        key = hashlib.sha1(str(root).encode()).hexdigest()[:12]
        return CHECKPOINT_DIR / key

    def create(self, rel, job_id=None, reason=""):
        """Sauvegarde l'état actuel de `rel` (ou son absence) avant qu'Ari ne le modifie."""
        base = self._dir()
        if not base:
            return None
        try:
            root, path = self.projects._inside(rel)
        except ValueError:
            return None
        cid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        folder = base / cid
        folder.mkdir(parents=True, exist_ok=True)
        existed = path.is_file()
        if existed:
            (folder / "before").write_bytes(path.read_bytes())
        meta = {"id": cid, "file": str(path.relative_to(root)), "existed": existed, "job": job_id,
                "time": time.time(), "reason": reason[:200]}
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        self.bus.emit("checkpoint", **meta)
        return meta

    def list(self, limit=50):
        base = self._dir()
        if not base or not base.exists():
            return []
        out = []
        for d in sorted(base.iterdir(), reverse=True)[:limit]:
            try:
                out.append(json.loads((d / "meta.json").read_text()))
            except (OSError, ValueError):
                continue
        return out

    def restore(self, cid):
        """Remet le fichier dans l'état d'avant la modification (le supprime s'il n'existait pas)."""
        base = self._dir()
        if not base or not re.fullmatch(r"[\w-]+", cid or ""):
            raise ValueError("point de restauration inconnu")
        folder = base / cid
        meta = json.loads((folder / "meta.json").read_text())
        root, path = self.projects._inside(meta["file"])
        with self.lock:
            # on garde aussi la version actuelle, pour pouvoir annuler l'annulation
            self.create(meta["file"], reason=f"avant annulation de {cid}")
            if meta["existed"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((folder / "before").read_bytes())
            elif path.exists():
                path.unlink()
        self.bus.emit("restored", id=cid, file=meta["file"])
        return meta


def save_file(projects, rel, content):
    """Enregistrement par l'utilisateur depuis l'éditeur (pas d'accord à demander : c'est lui qui écrit)."""
    root, path = projects._inside(rel)
    if path.is_dir():
        raise ValueError("c'est un dossier")
    data = str(content).encode("utf-8")
    if len(data) > MAX_SAVE:
        raise ValueError("fichier trop volumineux")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": str(path.relative_to(root)), "size": len(data)}


class Terminal:
    """Une commande à la fois dans le dossier projet, sortie diffusée en direct (événements « term »)."""

    def __init__(self, projects, bus):
        self.projects, self.bus = projects, bus
        self.proc = None
        self.lock = threading.Lock()

    def run(self, command):
        root = self.projects.current
        if not root:
            raise ValueError("ouvrez d'abord un projet")
        with self.lock:
            if self.proc and self.proc.poll() is None:
                raise ValueError("une commande tourne déjà (arrêtez-la avec ■)")
            env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", ""),
                       TERM="dumb", PYTHONUNBUFFERED="1", CLICOLOR="0")
            self.proc = subprocess.Popen(["/bin/zsh", "-lc", command], cwd=root, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                         start_new_session=True)
        tid = uuid.uuid4().hex[:8]
        self.bus.emit("term", id=tid, kind="start", command=command, cwd=str(root))
        threading.Thread(target=self._pump, args=(self.proc, tid), daemon=True).start()
        return tid

    def _pump(self, proc, tid):
        sent = 0
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            if sent < TERM_LIMIT:
                self.bus.emit("term", id=tid, kind="out", text=line[: TERM_LIMIT - sent])
                sent += len(line)
        code = proc.wait()
        self.bus.emit("term", id=tid, kind="end", code=code)

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            return True
        return False


def _git(root, *args, timeout=20):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def git_status(projects):
    root = projects.current
    if not root:
        raise ValueError("ouvrez d'abord un projet")
    code, out, err = _git(root, "status", "--porcelain=v1", "-b", "--untracked-files=all")
    if code != 0:
        return {"repo": False, "error": err.strip()[:300]}
    lines = out.splitlines()
    branch = lines[0][3:] if lines and lines[0].startswith("##") else ""
    files = [{"status": l[:2].strip() or "?", "path": l[3:].split(" -> ")[-1].strip('"')} for l in lines[1:]]
    _, log, _ = _git(root, "log", "-5", "--pretty=format:%h %s (%cr)")
    return {"repo": True, "branch": branch, "files": files[:500], "log": log.splitlines()}


def git_diff(projects, rel):
    root, path = projects._inside(rel)
    code, out, err = _git(root, "diff", "HEAD", "--", str(path.relative_to(root)))
    if code != 0 or not out:   # fichier non suivi (ou dépôt sans commit) : tout est « ajouté »
        code2, out2, _ = _git(root, "diff", "--no-index", "--", "/dev/null", str(path.relative_to(root)))
        out = out2 or out
    return {"path": str(path.relative_to(root)), "diff": out[:400_000]}


def git_commit(projects, message):
    root = projects.current
    if not root:
        raise ValueError("ouvrez d'abord un projet")
    message = (message or "").strip()
    if not message:
        raise ValueError("message de commit vide")
    _git(root, "add", "-A")
    code, out, err = _git(root, "commit", "-m", message)
    if code != 0:
        raise ValueError((err or out).strip()[:400])
    return {"ok": True, "output": out.strip()[:400]}


def git_init(projects):
    root = projects.current
    if not root:
        raise ValueError("ouvrez d'abord un projet")
    code, out, err = _git(root, "init")
    if code != 0:
        raise ValueError(err.strip()[:300])
    return {"ok": True}

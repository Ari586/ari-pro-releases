"""Dossier projet et modèle choisis dans l'interface, retenus entre les lancements.

Le dossier choisi devient le workspace des agents (`cfg.workspace`) : ils y lisent et modifient les
fichiers, avec les mêmes garde-fous que le cœur (chemins confinés au dossier, confirmations).
Par prudence, seul un dossier situé dans votre dossier personnel est accepté.
"""
import json
import threading
from pathlib import Path

from agentic_ari.config import ROOT

STATE_FILE = ROOT / ".ari" / "bridge_state.json"
HOME = Path.home().resolve()
FORBIDDEN = [HOME / "Library", HOME / ".ssh", HOME / ".Trash"]
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".DS_Store", ".idea", ".mypy_cache",
        ".pytest_cache", "dist", "build", ".next"}
MAX_FILE = 400_000
MAX_ENTRIES = 600
LANG = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".json": "json",
        ".md": "markdown", ".html": "html", ".css": "css", ".sh": "shell", ".swift": "swift", ".yml": "yaml",
        ".yaml": "yaml", ".toml": "toml", ".txt": "texte", ".sql": "sql", ".rs": "rust", ".go": "go", ".java": "java"}
MODELS = ("auto", "gemma", "qwen", "apple", "gemma-mlx")


class Projects:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"current": None, "recent": [], "model": "auto"}
        try:
            self.state.update(json.loads(STATE_FILE.read_text()))
        except (OSError, ValueError):
            pass
        if self.state.get("current") and not Path(self.state["current"]).is_dir():
            self.state["current"] = None

    def _save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    # ---------- état ----------
    @property
    def current(self):
        return Path(self.state["current"]) if self.state.get("current") else None

    @property
    def model(self):
        return self.state.get("model", "auto")

    def info(self):
        cur = self.current
        return {"current": str(cur) if cur else None, "name": cur.name if cur else None,
                "recent": [r for r in self.state.get("recent", []) if Path(r).is_dir()],
                "model": self.model, "home": str(HOME)}

    @staticmethod
    def check_dir(path):
        s = str(path)
        if s.startswith("file://"):
            s = s[7:]
        p = Path(s).expanduser().resolve()
        if not p.is_dir():
            raise ValueError(f"dossier introuvable : {s}")
        if p == HOME or p == Path("/"):
            raise ValueError("Choisissez un sous-dossier de projet (pas la racine du système ni votre dossier personnel complet).")
        sys_roots = [Path("/System"), Path("/Library"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/etc"), Path("/var"), Path("/private")]
        if any(p == f or f in p.parents for f in sys_roots + FORBIDDEN):
            raise ValueError("Ce dossier système est protégé.")
        return p

    def set_project(self, path):
        with self.lock:
            if not path:
                self.state["current"] = None
            else:
                p = self.check_dir(path)
                self.state["current"] = str(p)
                recent = [str(p)] + [r for r in self.state.get("recent", []) if r != str(p)]
                self.state["recent"] = recent[:8]
            self._save()
        return self.info()

    def set_model(self, model):
        from agentic_ari.local_servers import is_local_model
        model = str(model).strip()
        if not (model in MODELS or is_local_model(model) or (model.startswith("ollama:") and len(model) > 7)):
            raise ValueError(f"modèle inconnu : {model} (auto, gemma, qwen, apple, ollama:…, lmstudio:…, mlx:…, llamacpp:…, openai:…)")
        with self.lock:
            self.state["model"] = model
            self._save()
        return self.info()

    def apply(self, cfg):
        """Applique dossier et modèle à une Config du cœur, juste avant une tâche."""
        if self.current:
            cfg.workspace = self.current
        gemma, qwen = cfg.models["coordinator"], cfg.models["coder"]
        if self.model == "gemma":
            cfg.models = {k: gemma for k in cfg.models}
        elif self.model == "gemma-mlx":   # Gemma 4 via MLX (serveur mlx_lm), sans Ollama
            cfg.apply(model="gemma-mlx")
        elif self.model == "apple":   # Apple Intelligence (via le cœur) : lent pour piloter, vision sur Gemma
            cfg.apply(model="apple")
        elif self.model == "qwen":   # Qwen n'a pas de vision : les captures restent analysées par Gemma
            cfg.models = {k: (gemma if k == "vision" else qwen) for k in cfg.models}
        elif self.model.startswith("ollama:"):   # un autre modèle Ollama installé
            cfg.apply(model=self.model[len("ollama:"):])
        elif self.model not in MODELS:   # serveur local : LM Studio, MLX, llama.cpp, compatible OpenAI
            cfg.apply(model=self.model)
        return cfg

    # ---------- exploration ----------
    def list_dirs(self, path=None):
        """Sous-dossiers d'un dossier (pour le sélecteur de projet)."""
        if path:
            s = str(path)
            if s.startswith("file://"):
                s = s[7:]
            base = Path(s).expanduser().resolve()
        else:
            base = HOME
        if not base.is_dir():
            base = HOME
        dirs = []
        try:
            for child in sorted(base.iterdir(), key=lambda c: c.name.lower()):
                if child.is_dir() and not child.name.startswith(".") and child not in FORBIDDEN:
                    dirs.append({"name": child.name, "path": str(child)})
        except OSError as e:
            raise ValueError(f"lecture impossible : {e}")
        parent = str(base.parent) if base != Path("/") else None
        return {"path": str(base), "parent": parent, "dirs": dirs[:MAX_ENTRIES]}

    def _inside(self, rel):
        root = self.current
        if not root:
            raise ValueError("aucun projet ouvert")
        p = (root / (rel or ".")).resolve()
        if p != root and root not in p.parents:
            raise ValueError("chemin hors du projet")
        return root, p

    def tree(self, rel=""):
        root, base = self._inside(rel)
        if not base.is_dir():
            raise ValueError("ce n'est pas un dossier")
        out = []
        for child in sorted(base.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if child.name in SKIP or child.name.startswith(".") and child.name not in (".env.example", ".gitignore"):
                continue
            entry = {"name": child.name, "path": str(child.relative_to(root)), "dir": child.is_dir()}
            if not entry["dir"]:
                try:
                    entry["size"] = child.stat().st_size
                except OSError:
                    entry["size"] = 0
            out.append(entry)
            if len(out) >= MAX_ENTRIES:
                break
        return {"path": str(base.relative_to(root)) if base != root else "", "entries": out}

    def read(self, rel):
        root, p = self._inside(rel)
        if not p.is_file():
            raise ValueError("fichier introuvable")
        size = p.stat().st_size
        info = {"path": str(p.relative_to(root)), "name": p.name, "size": size,
                "lang": LANG.get(p.suffix.lower(), p.suffix.lstrip(".") or "texte")}
        if size > MAX_FILE:
            return {**info, "text": None, "note": "fichier trop volumineux pour l'aperçu"}
        raw = p.read_bytes()
        if b"\0" in raw[:4096]:
            return {**info, "text": None, "note": "fichier binaire"}
        text = raw.decode("utf-8", errors="replace")
        return {**info, "text": text, "lines": text.count("\n") + 1}

"""Interface terminal façon Claude Code, branchée sur le pont local : ./ari-code

Elle partage la conversation avec l'app Mac et l'extension (même serveur, mêmes tâches).
Si le pont ne tourne pas, elle le démarre en arrière-plan (il continue après la sortie,
comme le service permanent).
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

import httpx
from rich.console import Console
from rich.table import Table
from rich.markdown import Markdown
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import SPINNERS
from rich.text import Text

from agentic_ari.config import ROOT

from .server import HOST, PORT, TOKEN_FILE, is_running

try:
    import readline  # noqa: F401  (flèches et historique dans input())
except ImportError:
    pass

BASE = f"http://{HOST}:{PORT}"
ACCENT = "#d97757"
SPINNERS["ari"] = {"interval": 120, "frames": "·✢✳✶✻✽✻✶✳✢"}
MODES = {
    "ari": "un seul cerveau : planifie, choisit les bons agents, vérifie (par défaut)",
    "team": "plan → agents → revue → critique",
    "coder": "Pair-programmer autonome (code, test, auto-correction)",
    "build": "écrit, exécute et teste du code",
    "research": "web + base locale",
    "browser": "Chrome piloté en arrière-plan",
    "review": "contrôle qualité",
    "system": "Mac, mémoire, compétences, tâches planifiées, MCP",
    "apps": "pilote les applications du Mac",
    "tab": "agit dans l'onglet ouvert (extension Chrome/Comet connectée)",
    "trader": "Trader pro & analyste quantitatif (multi-timeframe, risk management, crypto/actions/forex)",
}
STEP = {"PLAN": "Plan", "ROUTE": "Délégation", "OBSERVE": "Résultat", "CRITIQUE": "Critique",
        "coordinator": "Coordinateur", "info": "Info"}
COLORS = {"PLAN": ACCENT, "ROUTE": ACCENT, "coordinator": ACCENT, "CRITIQUE": "red", "research": "#7fb2ef",
          "review": "yellow", "OBSERVE": "grey50", "info": "grey50", "system": "#7fb2ef", "coder": "#a855f7", "trader": "#10b981"}
HELP = "\n".join([
    "[bold]Commandes[/]",
    "  /mode \\[nom]   changer de mode : " + ", ".join(MODES),
    "  /projet \\[dossier]   ouvrir un dossier projet (sans argument : afficher, « aucun » : fermer)",
    "  /modele \\[auto|gemma|qwen|apple]   choisir le modèle",
    "  /auto         activer / désactiver les confirmations",
    "  /undo         annuler la dernière modification de code",
    "  /clear        nouvelle conversation",
    "  /statut       état du pont et d'Ollama",
    "  /quitter      sortir (ctrl+d)",
    "  ctrl+c        arrêter la tâche en cours",
    "",
    "[bold]Modes[/]",
    *[f"  {k:<9} [dim]{v}[/]" for k, v in MODES.items()],
])


class Bridge:
    def __init__(self):
        self.token = TOKEN_FILE.read_text().strip()
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.client_id = f"tui-{os.getpid()}"
        self.events = queue.Queue()

    def get(self, path):
        return httpx.get(BASE + path, headers=self.headers, timeout=10).json()

    def post(self, path, body=None):
        r = httpx.post(BASE + path, headers=self.headers, json=body or {}, timeout=30)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(data.get("error", f"erreur {r.status_code}"))
        return data

    def listen(self, since):
        """Fil d'arrière-plan : lit le flux d'événements et le range dans la file."""
        def run():
            seq = since
            while True:
                try:
                    url = f"{BASE}/api/stream?client={self.client_id}&since={seq}"
                    with httpx.stream("GET", url, headers=self.headers, timeout=None) as r:
                        for line in r.iter_lines():
                            if line.startswith("data: "):
                                ev = json.loads(line[6:])
                                seq = max(seq, ev.get("seq", 0))
                                self.events.put(ev)
                except Exception:
                    self.events.put({"type": "offline"})
                    time.sleep(2)
        threading.Thread(target=run, daemon=True).start()


def ensure_bridge(console):
    if is_running():
        return True
    log = open(ROOT / ".ari" / "bridge.log", "ab")
    subprocess.Popen([sys.executable, "-m", "ari_bridge"], cwd=ROOT, stdout=log, stderr=log,
                     start_new_session=True)
    with console.status("Démarrage du pont local…", spinner="ari", spinner_style=ACCENT):
        for _ in range(60):
            if is_running():
                return True
            time.sleep(0.5)
    console.print(f"[red]⏺ Le pont local ne démarre pas. Journal : {ROOT / '.ari' / 'bridge.log'}[/]")
    return False


class TUI:
    def __init__(self):
        self.console = Console(highlight=False)
        self.mode = "ari"
        self.auto = False
        self.bridge = None

    # ---------- affichage ----------
    def welcome(self, status):
        models = status.get("models", {})
        ollama = status.get("ollama", {})
        body = Text.from_markup(
            f"[{ACCENT}]✻[/] Bienvenue dans [bold]Agentic Ari[/] !\n\n"
            f"  [dim]/aide pour l'aide · /mode pour changer de mode[/]\n"
            f"  [dim]modèles : {models.get('coordinator', '?')} · {models.get('coder', '?')} · 100 % local[/]\n"
            f"  [dim]cwd : {str(ROOT).replace(os.path.expanduser('~'), '~')}[/]")
        self.console.print(Panel(body, border_style=ACCENT, expand=False, padding=(0, 2)))
        if not ollama.get("ok"):
            self.console.print("[yellow]⏺ Ollama ne répond pas : ouvrez l'app Ollama ou lancez `ollama serve`.[/]")
        self.console.print()

    def footer(self):
        auto = "[yellow]⏵⏵ sans confirmation[/]" if self.auto else "[dim]⏵⏵ confirmations activées[/]"
        self.console.print(f"  [{ACCENT}]⏵ {self.mode}[/] · {auto} [dim]· /aide[/]")

    def step(self, role, text):
        name, arg, out = STEP.get(role, role), "", ""
        if role == "ROUTE" and text.startswith("→"):
            agent, _, task = text[1:].partition(":")
            name, out = f"Délégation({agent.strip()})", task.strip()
        elif role == "OBSERVE" and "✔" in text:
            agent, _, res = text.partition("✔")
            name, out = f"Résultat({agent.strip()})", res.strip()
        elif role in STEP or role.isupper():   # étapes du coordinateur (PLAN, MÉMOIRE…)
            name, out = STEP.get(role, role.capitalize()), text
        elif "(" in text and text.endswith(")") and text.split("(")[0].isidentifier():
            tool, _, rest = text.partition("(")
            name = tool if tool.startswith(role) else f"{role} · {tool}"
            arg = "" if rest == "{})" or rest == ")" else "(" + rest
        else:
            name, out = f"{role} · {'Erreur' if text.startswith('✗') else 'note'}", text.lstrip("✗ ")
        color = COLORS.get(role, "green")
        head = Text.assemble(("⏺ ", color), (name, "bold"), (f" {arg[:80]}{'…' if len(arg) > 80 else ''}", "dim"))
        self.console.print(head)
        if out:
            lines = out.strip().split("\n")
            shown = lines[0][:80]
            extra = len(lines) - 1
            txt = Text("  ⎿  ", style="dim")
            txt.append(shown, style="dim")
            if extra > 0 or len(lines[0]) > 80:
                txt.append(" …", style="dim italic")
            self.console.print(txt)

    def answer(self, text):
        self.console.print()
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(Text("⏺", style="bold"), Markdown(text))
        self.console.print(grid)

    # ---------- interaction ----------
    def confirm(self, ev):
        self.console.print(Panel(
            Text.from_markup(f"[bold {'red' if ('ARGENT RÉEL' in ev['text'] or 'COMMANDE DESTRUCTRICE' in ev['text']) else 'yellow'}]Autorisation requise[/]\n\n{escape(ev['text'])}\n\n"
                             f"[dim]Voulez-vous continuer ?[/]\n  [{ACCENT}]1. Oui, autoriser[/]\n  2. Non, refuser"),
            border_style="red" if ("ARGENT RÉEL" in ev["text"] or "COMMANDE DESTRUCTRICE" in ev["text"]) else "yellow", expand=False, padding=(0, 2)))
        while True:
            try:
                ans = self.console.input(f"[{ACCENT}]❯[/] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "2"
            if ans in ("1", "o", "oui", "y", "yes", "2", "n", "non", "no"):
                break
        try:
            self.bridge.post("/api/confirm", {"id": ev["id"], "ok": ans in ("1", "o", "oui", "y", "yes")})
        except Exception as e:
            self.console.print(f"[red]⏺ {e}[/]")

    def follow(self, job_id):
        """Affiche le déroulé d'une tâche jusqu'à sa fin. ctrl+c = arrêt, deux fois = détacher."""
        steps, stop_sent = 0, False
        started = time.time()
        status = self.console.status(f"[{ACCENT}]Réflexion…[/] [dim](ctrl+c pour arrêter)[/]",
                                     spinner="ari", spinner_style=ACCENT)
        status.start()
        try:
            while True:
                try:
                    ev = self.bridge.events.get(timeout=0.5)
                except queue.Empty:
                    s = int(time.time() - started)
                    status.update(f"[{ACCENT}]Réflexion…[/] [dim]({s} s · ctrl+c pour arrêter)[/]")
                    continue
                except KeyboardInterrupt:
                    if stop_sent:
                        self.console.print("[dim]Détaché : la tâche continue sur le pont.[/]")
                        return
                    stop_sent = True
                    self.bridge.post("/api/stop")
                    self.console.print("[dim]⏺ Arrêt demandé : Ari s'arrête après l'étape en cours (ctrl+c encore pour détacher).[/]")
                    continue
                if ev.get("job") not in (job_id, None):
                    continue
                t = ev["type"]
                if t == "log":
                    steps += 1
                    status.stop()
                    self.step(ev["role"], ev["text"])
                    status.start()
                elif t == "confirm":
                    status.stop()
                    self.confirm(ev)
                    status.start()
                elif t == "confirm_done":
                    status.stop()
                    self.console.print("  [green]✓ Autorisé[/]" if ev.get("ok") else "  [red]✗ Refusé[/]")
                    status.start()
                elif t == "answer":
                    status.stop()
                    self.answer(ev["text"])
                elif t == "error":
                    status.stop()
                    self.console.print(f"[red]⏺ {escape(ev['text'])}[/]")
                elif t == "offline":
                    status.update("[yellow]Pont local injoignable, reconnexion…[/]")
                elif t == "job" and ev.get("state") == "end":
                    status.stop()
                    self.console.print(f"\n[dim]✻ {steps} étape(s) en {ev.get('seconds', '?')} s[/]\n")
                    return
        finally:
            status.stop()

    def command(self, line):
        name, _, arg = line[1:].strip().partition(" ")
        name, arg = name.lower(), arg.strip().lower()
        if name in ("aide", "help", "?"):
            self.console.print(Padding(Text.from_markup(HELP), (0, 2)))
        elif name == "mode":
            if not arg:
                names = list(MODES)
                self.mode = names[(names.index(self.mode) + 1) % len(names)]
            elif arg in ("ari", "coder", "codeur"):
                self.mode = "coder"
            elif arg in MODES:
                self.mode = arg
            else:
                return self.console.print(f"[red]⏺ Mode inconnu : {arg}[/] [dim]({', '.join(MODES)})[/]")
            self.console.print(f"[dim]⏺ mode {self.mode} : {MODES[self.mode]}[/]")
        elif name == "auto":
            self.auto = not self.auto
            self.console.print("[yellow]⏺ Sans confirmation : Ari agit sans vous demander.[/]" if self.auto
                               else "[dim]⏺ Confirmations activées.[/]")
        elif name in ("clear", "nouveau"):
            try:
                self.bridge.post("/api/reset")
                self.console.clear()
                self.welcome(self.bridge.get("/api/status"))
            except Exception as e:
                self.console.print(f"[red]⏺ {e}[/]")
        elif name in ("undo", "annuler"):
            try:
                res = self.bridge.post("/api/ask", {"text": "Annule immédiatement la dernière modification de code avec l'outil undo_last_edit.", "mode": "coder", "auto": True})
                job_id = res.get("job")
                if job_id:
                    self.follow(job_id)
            except Exception as e:
                self.console.print(f"[red]⏺ {e}[/]")
        elif name in ("projet", "project"):
            try:
                raw = line[1:].strip().partition(" ")[2].strip()
                info = (self.bridge.get("/api/project") if not raw else
                        self.bridge.post("/api/project", {"path": None if raw.lower() == "aucun" else raw}))
                self.console.print(f"[dim]⏺ projet : {info['current'] or 'aucun (workspace par défaut)'}[/]")
            except Exception as e:
                self.console.print(f"[red]⏺ {e}[/]")
        elif name in ("modele", "modèle", "model"):
            try:
                info = self.bridge.post("/api/model", {"model": arg}) if arg else self.bridge.get("/api/project")
                self.console.print(f"[dim]⏺ modèle : {info['model']}[/]")
            except Exception as e:
                self.console.print(f"[red]⏺ {e}[/]")
        elif name in ("statut", "status"):
            s = self.bridge.get("/api/status")
            o = s["ollama"]
            self.console.print(f"[dim]⏺ pont v{s['version']} · {'occupé' if s['busy'] else 'libre'} · "
                               f"Ollama {'ok (' + str(len(o['models'])) + ' modèles)' if o['ok'] else 'arrêté'} · "
                               f"interfaces connectées : {', '.join(c['id'] for c in s['clients']) or 'aucune'}[/]")
        elif name in ("quitter", "exit", "quit", "q"):
            raise EOFError
        else:
            self.console.print(f"[red]⏺ Commande inconnue : /{name}[/] [dim](/aide)[/]")

    def run(self, first=None):
        if not ensure_bridge(self.console):
            return 1
        self.bridge = Bridge()
        status = self.bridge.get("/api/status")
        self.bridge.listen(status.get("seq", 0))
        self.welcome(status)
        if status.get("busy"):
            self.console.print("[dim]⏺ Une tâche lancée ailleurs est en cours : je l'affiche.[/]")
            self.follow(status["job"]["id"])
        pending = first
        while True:
            try:
                if pending:
                    line, pending = pending, None
                    self.console.print(f"[dim]>[/] {escape(line)}")
                else:
                    self.console.rule(style="grey35")
                    self.footer()
                    line = self.console.input("[bold]>[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]À bientôt.[/]")
                return 0
            if not line:
                continue
            if line.startswith("/"):
                try:
                    self.command(line)
                except EOFError:
                    self.console.print("[dim]À bientôt.[/]")
                    return 0
                continue
            try:
                job = self.bridge.post("/api/ask", {"text": line, "mode": self.mode, "auto": self.auto,
                                                    "client": self.bridge.client_id})
            except Exception as e:
                self.console.print(f"[red]⏺ {e}[/]")
                continue
            self.follow(job["job"])


def main():
    first = " ".join(sys.argv[1:]).strip() or None
    return TUI().run(first)


if __name__ == "__main__":
    sys.exit(main())

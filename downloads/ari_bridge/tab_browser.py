"""Navigateur « onglet réel » : mêmes outils que `agentic_ari.browser.Browser`, mais exécutés par
l'extension dans l'onglet ouvert de l'utilisateur (Chrome ou Comet) au lieu d'un Chrome piloté.

Le sous-agent browser du cœur l'utilise sans modification : seules les méthodes `browser_*`,
`SCHEMAS` et `close` sont nécessaires.
"""
import base64
import re
import tempfile
from pathlib import Path

from agentic_ari.browser import RISKY, Browser

from .bus import Cancelled


class TabBrowser:
    SCHEMAS = Browser.SCHEMAS

    def __init__(self, cfg, llm, console, bus, job):
        self.cfg, self.llm, self.console, self.bus, self.job = cfg, llm, console, bus, job
        self.shots = Path(tempfile.mkdtemp(prefix="ari-tab-shots-"))

    # ---------- transport vers l'extension ----------
    def _call(self, action, timeout=25, **args):
        target = self.bus.tab_client(self.job.client)
        if not target:
            raise RuntimeError("extension non connectée : ouvrez le panneau Agentic Ari dans Chrome ou Comet")
        answered, value = self.bus.request("tab", timeout, cancelled=self.job.is_cancelled,
                                           job=self.job.id, target=target, action=action, args=args,
                                           tab_id=self.job.tab_id)
        if not answered:
            raise RuntimeError(f"l'extension n'a pas répondu ({action})")
        value = value or {}
        if value.get("error"):
            raise RuntimeError(value["error"])
        return value

    def _confirm(self, what):
        if not self.cfg.confirm_commands or not self.console:
            return True
        ans = self.console.input(f"Action irréversible possible dans votre onglet : {what}")
        return ans.strip().lower() in ("y", "o", "yes", "oui")

    def close(self):
        pass  # l'onglet appartient à l'utilisateur : on ne ferme rien

    # ---------- outils (mêmes noms et formats que le navigateur du cœur) ----------
    def browser_open(self, url):
        if not re.match(r"^https?://", url):
            url = "https://" + url
        self._call("open", timeout=45, url=url)
        return self.browser_snapshot()

    def browser_snapshot(self, max_chars=3500):
        s = self._call("snapshot")
        text = re.sub(r"\n\s*\n+", "\n", s.get("text") or "").strip()
        if len(text) > int(max_chars):
            text = text[:int(max_chars)] + "\n…[texte tronqué]"
        hints, elements = s.get("hints"), s.get("elements")
        return (f"URL : {s.get('url')}\nTitre : {s.get('title')}\n\n--- Texte visible ---\n{text}\n\n"
                + (f"--- Indices non visibles en texte (notes, images…) ---\n{hints}\n\n" if hints else "")
                + f"--- Éléments interactifs (utilise le numéro) ---\n{elements or '(aucun)'}")

    def browser_click(self, ref):
        info = self._call("describe", ref=int(ref))
        label = info.get("label") or ""
        if RISKY.search(label) and not self._confirm(f"cliquer [{ref}] « {label.strip()[:60]} »"):
            return "Clic refusé par l'utilisateur."
        self._call("click", timeout=40, ref=int(ref))
        return "Cliqué.\n\n" + self.browser_snapshot()

    def browser_type(self, ref, text, submit=False):
        info = self._call("describe", ref=int(ref))
        if info.get("password"):
            return "Refusé : l'agent ne tape jamais de mot de passe. Connectez-vous vous-même dans l'onglet."
        submit = submit in (True, "true", "True", 1)
        if submit and not self._confirm(f"taper « {str(text)[:60]} » dans [{ref}] puis Entrée"):
            return "Envoi refusé par l'utilisateur."
        self._call("type", timeout=40, ref=int(ref), text=str(text), submit=submit)
        if submit:
            return "Tapé et envoyé.\n\n" + self.browser_snapshot()
        return f"Tapé dans [{ref}]."

    def browser_select(self, ref, value):
        self._call("select", ref=int(ref), value=str(value))
        return f"Option « {value} » choisie dans [{ref}]."

    def browser_scroll(self, direction="down"):
        self._call("scroll", direction="up" if direction == "up" else "down")
        return self.browser_snapshot()

    def browser_back(self):
        self._call("back", timeout=40)
        return self.browser_snapshot()

    def browser_extract(self):
        out = self._call("extract").get("text")
        return out or "Aucune liste répétée détectée : utilise browser_snapshot."

    def browser_look(self, question="Décris cette page : à quoi elle sert, sa mise en page, ce qui ressort."):
        shot = self._call("screenshot", timeout=30)
        data = (shot.get("data_url") or "").split(",", 1)[-1]
        if not data:
            return "Capture impossible."
        path = self.shots / f"shot_{len(list(self.shots.iterdir()))}.png"
        path.write_bytes(base64.b64decode(data))
        content, _ = self.llm.chat(self.cfg.models["vision"], [
            {"role": "user", "content": f"Capture d'écran de {shot.get('url', '')}.\n{question}", "images": [str(path)]}])
        return (f"(capture : {path})\n{content}\n\n[Rappel : estimation visuelle. Pour les chiffres, prix et notes, "
                "utilise browser_extract ou browser_snapshot.]")


__all__ = ["TabBrowser", "Cancelled"]

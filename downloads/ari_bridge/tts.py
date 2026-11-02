"""Voix d'Ari hors ligne avec Piper : une voix par langue, choisie selon la langue de la réponse.

- allemand : de_DE-thorsten-high · anglais : en_US-lessac-high · français : fr_FR-tom-medium
  (tom a été le plus intelligible des voix françaises testées, devant siwis et la voix Apple « Thomas »)
- la page découpe la réponse en phrases et demande chaque phrase l'une après l'autre : Ari commence à parler
  dès que la première est prête (environ 0,2 à 0,5 s), sans attendre la fin.
"""

import io
import re
import subprocess
import tempfile
import threading
import time
import wave

from agentic_ari.config import ROOT

DIR = ROOT / ".ari" / "models" / "piper"
VOICES = {"de": "de_DE-thorsten-high", "en": "en_US-lessac-high", "fr": "fr_FR-tom-medium"}


class PiperTTS:
    def __init__(self):
        self._voices = {}
        self.lock = threading.Lock()

    def available(self, lang=None):
        langs = [lang] if lang else list(VOICES)
        return all((DIR / f"{VOICES.get(l, '')}.onnx").exists() for l in langs if l in VOICES)

    def _voice(self, lang):
        from piper import PiperVoice

        name = VOICES.get(lang, VOICES["fr"])
        if name not in self._voices:
            self._voices[name] = PiperVoice.load(str(DIR / f"{name}.onnx"))
        return self._voices[name]

    def wav(self, text, lang="fr"):
        """Synthétise une phrase et renvoie un fichier WAV (octets)."""
        text = " ".join(str(text).split())[:1200]
        if not text:
            raise ValueError("texte vide")
        with self.lock:
            v = self._voice(lang if lang in VOICES else "fr")
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                v.synthesize_wav(text, wf)
        return buf.getvalue()

    def warm(self):
        """Charge les voix à l'avance (0,5 s chacune) pour que la première phrase parte vite."""
        for lang in VOICES:
            try:
                if self.available(lang):
                    self._voice(lang)
            except Exception:
                pass


# ---------- Voix du Mac (toutes les langues installées) et choix automatique par phrase ----------
# voix « gadgets » de macOS, jamais choisies automatiquement
NOVELTY = re.compile(
    r"^(Albert|Bad News|Bahh|Bells|Boing|Bubbles|Cellos|Wobble|Fred|Good News|Jester|Junior|Kathy|Organ|Ralph|"
    r"Superstar|Trinoids|Whisper|Zarvox|Eddy|Flo|Grandma|Grandpa|Reed|Rocko|Sandy|Shelley)\b"
)
QUALITY = re.compile(
    r"\((Premium|Enhanced|Erweitert|Améliorée|Amélioré|Mejorada|Migliorata|Melhorada|Verbeterd)\)", re.I
)
STOP = {  # mots très courants → langue de la phrase (simple, instantané, hors ligne)
    "fr": "le la les de des et est un une pour que qui dans vous je pas sur avec ce il elle au du en ne sont mais ou très bien",
    "en": "the and is of to in that it you for on with this are be not was your have what can will from they",
    "de": "der die das und ist ein eine nicht ich sie mit für auf den dem zu es von wir du auch sind aber oder sehr",
    "es": "el los las y es un una para que en no con por se del lo como pero más muy está mide velocidad precio",
    "it": "il lo gli e è un una per che non con del della dei delle di sono ma più molto come anche questo misura velocità",
    "pt": "o os as e é um uma para que não com do da em por mais muito como mas você",
    "nl": "de het een en is van dat niet ik je met voor op zijn maar ook wat",
    "mg": "ny sy dia amin'ny izy fa ho tsy aho ianao isika izahay ary koa no ao any eto mba azo tsara",
}
STOP = {k: set(v.split()) for k, v in STOP.items()}
LOCALE = {
    "fr": "fr_FR",
    "en": "en_US",
    "de": "de_DE",
    "es": "es_ES",
    "it": "it_IT",
    "pt": "pt_BR",
    "nl": "nl_NL",
    "ja": "ja_JP",
    "zh": "zh_CN",
    "ko": "ko_KR",
    "ru": "ru_RU",
    "pl": "pl_PL",
    "sv": "sv_SE",
    "tr": "tr_TR",
    "ar": "ar_001",
    "hi": "hi_IN",
}


def detect(text, hint="fr"):
    t = text.lower()
    if re.search(r"[぀-ヿ]", t):
        return "ja"
    if re.search(r"[一-鿿]", t):
        return "zh"
    if re.search(r"[가-힯]", t):
        return "ko"
    if re.search(r"[Ѐ-ӿ]", t):
        return "ru"
    if re.search(r"[؀-ۿ]", t):
        return "ar"
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœäößñáíóúãõ']+", t)
    scores = {k: sum(w in s for w in words) for k, s in STOP.items()}
    scores["de"] += len(re.findall(r"[äöüß]", t))
    scores["es"] += len(re.findall(r"[ñ¿¡]", t))
    scores["pt"] += len(re.findall(r"[ãõ]", t))
    top = max(scores.values())
    if top <= 0:
        return hint
    tied = [k for k, v in scores.items() if v == top]
    # mots partagés (« la », « de »…) : à égalité, la langue la plus spécifique l'emporte sur le français / l'anglais
    for k in tied:
        if k not in ("fr", "en"):
            return k
    return hint if hint in tied else tied[0]


class MacVoices:
    def __init__(self):
        self._list, self._at = [], 0

    def all(self):
        if time.time() - self._at > 300:
            out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
            items = []
            for line in out.splitlines():
                m = re.match(r"^(.+?)\s{2,}([a-z]{2,3}_[A-Z0-9]{2,3})\s+#", line)
                if m:
                    items.append(
                        {
                            "name": m.group(1).strip(),
                            "locale": m.group(2),
                            "lang": m.group(2)[:2],
                            "quality": bool(QUALITY.search(m.group(1))),
                            "novelty": bool(NOVELTY.match(m.group(1))),
                        }
                    )
            self._list, self._at = items, time.time()
        return self._list

    def best(self, lang):
        loc = LOCALE.get(lang, "")
        cands = [v for v in self.all() if v["lang"] == lang and not v["novelty"]]
        cands.sort(key=lambda v: (not v["quality"], v["locale"] != loc))
        return cands[0] if cands else None

    def wav(self, text, name):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
            subprocess.run(
                ["say", "-v", name, "--file-format=WAVE", "--data-format=LEI16@22050", "-o", f.name, "--", text],
                check=True,
                capture_output=True,
                timeout=30,
            )
            return open(f.name, "rb").read()


class Voices:
    """Choisit la voix de CHAQUE phrase : voix réglée par Andria, sinon voix Premium du Mac, sinon Piper (fr/de/en),
    sinon la meilleure voix du Mac pour la langue, sinon Piper français."""

    def __init__(self, piper, prefs=None):
        self.piper, self.mac = piper, MacVoices()
        self.prefs = prefs or (lambda: {})

    def choose(self, lang):
        pref = (self.prefs() or {}).get(lang)
        if pref == "piper" and lang in VOICES and self.piper.available(lang):
            return ("piper", VOICES[lang])
        if pref and pref.startswith("mac:") and any(v["name"] == pref[4:] for v in self.mac.all()):
            return ("mac", pref[4:])
        best = self.mac.best(lang)
        if best and best["quality"]:
            return ("mac", best["name"])
        if lang in VOICES and self.piper.available(lang):
            return ("piper", VOICES[lang])
        if best:
            return ("mac", best["name"])
        return ("piper", VOICES["fr"])

    def wav(self, text, lang="auto", hint="fr"):
        text = " ".join(str(text).split())[:1200]
        if not text:
            raise ValueError("texte vide")
        lang = detect(text, hint) if lang in ("", "auto", None) else lang[:2]
        engine, name = self.choose(lang)
        if engine == "mac":
            try:
                return self.mac.wav(text, name), lang, f"mac:{name}"
            except Exception:
                engine, name = "piper", VOICES.get(lang, VOICES["fr"])
        return self.piper.wav(text, lang if lang in VOICES else "fr"), lang, f"piper:{name}"

    def catalog(self):
        langs = sorted({v["lang"] for v in self.mac.all()} | set(VOICES) | {"mg"})
        out = []
        for l in langs:
            opts = (
                [{"id": "piper", "name": f"Piper · {VOICES[l]}", "quality": True}]
                if l in VOICES and self.piper.available(l)
                else []
            )
            opts += [
                {"id": f"mac:{v['name']}", "name": v["name"], "quality": v["quality"]}
                for v in self.mac.all()
                if v["lang"] == l and not v["novelty"]
            ]
            out.append(
                {
                    "lang": l,
                    "auto": "{}:{}".format(*self.choose(l)),
                    "options": opts,
                    "pref": (self.prefs() or {}).get(l),
                }
            )
        return out

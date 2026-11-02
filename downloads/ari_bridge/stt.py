"""Dictée de secours 100 % hors ligne : Whisper (via sherpa-onnx) + détection de parole Silero.

Utilisée quand la dictée d'Apple n'est pas installée sur le Mac pour la langue choisie (ex. français).
Le pont écoute le micro (autorisation de l'app, dont il est le processus enfant), s'arrête quand vous
vous taisez, puis transcrit sur le Mac. Rien ne part en ligne.
"""
import threading
import time

from agentic_ari.config import ROOT

MODELS = ROOT / ".ari" / "models"
WHISPER = MODELS / "sherpa-onnx-whisper-small"
VAD = MODELS / "silero_vad.onnx"
SR = 16000


class LocalSTT:
    def __init__(self, bus):
        self.bus = bus
        self.lock = threading.Lock()
        self._rec = {}
        self.cancel = False   # « stop » cliqué pendant l'écoute : on lâche le micro tout de suite

    @staticmethod
    def available():
        return (WHISPER / "small-encoder.int8.onnx").exists() and VAD.exists()

    def _recognizer(self, lang):
        import sherpa_onnx
        if lang not in self._rec:
            self._rec[lang] = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(WHISPER / "small-encoder.int8.onnx"), decoder=str(WHISPER / "small-decoder.int8.onnx"),
                tokens=str(WHISPER / "small-tokens.txt"), language=lang, task="transcribe", num_threads=4)
        return self._rec[lang]

    def _record(self, first_silence=7.0, end_silence=1.1, max_len=20.0):
        """Enregistre jusqu'à la fin de la phrase (silence), ou rien si personne ne parle."""
        import numpy as np
        import sherpa_onnx
        import sounddevice as sd
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(VAD)
        cfg.silero_vad.min_silence_duration = end_silence
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.threshold = 0.5
        cfg.sample_rate = SR
        vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=max_len + 5)
        speech, started, t0 = [], False, time.time()
        last_level = 0
        with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=512) as mic:
            self.bus.emit("stt", kind="listening")
            while time.time() - t0 < max_len and not self.cancel:
                chunk, _ = mic.read(512)
                a = np.ascontiguousarray(chunk[:, 0])
                vad.accept_waveform(a)
                if time.time() - last_level > 0.07:
                    self.bus.emit("stt", kind="level", level=min(1.0, float(abs(a).max()) * 3))
                    last_level = time.time()
                if vad.is_speech_detected():
                    started = True
                while not vad.empty():   # une phrase complète vient de se terminer
                    speech.append(np.array(vad.front.samples, dtype="float32"))
                    vad.pop()
                    return np.concatenate(speech)
                if not started and time.time() - t0 > first_silence:
                    return None
        if self.cancel:
            return None
        vad.flush()
        while not vad.empty():
            speech.append(np.array(vad.front.samples, dtype="float32"))
            vad.pop()
        return np.concatenate(speech) if speech else None

    def listen(self, lang="fr"):
        if not self.available():
            raise RuntimeError("le modèle Whisper local n'est pas encore installé")
        lang = lang if lang in ("fr", "en", "de") else ""   # "" : Whisper détecte la langue parlée
        self.cancel = False
        with self.lock:
            audio = self._record()
            self.bus.emit("stt", kind="done")
            if audio is None or len(audio) < SR * 0.3:
                return "", ""
            rec = self._recognizer(lang)
            s = rec.create_stream()
            s.accept_waveform(SR, audio)
            rec.decode_stream(s)
            text = s.result.text.strip()
            import re   # Whisper note parfois le bruit ambiant : « *Musique* », « [Applaudissements] »
            text = re.sub(r"[\*\[\(][^\*\]\)]{0,40}[\*\]\)]", "", text).strip()
            spoken = (getattr(s.result, "lang", "") or lang or "").strip("<|>")[:2]
            return text, spoken

"""Mot d'éveil « Hey Ari » avec sherpa-onnx (repérage de mots-clés, 100 % hors ligne).

Un petit modèle (3,3 M paramètres) écoute le micro en continu dans le pont et prévient l'app quand il
entend « Hey Ari » (événement « wake »). La transcription de la demande reste faite par Apple sur le Mac.
Le pont est lancé par l'app : macOS lui applique l'autorisation micro de l'app.
"""
import threading
import time

from agentic_ari.config import ROOT

MODEL_DIR = ROOT / ".ari" / "models" / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
KEYWORDS = ROOT / ".ari" / "models" / "ari_keywords.txt"
# variantes de « Hey Ari » (accents, prononciations) ; la tokenisation BPE est faite au démarrage
PHRASES = ["HEY ARI", "HI ARI", "HE ARI", "HEY ARIE", "HEY ARRY", "HEY HARRY", "OK ARI", "HALLO ARI", "AY ARI",
           "EH ARI", "HEY AREE", "HAY ARI", "HEY ARRIE", "HEY AH RI", "EH HARRY", "HAY HARRY", "HEY ERRY",
           "HEY AIRY", "HELLO ARI", "ALLO ARI"]
SAMPLE_RATE = 16000


class SherpaWake:
    def __init__(self, bus):
        self.bus = bus
        self.thread = None
        self.running = False
        self.paused = False
        self.last = 0.0
        self.error = None
        self.level = 0.0
        self.ring = []

    @staticmethod
    def available():
        try:
            import sherpa_onnx  # noqa: F401
            import sounddevice  # noqa: F401
        except ImportError:
            return False
        return MODEL_DIR.is_dir()

    def _keywords(self):
        import sentencepiece as spm
        sp = spm.SentencePieceProcessor(model_file=str(MODEL_DIR / "bpe.model"))
        KEYWORDS.write_text("\n".join(" ".join(sp.encode(p, out_type=str)) + " @HEY_ARI" for p in PHRASES) + "\n")
        return KEYWORDS

    def _spotter(self):
        import sherpa_onnx
        m = MODEL_DIR
        return sherpa_onnx.KeywordSpotter(
            tokens=str(m / "tokens.txt"),
            encoder=str(m / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(m / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=str(m / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=str(self._keywords()), num_threads=1, provider="cpu",
            keywords_score=3.0, keywords_threshold=0.05)   # réglés sur des essais « Hey Ari » / phrases ordinaires

    def start(self):
        if not self.available():
            raise RuntimeError("sherpa-onnx ou son modèle n'est pas installé")
        self.paused = False
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name="wake-sherpa")
        self.thread.start()

    def stop(self):
        self.running = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def dump(self, path):
        """Enregistre les 6 dernières secondes entendues (WAV 16 kHz) pour régler la détection."""
        import wave
        import numpy as np
        a = np.concatenate(self.ring) if self.ring else np.zeros(1, dtype="float32")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
        return str(path)

    def status(self):
        return {"engine": "sherpa-onnx", "running": self.running, "paused": self.paused, "error": self.error,
                "level": round(self.level, 3)}

    def _loop(self):
        import numpy as np
        import sounddevice as sd
        try:
            kws = self._spotter()
            stream = kws.create_stream()
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=1600) as mic:
                self.error = None
                while self.running:
                    chunk, _ = mic.read(1600)   # 0,1 s
                    self.level = max(self.level * 0.9, float(abs(chunk).max()))
                    self.ring.append(chunk[:, 0].copy())
                    del self.ring[:-60]   # 6 dernières secondes (diagnostic)
                    if self.paused:
                        continue
                    stream.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(chunk[:, 0]))
                    while kws.is_ready(stream):
                        kws.decode_stream(stream)
                        if kws.get_result(stream):
                            kws.reset_stream(stream)
                            if time.time() - self.last > 2.5:   # anti-rebond
                                self.last = time.time()
                                self.paused = True              # l'app reprend la main (dictée)
                                self.bus.emit("wake", source="sherpa")
        except Exception as e:   # micro refusé, périphérique absent…
            self.error = f"{type(e).__name__} : {e}"
            self.bus.emit("wake_error", text=self.error)
        finally:
            self.running = False

"""Réponses en direct : même LLM que le cœur, mais Ollama en mode flux, et chaque morceau de texte part vers l'app.

- on ne touche pas au cœur : `StreamLLM` remplace `LLM` pour les tâches lancées depuis l'app
- un appel = un brouillon : l'app l'efface au début de chaque nouvel appel, puis la réponse finale le remplace
- jamais de fouillis : un texte qui commence comme un appel d'outil ({, [, ```, <) n'est pas montré,
  et le brouillon est effacé si l'appel se termine par des appels d'outils
- Apple Intelligence, serveurs locaux et sorties JSON (fmt) passent par le chemin normal, sans flux
"""
import httpx
import ollama

from agentic_ari.llm import LLM, _fix_args, clean_spaces, fix_byte_tokens, parse_text_tool_calls

HOLD = ("{", "[", "```", "<", "tool_call", "call:")
MIN_CHARS = 12   # assez pour savoir si c'est de la prose


class StreamLLM(LLM):
    def __init__(self, cfg, on_delta=None, on_reset=None, cancelled=lambda: False):
        super().__init__(cfg)
        self.on_delta = on_delta or (lambda text: None)
        self.on_reset = on_reset or (lambda: None)
        self.cancelled = cancelled
        self.first_delta = None   # horodatage du premier mot montré (mesure)

    def chat(self, model, messages, tools=None, fmt=None):
        from agentic_ari import local_servers
        if fmt or model == "apple" or local_servers.is_local_model(model):
            return super().chat(model, messages, tools, fmt)
        messages = [dict(m, content=clean_spaces(m["content"])) if isinstance(m.get("content"), str) else m
                    for m in messages]
        kwargs = dict(model=model, messages=messages, stream=True,
                      options={"num_ctx": self.cfg.num_ctx, "temperature": self.cfg.temperature},
                      keep_alive=self.cfg.keep_alive)
        if tools:
            kwargs["tools"] = tools
        try:
            try:
                chunks = self.client.chat(think=self.cfg.think, **kwargs)
            except TypeError:
                chunks = self.client.chat(**kwargs)
            return self._consume(chunks, tools)
        except ollama.ResponseError:
            return super().chat(model, messages, tools, fmt)   # modèle sans « thinking » ni flux : chemin normal
        except httpx.TimeoutException:
            return f"(le modèle {model} n'a pas répondu en {self.cfg.llm_timeout}s)", []

    def _consume(self, chunks, tools):
        import time
        text, calls = "", []
        shown, held, started = 0, False, False
        for ch in chunks:
            if self.cancelled():
                break
            msg = ch.message
            for c in (msg.tool_calls or []):
                calls.append({"name": c.function.name, "arguments": _fix_args(dict(c.function.arguments or {}))})
            piece = msg.content or ""
            if not piece:
                continue
            text += piece
            if held or calls:
                continue
            head = text.lstrip()
            if not started:
                if len(head) < MIN_CHARS:
                    continue
                if any(h in head[:MIN_CHARS + 8].lower() for h in HOLD):
                    held = True
                    continue
                started = True
                self.on_reset()
            out = fix_byte_tokens(text[shown:])
            if out:
                if self.first_delta is None:
                    self.first_delta = time.time()
                self.on_delta(out)
                shown = len(text)
        content = fix_byte_tokens(text)
        if not calls and tools:
            calls = parse_text_tool_calls(content, {t["function"]["name"] for t in tools})
            if calls:
                content = ""
        if calls and started:
            self.on_reset()   # c'était le début d'un appel d'outil : on retire le brouillon
        return content, calls

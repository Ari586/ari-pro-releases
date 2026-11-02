"""AriRelay : relais entre ARI Pro et MetaTrader 5 sur ce VPS Windows.

Laisse cette fenêtre ouverte, avec MetaTrader 5 et l'EA AriBridge sur un graphique.
AriRelay reçoit les ordres d'ARI Pro, chiffrés et signés avec le code affiché au démarrage,
les dépose pour l'EA dans le dossier commun de MetaTrader 5 et renvoie sa réponse.
Toute demande sans le bon code est refusée sans rien toucher.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RELAY_VERSION = "2"
PORTS = (8787, 443)     # 443 en plus : certains hébergeurs bloquent 8787
TASK_NAME = "AriRelay"  # tâche planifiée Windows (--demarrage-auto)
MAX_BODY = 65536
WINDOW_S = 120          # écart d'horloge toléré entre ARI Pro et le VPS
SEND_MAX_S = 25
KEY = re.compile(r"[a-z_]{1,20}")


def b64(x):
    return base64.b64encode(x).decode()


def keys(secret):
    return (hmac.new(secret, b"ari-enc", hashlib.sha256).digest(),
            hmac.new(secret, b"ari-mac", hashlib.sha256).digest())


def stream(k, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(k, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def seal(secret, kind, obj):
    ke, km = keys(secret)
    pt = json.dumps(obj).encode()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(pt, stream(ke, nonce, len(pt))))
    tag = hmac.new(km, kind + nonce + ct, hashlib.sha256).digest()
    return {"v": 1, "n": b64(nonce), "c": b64(ct), "t": b64(tag)}, nonce


def unseal(secret, kind, env):
    ke, km = keys(secret)
    nonce, ct, tag = (base64.b64decode(env[k], validate=True) for k in "nct")
    good = hmac.new(km, kind + nonce + ct, hashlib.sha256).digest()
    if len(nonce) != 16 or not hmac.compare_digest(tag, good):
        raise ValueError("signature")
    return json.loads(bytes(a ^ b for a, b in zip(ct, stream(ke, nonce, len(ct))))), nonce


def code_of(secret):
    s = base64.b32encode(secret).decode().rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def parse(text):
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Relay:
    def __init__(self, secret, common):
        self.secret, self.common = secret, Path(common)
        self.seen, self.lock = {}, threading.Lock()
        self.ports = []

    def fresh(self, nonce, ts):
        now = time.time()
        try:
            if abs(now - float(ts)) > WINDOW_S:
                return False
        except (TypeError, ValueError):
            return False
        with self.lock:
            for k, t in list(self.seen.items()):
                if t < now:
                    del self.seen[k]
            if nonce in self.seen:
                return False
            self.seen[nonce] = now + 2 * WINDOW_S
        return True

    def handle(self, req):
        op = req.get("op")
        if op == "account":
            return self.account()
        if op == "status":
            return {**self.account(), "relay_version": RELAY_VERSION, "ports": list(self.ports)}
        if op == "send":
            return self.send(req.get("cmd") or {}, req.get("timeout") or 20)
        return {"ok": False, "error": "demande inconnue"}

    def account(self):
        f = self.common / "ari_account.txt"
        try:
            age = max(time.time() - f.stat().st_mtime, 0.0)
            text = f.read_text(encoding="latin-1")
        except OSError:
            return {"ok": True, "found": False}
        return {"ok": True, "found": True, "age": age, "text": text}

    def send(self, cmd, timeout):
        lines = []
        for k, v in dict(cmd).items():
            k, v = str(k), str(v)
            if not KEY.fullmatch(k) or k == "id" or len(v) > 80 or any(c in v for c in "\r\n="):
                return {"ok": False, "error": "commande invalide"}
            lines.append(k + "=" + v + "\n")
        cid = secrets.token_hex(6)
        cmd_f = self.common / ("ari_cmd_" + cid + ".txt")
        res_f = self.common / ("ari_res_" + cid + ".txt")
        tmp = self.common / ("ari_cmd_" + cid + ".tmp")
        tmp.write_bytes(("id=" + cid + "\n" + "".join(lines)).encode("ascii", "replace"))
        os.replace(tmp, cmd_f)
        what = " ".join(parse("".join(lines)).get(k, "") for k in ("action", "side", "symbol", "position"))
        say("Transmis à MetaTrader 5 : " + " ".join(what.split()))
        deadline = time.monotonic() + min(max(float(timeout), 1.0), SEND_MAX_S)
        while time.monotonic() < deadline:
            if res_f.exists():
                text = res_f.read_text(encoding="latin-1")
                if parse(text).get("id") == cid:
                    try:
                        res_f.unlink()
                    except OSError:
                        pass
                    return {"ok": True, "status": "done", "text": text}
            time.sleep(0.2)
        try:
            cmd_f.unlink()
            say("MetaTrader 5 n'a pas pris la commande : annulée.")
            return {"ok": True, "status": "not_taken"}
        except FileNotFoundError:
            return {"ok": True, "status": "unconfirmed"}


class Handler(BaseHTTPRequestHandler):
    relay = None
    timeout = 15
    server_version = "AriRelay"
    sys_version = ""

    def do_POST(self):
        if self.path != "/ari":
            return self.reply(404, b"")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self.reply(413, b"")
        try:
            req, nonce = unseal(self.relay.secret, b"Q", json.loads(self.rfile.read(n)))
        except Exception:
            say("Demande refusée (code faux) depuis " + self.client_address[0])
            return self.reply(403, b"")
        if not self.relay.fresh(nonce, req.get("ts")):
            say("Demande refusée (rejouée ou horloge décalée) depuis " + self.client_address[0])
            return self.reply(403, b"")
        try:
            out = self.relay.handle(req)
        except Exception as e:
            out = {"ok": False, "error": str(e)[:200]}
        out["re"] = b64(nonce)
        env, _ = seal(self.relay.secret, b"R", out)
        self.reply(200, json.dumps(env).encode(), "application/json")

    def do_GET(self):
        # « ping » sans code : dit seulement que c'est bien AriRelay (aucune info du compte).
        if self.path == "/ping":
            body = json.dumps({"ari_relay": True, "version": RELAY_VERSION}).encode()
            return self.reply(200, body, "application/json")
        self.reply(404, b"")

    def reply(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def load_secret(path, renew):
    try:
        if not renew:
            s = bytes.fromhex(path.read_text().strip())
            if len(s) == 16:
                return s
    except (OSError, ValueError):
        pass
    s = secrets.token_bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(s.hex())
    return s


def firewall_commands(port):
    """Commandes netsh : (voir si la règle existe, l'ajouter) pour ouvrir le port en entrée."""
    name = "AriRelay " + str(port)
    show = ["netsh", "advfirewall", "firewall", "show", "rule", "name=" + name]
    add = ["netsh", "advfirewall", "firewall", "add", "rule", "name=" + name, "dir=in",
           "action=allow", "protocol=TCP", "localport=" + str(port)]
    return show, add


def open_firewall(port):
    """Règle entrante du pare-feu Windows pour le port (administrateur seulement)."""
    if os.name != "nt":
        return
    show, add = firewall_commands(port)
    if subprocess.run(show, capture_output=True).returncode == 0:
        return
    if subprocess.run(add, capture_output=True).returncode != 0:
        say("Pare-feu Windows : port " + str(port) + " non ouvert. Relance « Lancer AriRelay.bat » "
            "avec un clic droit › Exécuter en tant qu'administrateur.")
    else:
        say("Pare-feu Windows : port " + str(port) + " ouvert.")


def startup_commands(script, python=None, port=None):
    """Commandes schtasks : (créer la tâche qui relance AriRelay à l'ouverture de session, la retirer)."""
    run = '"' + str(python or sys.executable) + '" "' + str(script) + '"'
    if port:
        run += " --port " + str(int(port))
    create = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", run, "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"]
    delete = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    return create, delete


def set_startup(on, port=None):
    """Ajoute ou retire le démarrage automatique (tâche planifiée Windows) ; rend True si c'est fait."""
    if os.name != "nt":
        say("Démarrage automatique : seulement sous Windows.")
        return False
    create, delete = startup_commands(Path(__file__).resolve(), port=port)
    done = subprocess.run(create if on else delete, capture_output=True).returncode == 0
    if on:
        say("Démarrage automatique activé : AriRelay se relancera à chaque ouverture de session."
            if done else "Démarrage automatique non activé. Relance avec un clic droit › Exécuter "
            "en tant qu'administrateur.")
    else:
        say("Démarrage automatique retiré." if done else "Pas de démarrage automatique à retirer.")
    return done


def why_closed(err):
    """Raison en mots simples pour un port qui n'a pas pu s'ouvrir."""
    n = getattr(err, "winerror", None) or getattr(err, "errno", None)
    if n in (98, 48, 10048):
        return "déjà utilisé par un autre programme (serveur web, IIS…)"
    if n in (13, 10013):
        return "accès refusé (port réservé, ou lance en administrateur)"
    return str(err) or err.__class__.__name__


def open_servers(ports, handler, host="0.0.0.0"):
    """Un serveur par port ; rend ([(port, serveur)], [(port, raison)])."""
    opened, failed = [], []
    for port in ports:
        try:
            server = ThreadingHTTPServer((host, port), handler)
        except OSError as e:
            failed.append((port, why_closed(e)))
            continue
        server.daemon_threads = True
        opened.append((port, server))
    return opened, failed


def parse_args(argv=None):
    appdata = Path(os.environ.get("APPDATA") or Path.home())
    ap = argparse.ArgumentParser(description="Relais ARI Pro ↔ MetaTrader 5")
    ap.add_argument("--port", type=int, default=None, help="un seul port (sinon 8787 et 443)")
    ap.add_argument("--common", default=str(appdata / "MetaQuotes" / "Terminal" / "Common" / "Files"))
    ap.add_argument("--code-file", default=str(appdata / "AriRelay" / "code.txt"))
    ap.add_argument("--nouveau-code", action="store_true", help="remplace le code (l'ancien ne marche plus)")
    ap.add_argument("--sans-pare-feu", action="store_true")
    ap.add_argument("--demarrage-auto", action="store_true",
                    help="relance AriRelay à chaque ouverture de session Windows (tâche planifiée)")
    ap.add_argument("--sans-demarrage-auto", action="store_true", help="retire le démarrage automatique")
    a = ap.parse_args(argv)
    a.ports = [a.port] if a.port else list(PORTS)
    return a


def main(argv=None):
    a = parse_args(argv)
    if a.sans_demarrage_auto:
        set_startup(False)
        return 0
    if a.demarrage_auto:
        set_startup(True, a.port)
    secret = load_secret(Path(a.code_file), a.nouveau_code)
    common = Path(a.common)
    if not common.is_dir():
        say("Dossier de MetaTrader 5 introuvable (" + str(common) + ") : ouvre MetaTrader 5 au moins une fois.")
        common.mkdir(parents=True, exist_ok=True)
    Handler.relay = Relay(secret, common)
    opened, failed = open_servers(a.ports, Handler)
    for port, why in failed:
        say("Port " + str(port) + " non ouvert : " + why + ".")
    if not opened:
        say("Aucun port n'a pu s'ouvrir : AriRelay s'arrête. Essaie un autre port avec --port 8788.")
        return 1
    Handler.relay.ports = [p for p, _ in opened]
    for port, server in opened:
        if not a.sans_pare_feu:
            open_firewall(port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    ports = ", ".join(str(p) for p, _ in opened)
    print("", flush=True)
    print("  AriRelay est prêt (port " + ports + ").", flush=True)
    print("  Code à saisir dans ARI Pro : " + code_of(secret), flush=True)
    print("  Adresse : l'adresse IP de ce VPS (celle du Bureau à distance).", flush=True)
    print("  Laisse cette fenêtre ouverte, avec MetaTrader 5 et l'EA AriBridge.", flush=True)
    print("", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    for _, server in opened:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

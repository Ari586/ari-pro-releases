"""python -m ari_bridge          lance le pont local
python -m ari_bridge token    affiche la clé (et la copie dans le presse-papiers) pour l'extension
"""
import subprocess
import sys

from .server import load_token, serve


def main():
    if sys.argv[1:] == ["token"]:
        tok = load_token()
        try:
            subprocess.run(["pbcopy"], input=tok.encode(), check=True)
            print(tok + "\n(copiée dans le presse-papiers : collez-la dans les réglages de l'extension)")
        except Exception:
            print(tok)
        return 0
    if sys.argv[1:2] == ["shortcut"]:   # raccourci Siri « Demander à Ari »
        from .siri import make
        path = make(open_after="--open" in sys.argv)
        print(f"Raccourci prêt : {path}")
        return 0
    if sys.argv[1:2] == ["mobile"]:   # accès depuis l'app Android (désactivé par défaut)
        return mobile(sys.argv[2] if len(sys.argv) > 2 else "état")
    return serve()


def mobile(action):
    """python -m ari_bridge mobile on|off|état : ouvre / ferme le pont à l'app Android du réseau local."""
    import socket
    from .server import LAN_FLAG, PORT
    if action in ("on", "activer"):
        LAN_FLAG.parent.mkdir(parents=True, exist_ok=True)
        LAN_FLAG.touch()
    elif action in ("off", "désactiver"):
        LAN_FLAG.unlink(missing_ok=True)
    elif action not in ("état", "etat", "status"):
        print("Usage : python -m ari_bridge mobile on | off | état")
        return 2
    on = LAN_FLAG.exists()
    print(f"Accès mobile : {'ACTIVÉ' if on else 'désactivé'}")
    if action in ("on", "activer", "off", "désactiver"):
        print("Relancez l'app Agentic Ari (⌘Q puis rouvrir) pour l'appliquer.")
    if on:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            ip = "<adresse-du-mac>"
        print(f"Dans l'app Android → Réglages :\n  Adresse : http://{ip}:{PORT}\n  Clé     : {load_token()}")
    return 0



if __name__ == "__main__":
    sys.exit(main())

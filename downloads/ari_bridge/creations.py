"""Créations d'Ari (documents, photos retouchées, vidéos, images) : tout est fait par le cœur (agentic_ari.creator),
rangé dans ~/Documents/Ari ; ici on liste, on sert les aperçus et on reçoit les fichiers choisis dans l'app.

- un nom de fichier venant de l'app est toujours ramené à un nom simple DANS le dossier de sortie (jamais de chemin)
- les images envoyées par l'app (retouche, diaporama) sont décodées dans .ari/attachments, jamais exécutées
"""
import base64
import re
import subprocess
import time
import uuid

from agentic_ari.config import ROOT

KINDS = {".docx": "word", ".xlsx": "excel", ".pptx": "powerpoint", ".pdf": "pdf", ".jpg": "image", ".jpeg": "image",
         ".png": "image", ".webp": "image", ".mp4": "video", ".mov": "video"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".mp4": "video/mp4",
        ".mov": "video/quicktime", ".pdf": "application/pdf"}
UPLOADS = ROOT / ".ari" / "attachments" / "creations"
MAX_UPLOAD = 25 * 1024 * 1024


def out_dir():
    from agentic_ari import creator
    d = creator.OUT
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_path(name):
    """Nom envoyé par l'app → chemin dans le dossier de sortie, ou ValueError."""
    name = str(name or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("nom de fichier invalide")
    p = (out_dir() / name).resolve()
    if p.parent != out_dir().resolve() or not p.is_file():
        raise ValueError("fichier introuvable")
    return p


def listing(limit=60):
    items = []
    for p in out_dir().iterdir():
        kind = KINDS.get(p.suffix.lower())
        if not kind or not p.is_file():
            continue
        st = p.stat()
        items.append({"name": p.name, "kind": kind, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: -x["mtime"])
    return {"dir": str(out_dir()), "items": items[:limit]}


def file_bytes(name):
    p = safe_path(name)
    mime = MIME.get(p.suffix.lower())
    if not mime:
        raise ValueError("aperçu indisponible pour ce type de fichier")
    return p.read_bytes(), mime


def reveal(name, open_it=False):
    p = safe_path(name)
    subprocess.run(["open", str(p)] if open_it else ["open", "-R", str(p)], check=False)
    return True


def _save_upload(data_url):
    m = re.match(r"^data:image/(png|jpe?g|webp|heic);base64,(.+)$", str(data_url or ""), re.S)
    if not m:
        raise ValueError("image invalide (PNG, JPEG, WebP ou HEIC)")
    raw = base64.b64decode(m.group(2), validate=False)
    if len(raw) > MAX_UPLOAD:
        raise ValueError("image trop lourde (25 Mo au plus)")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    ext = {"jpeg": "jpg"}.get(m.group(1), m.group(1))
    p = UPLOADS / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.{ext}"
    p.write_bytes(raw)
    return p


def _name(path):
    return None if not path else str(path).rsplit("/", 1)[-1]


def create_document(kind, title, content):
    from agentic_ari import creator
    if kind not in ("pdf", "docx", "xlsx", "pptx"):
        raise ValueError("type de document inconnu")
    title, content = str(title or "").strip()[:200], str(content or "").strip()[:200000]
    if not title and not content:
        raise ValueError("donnez au moins un titre ou un contenu")
    path = creator.create_document(kind, title or "Document", content)
    return {"name": _name(path), "message": "Document créé."}


def edit_photo(image, operations):
    from agentic_ari import creator
    ops = str(operations or "").strip()[:500]
    if not ops:
        raise ValueError("dites quelles retouches faire (ex. plus lumineux, contraste, carré)")
    res = creator.edit_photo(str(_save_upload(image)), ops)
    path, done = (res if isinstance(res, tuple) else (res, []))
    return {"name": _name(path), "message": "Retouches : " + (", ".join(done) if done else ops)}


def slideshow(images, title, seconds):
    from agentic_ari import creator
    if not isinstance(images, list) or not 1 <= len(images) <= 60:
        raise ValueError("choisissez entre 1 et 60 images")
    paths = [str(_save_upload(i)) for i in images]
    try:
        sec = max(1.0, min(10.0, float(seconds or 3)))
    except (TypeError, ValueError):
        sec = 3.0
    path = creator.slideshow(paths, str(title or "video")[:80], sec)
    return {"name": _name(path), "message": f"Vidéo créée à partir de {len(paths)} image(s)."}


def generate_image(prompt):
    from agentic_ari import creator
    prompt = str(prompt or "").strip()[:1000]
    if not prompt:
        raise ValueError("décrivez l'image à créer")
    path, msg = creator.generate_image(prompt)
    return {"name": _name(path), "message": msg or ("Image créée." if path else "Image non créée.")}


def ai_video(prompt):
    from agentic_ari import creator
    prompt = str(prompt or "").strip()[:1000]
    if not prompt:
        raise ValueError("décrivez la vidéo à inventer")
    path, msg = creator.ai_video(prompt)
    return {"name": _name(path), "message": msg or ("Vidéo créée." if path else "Vidéo non créée.")}


def remove_background(image):
    from agentic_ari import creator
    path, msg = creator.remove_background(str(_save_upload(image)))
    return {"name": _name(path), "message": msg or ("Fond enlevé." if path else "Fond non enlevé.")}

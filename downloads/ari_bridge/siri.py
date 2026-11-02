"""Raccourci Siri « Demander à Ari » : python -m ari_bridge shortcut

Le raccourci demande (à la voix avec « Dis Siri, Demander à Ari », ou au clavier dans Raccourcis),
envoie la demande au pont local, attend la réponse et la lit à voix haute. Les demandes
d'autorisation s'affichent dans l'app ou l'extension, exactement comme d'habitude.

Le fichier contient la clé du pont : il est signé en mode « personnes qui me connaissent »
(signature faite sur ce Mac) et reste dans .ari/, jamais partagé.
"""
import plistlib
import subprocess
import uuid

from agentic_ari.config import ROOT

from .server import PORT, load_token

NAME = "Demander à Ari"
OUT_DIR = ROOT / ".ari"


def _var(uid, name):
    return {"Value": {"string": "￼", "attachmentsByRange": {"{0, 1}": {
        "Type": "ActionOutput", "OutputUUID": uid, "OutputName": name}}},
            "WFSerializationType": "WFTextTokenString"}


def _text(s):
    return {"Value": {"string": s}, "WFSerializationType": "WFTextTokenString"}


def _dict(items):
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0, "WFKey": _text(k), "WFValue": v} for k, v in items]},
            "WFSerializationType": "WFDictionaryFieldValue"}


def build():
    ask, http, val = (str(uuid.uuid4()).upper() for _ in range(3))
    actions = [
        {"WFWorkflowActionIdentifier": "is.workflow.actions.ask", "WFWorkflowActionParameters": {
            "UUID": ask, "WFAskActionPrompt": "Que veux-tu demander à Ari ?", "WFInputType": "Text"}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl", "WFWorkflowActionParameters": {
            "UUID": http, "WFURL": f"http://127.0.0.1:{PORT}/api/ask_sync", "WFHTTPMethod": "POST",
            "ShowHeaders": True, "WFHTTPBodyType": "JSON",
            "WFHTTPHeaders": _dict([("Authorization", _text(f"Bearer {load_token()}"))]),
            "WFJSONValues": _dict([("text", _var(ask, "Entrée fournie")), ("mode", _text("team")),
                                   ("client", _text("siri"))])}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.getvalueforkey", "WFWorkflowActionParameters": {
            "UUID": val, "WFDictionaryKey": "speech", "WFInput": {
                "Value": {"Type": "ActionOutput", "OutputUUID": http, "OutputName": "Contenu de l'URL"},
                "WFSerializationType": "WFTextTokenAttachment"}}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.speaktext", "WFWorkflowActionParameters": {
            "WFText": _var(val, "Valeur du dictionnaire"), "WFSpeakTextLanguage": "fr-FR"}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.showresult", "WFWorkflowActionParameters": {
            "Text": _var(val, "Valeur du dictionnaire")}},
    ]
    return {"WFWorkflowActions": actions, "WFWorkflowClientVersion": "2605.0.5",
            "WFWorkflowMinimumClientVersion": 900, "WFWorkflowHasShortcutInputVariables": False,
            "WFWorkflowIcon": {"WFWorkflowIconStartColor": 1440408063, "WFWorkflowIconGlyphNumber": 59511},
            "WFWorkflowImportQuestions": [], "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
            "WFWorkflowTypes": ["Watch", "MenuBar"], "WFQuickActionSurfaces": [],
            "WFWorkflowOutputContentItemClasses": []}


def make(open_after=False):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw, signed = OUT_DIR / "siri_unsigned.shortcut", OUT_DIR / f"{NAME}.shortcut"
    with open(raw, "wb") as f:
        plistlib.dump(build(), f, fmt=plistlib.FMT_BINARY)
    raw.chmod(0o600)
    r = subprocess.run(["shortcuts", "sign", "--mode", "people-who-know-me", "--input", str(raw),
                        "--output", str(signed)], capture_output=True, text=True)
    raw.unlink(missing_ok=True)
    if r.returncode != 0 or not signed.exists():
        raise RuntimeError(f"signature du raccourci impossible : {(r.stderr or r.stdout).strip()[:300]}")
    signed.chmod(0o600)
    if open_after:
        subprocess.run(["open", str(signed)])   # Raccourcis propose « Ajouter le raccourci »
    return signed

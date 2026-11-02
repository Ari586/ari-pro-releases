# Courtiers MetaTrader sans MetaAPI, et mise à jour seule sur Mac (2026-10-27, Claude)

Base : `d7759db` (1.0.44+81, branche `metatrader-login`). Rien n'est committé ni publié. Contient aussi la correction de [maj-mac](../maj-mac/LISEZMOI.md).

## Pourquoi

Andria (JustMarkets, MetaTrader) ne pouvait pas se connecter : le jeton MetaAPI est refusé, MetaAPI est devenu payant (30 $ par mois après un essai de 7 jours). Elle a un compte WebhookTrade et veut qu'ARI Pro y envoie ses propres signaux, chaque utilisateur avec son propre compte. Un pont par EA sert de deuxième option.

## Ce qui change

**WebhookTrade** (`bridge_type = "webhooktrade"`) : MetaTrader dans le cloud, Mac éteint possible.
- Réglages : adresse Webhook (`webhook_url`), nom d'utilisateur (`login`), clé API (`api_key`), capital du compte (`webhook_capital`), suffixe des symboles (`symbol_suffix`, nouvelle colonne).
- La clé ne part que vers `https://webhooktrade.com` ; « Connecter » vérifie les champs sans envoyer d'ordre.
- Ordre : JSON des alertes TradingView de WebhookTrade (`broker: metatrader`, `action`, `sl_distance_price`, `tp_distance_price`, `risk_percentage` ou `size`, `strategy: "ARI"`).
- Clôture : `close_buy` / `close_sell` sur le symbole (toutes les positions d'ARI dans ce sens). Pas de liste de positions chez WebhookTrade : fermées dans l'app quand le prix touche le stop ou l'objectif.

**EA AriBridge** (`bridge_type = "ea"`) : gratuit, MetaTrader 5 ouvert sur le même ordinateur qu'ARI Pro.
- `core/tontolo/ari/mt_files.py` : échange par le dossier commun de MetaTrader (`ari_account.txt`, `ari_cmd_<id>.txt`, `ari_res_<id>.txt`), trouvé aussi dans Wine sur Mac.
- `core/tontolo/ari/ea_source.py` : source MQL5 de l'EA ; route `POST /ari/trading/bridge/ea/install` (copie dans `MQL5/Experts`, sinon dans Documents).
- L'EA calcule le lot avec le contrat du courtier (`OrderCalcProfit`), comptes en cents compris.
- L'EA n'a pas encore été compilé dans MetaTrader : à vérifier sur un compte démo.

**App** : deux choix de plus dans Trading › Robot et courtier ; l'aide MetaAPI dit qu'il est payant (`strings_ea.dart`, fr, en, de, es).

## Tests (conteneur)

- pytest : 993 réussis ; 1 échec déjà présent (`test_versioning`, version 1.0.43 du conteneur, corrigé sur le Mac par `c8edac5`).
- flutter analyze : 0 problème. flutter test : tout passe sauf les 2 contrôles CHANGELOG 1.0.44 déjà corrigés sur le Mac.

## Appliquer

```sh
cd ~/Documents/agentic-ari/externes/EL
git apply --3way /chemin/vers/code.patch
python3 scripts/api_docs.py   # docs/API.md complet (route ea/install)
```

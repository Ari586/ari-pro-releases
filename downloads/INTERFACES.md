# Agentic Ari : app Mac, extension Chrome / Comet et terminal

L'app a trois colonnes : l'agent (fil + saisie), le projet (fichiers, aperçu, diffs) et un petit panneau
« Détails » (ce qu'Ari fait en ce moment), masquable. Les choix se font dans la zone de saisie, comme
dans Gemini : **+** (projet, page, nouvelle conversation), **mode**, **modèle**, **bouclier** (confirmations).
L'extension reprend la colonne de l'agent avec la même zone de saisie.

Les trois interfaces parlent au même **pont local** (`ari_bridge/`) : même conversation, mêmes tâches,
mêmes demandes d'autorisation. Le cœur (`agentic_ari/`) n'est pas modifié.

## 1. App Mac

1. Ouvrez `dist/AgenticAri.dmg`, glissez **Agentic Ari** dans **Applications**.
2. Lancez-la. Elle réutilise le pont s'il tourne déjà (service `./ari service run`), sinon elle le démarre
   et l'arrête en quittant.

Menu **Agentic Ari** : copier la clé pour l'extension (⇧⌘K), afficher le dossier de l'extension,
ouvrir le workspace, journal du pont. Reconstruire après une modification : `./macapp/build.sh`.

## 2. Extension Chrome et Comet

1. Ouvrez `chrome://extensions` (dans Comet : `comet://extensions`), activez **Mode développeur**.
2. **Charger l'extension non empaquetée** → choisissez le dossier `~/Documents/agentic-ari/extension`.
3. Cliquez sur l'icône Agentic Ari (ou ⌘⇧Y) : le panneau latéral s'ouvre.
4. Collez la clé dans **/réglages** (copiée depuis l'app, ou `./ari-bridge token`).

Ce qu'elle fait :
- **Joindre la page** (`/page`) : la page ouverte est envoyée à l'agent comme donnée à analyser.
- **Mode « mon onglet »** : le sous-agent navigateur agit dans votre onglet réel (lire, cliquer, remplir,
  défiler, capture d'écran analysée par Gemma 4) puis rend `CONSTATS / DÉCISION / RAISONS / RISQUES`.
- Clic droit sur une page ou une sélection → **Analyser avec Agentic Ari**.

Les pages internes du navigateur (`chrome://`, Web Store) sont protégées : l'agent ne peut pas y agir.

## 3. Terminal façon Claude Code

```bash
./ari-code                 # interactif
./ari-code "ta demande"    # une demande puis la suite en interactif
```

`/aide`, `/mode [team|research|browser|build|review|system|tab]`, `/auto`, `/clear`, `/statut`,
`ctrl+c` arrête la tâche en cours.

## Projet et modèle

- **Ouvrir un projet** : bouton **+** de la zone de saisie, ou « Projet / … » en haut ; choisissez un dossier
  récent, parcourez vos dossiers, ou utilisez **Choisir avec le Finder…** dans l'app. Les agents lisent
  et modifient alors les fichiers de ce dossier. Chaque modification s'affiche en diff coloré
  (au centre de l'app et dans le fil) et attend **Autoriser / Refuser**.
- Dans l'app, la colonne centrale montre l'arborescence et le contenu des fichiers, et se met à jour
  après chaque modification acceptée.
- Seuls les dossiers situés dans votre dossier personnel sont acceptés (ni ~ lui-même, ni ~/Library).
- **Modèle** (menu dans la zone de saisie, ou `/modele`) :
  Auto (Gemma 4 pilote, Qwen2.5-coder écrit le code), Gemma 4 pour tout (le plus fiable),
  Qwen2.5-coder pour tout (la vision reste sur Gemma ; Qwen est moins fiable pour enchaîner les outils).

## Utilisation commune

- **Modes** : équipe (plan → agents → revue → critique), trading pro (cotations réelles, multi-timeframe, market structure, risk management), recherche, navigateur automatique, code, revue,
  système (Mac, mémoire, compétences, tâches planifiées, MCP), mon onglet (extension).
- **Raccourcis** (app et extension) : entrée envoie, maj+entrée nouvelle ligne, maj+tab change de mode,
  échap arrête, 1 / 2 répond à une demande d'autorisation.
- Une seule tâche à la fois : les modèles locaux partagent le même processeur graphique.

## Sécurité

- Le pont écoute **uniquement sur 127.0.0.1:8765** (`ARI_BRIDGE_PORT` pour changer), jamais sur le réseau.
- Chaque appel exige la clé secrète `.ari/bridge_token` (créée au premier lancement, lisible par vous seul).
- Les requêtes venant de sites web sont refusées (contrôle de l'origine et de l'hôte, contre le DNS
  rebinding). L'interface servie a une CSP stricte et ne peut pas être intégrée dans un autre site.
- Les actions difficiles à annuler (commande, envoi, achat, connexion, publication, suppression…) attendent
  votre accord, sauf en mode **sans confirmation**. L'agent ne tape jamais de mot de passe.
- Le contenu des pages est transmis à l'agent comme une donnée, jamais comme des instructions. Une page
  peut tout de même tenter de manipuler l'agent : gardez les confirmations sur les sites que vous ne
  connaissez pas.
- L'app n'est pas signée par un développeur Apple (signature locale) : usage personnel sur ce Mac.

## Structure

```
ari_bridge/server.py       serveur HTTP local, clé, contrôle d'origine, flux d'événements
ari_bridge/jobs.py         exécution des tâches avec le cœur, confirmations vers l'interface
ari_bridge/tab_browser.py  outils navigateur du cœur, exécutés dans l'onglet réel par l'extension
ari_bridge/bus.py          diffusion des événements, questions / réponses
ari_bridge/tui.py          interface terminal (./ari-code)
extension/                 extension MV3 (panneau latéral) + interface partagée avec l'app
macapp/                    app Swift (fenêtre WebKit), icône, build.sh → dist/AgenticAri.dmg
```

## Version finale (octobre 2026)

- **Hub d'Ari** (icône en grille, ou menu + › Hub d'Ari) : accueil, fournisseurs (Ollama, LM Studio, MLX, llama.cpp, Apple, OmniRoute, Ari Cloud), modèles, routage (qui fait quoi), OmniRoute, créations, santé, journaux, usage et quotas.
- **Créer** (menu + › Créer) : Word, Excel, PowerPoint, PDF, retouche photo, fond enlevé, vidéo à partir d'images, vidéo IA et image IA (ces deux-là passent par Ari Cloud). Fichiers dans `~/Documents/Ari`.
- **Voix d'Ari** (menu + › Voix d'Ari) : lecture pendant que la réponse s'écrit, voix choisie selon la langue de chaque phrase (Piper fr/de/en, voix du Mac pour les autres langues, voix « Premium » du Mac en priorité si elles sont téléchargées).
- **Tableau de bord trading** (icône graphique) : prix XAUUSD Capital.com, zones d'entrée, TP1/TP2/TP3, styles, stratégies avec interrupteurs, checklist 7 blocs, multi-unités, risque, Binance / Kraken / Freqtrade / OctoBot en lecture seule.
- **OctoBot** (icône OctoBot) : son interface dans le canevas. **OmniRoute** : son tableau de bord dans une fenêtre de l'app.
- Sécurité : clés (Binance, OmniRoute) vérifiées puis rangées dans le Trousseau, jamais réaffichées ; seule la page d'Ari peut parler à l'app Mac.

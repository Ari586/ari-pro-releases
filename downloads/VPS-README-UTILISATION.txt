PACK ARI PRO VPS — INSTALLATION STABLE 1.21

Ce pack contient uniquement les fichiers nécessaires au relais VPS Windows :
- AriBridge-1.21.ex5 et AriBridge.ex5 : EA compilé pour MetaTrader 5 ;
- AriBridge.mq5 : source MQL5 ;
- AriRelay.py : relais entre ARI Pro et MetaTrader 5 ;
- Lancer AriRelay.bat : démarrage manuel ;
- Demarrage auto AriRelay.bat : démarrage automatique Windows ;
- LISEZMOI.txt : installation ;
- MISE-A-JOUR-ARIBRIDGE-1.21.txt : mise à jour sans double instance.

INSTALLATION SÛRE
1. Copier le pack sur le VPS Windows.
2. Installer Python 3 avec le lanceur « py ».
3. Dans MT5, ouvrir le dossier des données puis MQL5/Experts.
4. Copier AriBridge-1.21.ex5 et attacher une seule instance au graphique prévu.
5. Activer Algo Trading et vérifier le compte, le serveur et le symbole.
6. Lancer « Lancer AriRelay.bat » pour un test ou « Demarrage auto AriRelay.bat » pour la continuité.
7. Ouvrir le port TCP 8787 (et 443 si nécessaire) dans le pare-feu Windows et chez l’hébergeur.
8. Dans ARI Pro, saisir l’IP du VPS et le code affiché par AriRelay.

CONTINUITÉ DE SERVICE
- Utiliser un compte démo pour valider l’installation.
- Ne jamais lancer deux AriBridge sur le même dossier commun.
- Ne pas modifier WikiFXDefense ou les autres EA du terminal.
- Vérifier chaque jour le journal Experts de MT5, le processus AriRelay, Algo Trading, le réseau, la marge et les ordres en attente.
- En cas de panne : arrêter les nouveaux signaux, contrôler les positions, relancer AriRelay une seule fois, puis tester la connexion ARI.
- Désactiver l’EA ne clôture pas les positions déjà ouvertes.

CE PACK NE CONTIENT PAS
- de conversations ;
- de mémoire ARI ;
- de cache personnel ;
- de clé API, mot de passe, token ou code AriRelay prérempli.

Ne jamais partager le code AriRelay ou les identifiants du VPS.

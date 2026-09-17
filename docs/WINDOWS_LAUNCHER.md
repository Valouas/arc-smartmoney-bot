# Lanceur Windows de l'ensemble du bot

Double-cliquer `LANCER_BOT.bat` dans le dossier du depot. Le fichier se place
automatiquement dans ce dossier et utilise `.venv\Scripts\python.exe`.
Il ne fait pas de `git pull`, ne modifie pas `.env`, les seuils ou les portefeuilles.

Le lanceur regenere les exports historiques sans collecte reseau, puis demarre :

- launch_indexer, wallet_profiler, auto_discovery et bot ;
- historical_analysis si HISTORICAL_ANALYSIS_ENABLED=true ;
- alchemy_webhook si ALCHEMY_WEBHOOK_ENABLED=true ;
- paper_copy avec la selection paper configuree par defaut.

Le tunnel ngrok local existant vers le port du webhook est reutilise. Son URL
reste donc celle deja configuree dans Alchemy. Sans tunnel correspondant, ngrok
est lance s'il est dans PATH, avec son authentification deja enregistree. La
nouvelle URL est affichee ; si elle change, il faut la reporter dans Alchemy.
Si ngrok manque ou s'arrete, les scanners RPC continuent.

Le fichier `data/alchemy_addresses.json` contient la liste complete a surveiller.
La synchronisation des adresses dans Alchemy reste manuelle : la signing key
authentifie les notifications et ne remplace pas un acces de gestion Alchemy.

`ETAT_BOT.bat` affiche l'etat du superviseur, du V4 et des portefeuilles paper.
Les journaux se trouvent dans `data/launcher_logs/`, un fichier par composant.

`ARRETER_BOT.bat` demande l'arret du superviseur via un fichier local. Chaque
processus cree recoit un signal d'interruption, dispose de 25 secondes pour
fermer ses transactions et liberer ses verrous, puis peut etre termine si bloque.
Le lanceur ne termine que ses propres enfants. Un ngrok preexistant est conserve.
Ctrl+C dans la fenetre du lanceur a le meme effet. Eviter de fermer brutalement
la console pendant une ecriture ; SQLite et les checkpoints restent persistants.

Un double lancement est refuse. Lors du premier passage depuis les anciennes
fenetres manuelles, les arreter avec Ctrl+C avant d'utiliser le fichier .bat.
Ne pas lancer `run_v4.py` ou `paper_copy` manuellement en parallele du lanceur.

Pour mettre a jour le code : arreter l'ensemble, faire `git pull --ff-only`, puis
relancer le .bat. La preparation regenere la selection ; aucun achat historique
n'est rempli retroactivement et aucun portefeuille n'est recredite a 1 000.

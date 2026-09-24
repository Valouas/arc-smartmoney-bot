# Lanceur Windows de l'ensemble du bot

Double-cliquer `LANCER_BOT.bat` dans le dossier du depot. Le fichier se place
automatiquement dans ce dossier et utilise `.venv\Scripts\python.exe`.
Il ne fait pas de `git pull`, ne modifie pas `.env`, les seuils ou les portefeuilles.

Le lanceur regenere les exports historiques sans collecte reseau, puis demarre :

- launch_indexer, wallet_profiler, auto_discovery et bot ;
- historical_analysis si HISTORICAL_ANALYSIS_ENABLED=true ;
- alchemy_webhook si ALCHEMY_WEBHOOK_ENABLED=true ;
- paper_copy avec la selection paper configuree par defaut.
- dashboard sur http://127.0.0.1:8001 (lecture seule).

`TABLEAU_DE_BORD.bat` ouvre le navigateur sur le tableau de bord. Le serveur est
lance par `LANCER_BOT.bat` et arrete avec le reste. Il n'est pas expose par ngrok :
le tunnel reste reserve au webhook sur le port 8000. Une panne du tableau de bord
n'arrete pas les scanners.

Le tableau de bord lit les deux bases en mode SQLite read-only, avec un cache de
10 secondes, sans RPC ni modification des selections. Il presente les comptes
paper, positions ouvertes, dernieres executions, courbe quotidienne du PnL realise,
statuts moteur, progression historique et checkpoints. Les photos historiques
proviennent du dernier CSV exporte et affichent sa date. Elles ne sont pas
recalculees par la page. Le PnL latent et la valeur de marche restent inconnus.
La courbe est reconstituee depuis les fills, donc persiste apres redemarrage ;
avant la premiere execution elle reste vide. Les composants affiches sont ceux
declares par le superviseur ; apres 15 secondes sans heartbeat leur etat est inconnu.

Le tunnel ngrok local existant vers le port du webhook est reutilise. Son URL
reste donc celle deja configuree dans Alchemy. Sans tunnel correspondant, ngrok
est lance s'il est dans PATH, avec son authentification deja enregistree. La
nouvelle URL est affichee ; si elle change, il faut la reporter dans Alchemy.
Si ngrok manque ou s'arrete, les scanners RPC continuent.

Le fichier `data/alchemy_addresses.json` contient la liste complete a surveiller.
Avec `ALCHEMY_SYNC_ENABLED=true` et le token Notify configure, `alchemy_sync`
ajoute automatiquement les wallets paper a Alchemy (voir [PAPER_LIVE.md](PAPER_LIVE.md)).
Sinon la synchronisation reste manuelle : la signing key seule ne donne aucun
acces de gestion Alchemy.

`ETAT_BOT.bat` affiche l'etat du superviseur, du V4 et des portefeuilles paper.
Les journaux se trouvent dans `data/launcher_logs/`, un fichier par composant.

Le journal `data/launcher_logs/supervisor.jsonl` conserve les demarrages, les
sorties de composants, leurs codes, la raison de l'arret (commande via fichier,
interruption clavier, composant principal arrete, exception) et les fermetures
forcees apres le delai de grace. Les erreurs indiquent categorie, operation,
errno/winerror et emplacement de code, sans message brut, commande ou secret.

Sous Windows, un lecteur peut temporairement empecher le remplacement atomique
de `data/run_all_state.json`. Le lanceur retente trois fois, puis conserve les
bots actifs et retente au cycle suivant. Il journalise cette degradation au plus
une fois par minute et son retablissement. Le dashboard affichera un etat inconnu
si son heartbeat devient perime. Cette tolerance concerne uniquement le fichier
d'affichage et le journal du superviseur ; les bases et les donnees de trading
conservent leurs garde-fous. Un arret de securite du paper ou une perte de la
protection des sauvegardes continue d'arreter l'ensemble gere.

### Panne de l'analyse historique

Une sortie de `historical_analysis` ne coupe plus le paper live, le recepteur
webhook ni les autres collecteurs. Le superviseur tente au maximum trois reprises
par lancement, apres 5, 30 puis 120 secondes, sans bloquer son heartbeat.
Chaque reprise utilise la file et les checkpoints persistants, sans preparation
des exports, effacement des donnees ou modification du contexte de decodage.
Si un ancien processus detient encore un verrou historique, aucune reprise
n'est lancee. Apres epuisement des essais, le composant reste STOPPED dans le
dashboard et le journal indique `historical_recovery_stopped` ; le paper reste
actif. Une intervention manuelle est alors necessaire pour l'analyse.

Tous les workers du lanceur activent `-X faulthandler` : en cas de crash natif,
Python tente de journaliser les piles d'appels sans variables locales ni corps
des requetes. Cela ameliore le diagnostic mais ne garantit pas de capturer toute
panne native. Le crash Windows du 20 septembre 2026 a produit `0xC0000005` a
l'adresse `0x2` ; sa cause native exacte n'est pas identifiee. Cette isolation
ne doit pas etre presentee comme une correction de cette cause.

Apres un arret prolonge, le paper reprend son checkpoint live existant et
rattrape les blocs manquants. Il ne saute pas automatiquement a la tete de
chaine et ne remplit pas retroactivement les signaux trop anciens. Verifier le
retard dans le dashboard avant de considerer le suivi comme a jour.

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

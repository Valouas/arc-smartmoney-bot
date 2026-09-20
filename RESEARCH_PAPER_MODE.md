# Recherche historique et copie paper live

`LANCER_BOT.bat` / `python run_all.py` peut fonctionner avec :

```dotenv
RESEARCH_PAPER_ONLY=true
HISTORICAL_ANALYSIS_ENABLED=true
PAPER_LIVE_ENABLED=true
PAPER_LOG_QUERY_BLOCKS=100
PAPER_CATCHUP_WINDOW_BLOCKS=1000
```

Ce mode suspend explicitement `bot.py` et `wallet_profiler.py`, les consommateurs
du parcours V4 bloc par bloc. Leur checkpoint et leurs données restent conservés.
Le scoring forward V4 n'est donc plus alimenté par ce scanner tant que ce mode
est actif. La découverte des tokens, des acheteurs précoces, l'analyse des
historiques de wallets, le webhook, la synchronisation Alchemy, la copie paper,
le dashboard et les sauvegardes configurées continuent. Aucun seuil de sélection
ou de sécurité ne change. `run_v4.py` seul n'est pas le lanceur du paper séparé.

Les valeurs par défaut restent compatibles avec le fonctionnement précédent :
mode désactivé, requêtes de 10 blocs, fenêtre de 40 blocs. Activer 100 blocs
seulement après vérification que le provider les accepte. Un refus JSON-RPC
reconnu de taille/plage provoque une subdivision bornée jusqu'à 10 blocs ; une
erreur HTTP, un timeout ou une réponse malformée reste un échec, jamais une
preuve d'absence de logs. La limite apprise reste attachée au provider pour
éviter de retester une plage trop grande à chaque cycle.

Toutes les sous-requêtes sont validées avant couverture de la fenêtre. Les blocs
actifs sont traités dans l'ordre, avec vérification des reçus, transactions et
ancres de chaîne. La reprise conserve le bloc non terminé. Un signal ancien ne
peut pas devenir une exécution paper rétroactive. Les montants, le dimensionnement,
les frais, la déduplication et la politique paper persistée ne changent pas.

La file historique sert en priorité les dossiers qui satisfont les critères
paper sauf expiration/fraîcheur, puis ceux actuellement admis. Un créneau sur
quatre reste réservé au wallet servi le moins récemment. Le délai après erreur
transport reste obligatoire. L'ordre de collecte ne constitue jamais une
admission ; seul le rapport validé habituel renouvelle la sélection.

Dans chaque niveau de priorité paper, terminer d'abord les dossiers en DECODING
ayant le moins de transactions encore à reconstruire. Le total d'un dossier
FETCHING n'est pas encore connu et ne permet pas d'estimer sa taille finale.
Les rafales restent bornées, un créneau sur quatre reste équitable et une erreur
libère la priorité pendant le délai de reprise. Aucun wallet n'est supprimé.

## Validation locale du 20 septembre 2026

Deux requêtes publiques de 100 blocs ont donné exactement les mêmes logs que
vingt requêtes de 10 blocs sur la même plage (vide pour les wallets concernés).
Les tests déterministes couvrent séparément les logs non vides aux frontières,
le découpage après limite de plage, les échecs, la reprise, les swaps confirmés,
les doublons et le rejet des signaux périmés. Sur 1 000 blocs vides et un lot de
wallets, 100 blocs par requête nécessitent 20 appels getLogs contre 200 auparavant.
Cela ne signifie pas une accélération globale garantie de dix fois.

Trois transactions ambiguës des principaux dossiers ont été rejouées en lecture
seule avec le décodeur existant : une direction BUY/SELL est observable mais la
preuve de swap reste non confirmée (`confirmation_eligible=false`). Cela ne
démontre pas un bug du décodeur. Les métadonnées brutes anciennes étaient absentes
du cache pour les erreurs examinées des dix premiers dossiers ; cette absence
ne signifie pas que leurs analyses sauvegardées ont été supprimées. Pas de
réécriture, de validation forcée ni d'invalidation de génération dans ce changement.

Les modules participant à l'empreinte de reconstruction restent inchangés.
Les limites RPC globales ne sont pas augmentées par ces options. Comparer le
retard paper, les dossiers terminés et les coûts avant de demander plus de débit.

## Activer ou revenir au mode précédent

Arrêter proprement avec `ARRETER_BOT.bat`, modifier uniquement les variables
ci-dessus dans `.env`, puis relancer `LANCER_BOT.bat`. Pour reprendre le scanner
V4 à son checkpoint conservé : `RESEARCH_PAPER_ONLY=false`. Ne supprimer aucune
base, aucun fichier d'état et aucun checkpoint pour changer de mode.

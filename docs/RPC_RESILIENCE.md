# RPC public : débit, retries et reprise

Le transport de tous les `ArcRPC` protège chaque requête HTTP JSON-RPC, y compris
les appels internes de Web3 (`eth_chainId`, `eth_call`) et ceux d'auto-discovery.
Le retry Arcscan REST reste indépendant. Scoring, seuils, promotion, read-only et
paper mode restent inchangés.

## Configuration .env

```dotenv
ARC_RPC_URL=https://rpc.mainnet.arc.io
ARC_RPC_FALLBACK_URLS=https://rpc.arc-scan.org
ARC_RPC_REQUESTS_PER_SECOND=2
ARC_RPC_FALLBACK_REQUESTS_PER_SECOND=2
ARC_RPC_RATE_LIMIT_FILE=data/rpc_rate_limit.sqlite3
ARC_CHAIN_ID=5042
```

Le fallback est **optionnel**, désactivé par défaut (valeur vide). Plusieurs URLs
peuvent être séparées par des virgules. Ne recopiez pas `.env.example` sur votre
configuration existante. Le défaut de l'URL primaire existante reste inchangé.

Le budget par défaut est de **2 requêtes par seconde pour le primary**, sans rafale,
partagé entre processus et instances grâce à un fichier SQLite distinct de la
base métier. Tous les fallbacks partagent un second budget, également de
**2 requêtes par seconde** par défaut, dans le fichier portant le même nom suivi
de `.fallback`. Chaque requête, tentative et contrôle de chaîne consomme une place
dans le budget de son provider. Le cooldown `Retry-After` est partagé entre les
processus sur ce budget ; le processus qui bascule respecte aussi ce délai.
Tous les processus doivent employer le même chemin absolu (un chemin relatif est
résolu depuis le répertoire de lancement) et le même débit. `run_v4.py` les lance
dans le même répertoire. Le fichier doit être sur un disque local ; des machines
distinctes ne partagent pas ce budget. Ne le supprimez pas pendant l'exécution.

Ce réglage conservateur réduit la pression sur un RPC public sans garantir
l'absence de 429 (quotas partagés, limites par minute et autres clients possibles).
Il peut ralentir le rattrapage : augmentez-le seulement selon le quota réellement
accordé par votre fournisseur. Les quatre processus peuvent continuer à vivre
pendant que le scanner attend ; cela ne signifie pas qu'il suit le head en temps réel.

## Erreurs et fallback

- Au maximum **3 tentatives HTTP par provider et par appel** : timeout, erreur de
  connexion, HTTP 429, 500, 502, 503, 504 seulement. Web3 n'ajoute pas de retries.
- Attentes locales : 0,5 s puis 1 s, chacune avec jitter de 0 à 0,25 s.
  `Retry-After` (secondes ou date HTTP) est un minimum, même avant un fallback.
  Le limiteur peut ajouter une attente. Le nombre de tentatives est borné ; un
  `Retry-After` long peut prolonger le temps total. Le délai reçu est également
  conservé dans le budget partagé, même à la dernière tentative : les autres
  processus et le prochain cycle ne peuvent pas l'ignorer en recommençant aussitôt.
- Aucune redirection HTTP. Aucun retry/fallback sur les autres 4xx, les réponses
  JSON-RPC d'erreur, le JSON invalide ou une réponse malformée/partielle, sauf
  le refus mensuel Alchemy décrit ci-dessous.
- Le fallback n'est essayé qu'après épuisement d'une erreur transitoire. Sa
  chaîne doit être celle configurée, **5042 pour Arc**, avant tout appel métier.
  Un mauvais chain id est refusé. Après succès, il reste actif dans ce processus
  jusqu'au redémarrage ; aucun aller-retour automatique ne surcharge le primary.
- La requête entière et ses paramètres sont rejoués. Jamais d'erreur convertie en
  `[]`, ni d'assemblage d'une réponse partielle. `eth_getLogs` vide n'est accepté
  qu'après une vraie réponse JSON-RPC valide contenant `result: []`.
- Les warnings du transport contiennent seulement méthode, catégorie, tentative
  et label `primary`/`fallback-N`. Aucun URL, credential ou corps de réponse.

### Alchemy principal et RPC public de secours

Dans votre `.env` local uniquement, utilisez l'endpoint HTTPS Arc Mainnet Alchemy
comme `ARC_RPC_URL` et gardez l'ancien RPC dans
`ARC_RPC_FALLBACK_URLS=https://rpc.mainnet.arc.io`. Ne commitez jamais la clé.
Le changement prend effet au prochain redémarrage de `run_v4.py` ; il ne modifie
ni les curseurs, ni les checkpoints. Pour un compte Alchemy affichant 300 CU/s :

```dotenv
ARC_RPC_REQUESTS_PER_SECOND=5
ARC_RPC_FALLBACK_REQUESTS_PER_SECOND=2
```

Le coût le plus élevé des méthodes actuellement utilisées est `eth_getLogs`,
60 CU d'après le [barème Alchemy](https://www.alchemy.com/docs/reference/compute-unit-costs).
5 appels/s correspondent donc au plafond de 300 CU/s même si tous demandent
des logs. C'est une limite fixe adaptée au cas le plus coûteux, pas un ordonnanceur
pondéré en CU : des méthodes moins chères pourraient autoriser davantage d'appels.
Le quota Alchemy est partagé avec les autres usages du même compte. Ce réglage
ne garantit ni l'absence de 429, ni le rattrapage du live ; le quota mensuel est
aussi consommé plus vite (30 millions de CU font environ 27,8 h à 300 CU/s constants).
Le secours public reste limité à 2 appels/s même si d'autres processus utilisent
encore Alchemy. Arrêtez puis relancez les quatre processus ensemble pour charger
les mêmes limites et éviter de mélanger des versions du limiteur.

Les HTTP 429 déclenchent les trois tentatives habituelles puis le fallback.
Alchemy documente aussi le quota mensuel épuisé comme HTTP 403, code JSON-RPC
`-32600`, message exact `Monthly capacity limit exceeded.` :
[référence officielle](https://www.alchemy.com/docs/reference/error-reference).
Ce refus explicite sur un endpoint HTTPS `*.g.alchemy.com`, avec enveloppe et
identifiant de requête valides, permet un fallback immédiat sans retenter un
quota mensuel épuisé. `Retry-After` reste respecté et la chaîne du secours est
vérifiée avant tout appel métier. La catégorie journalisée est `monthly_capacity`.
Les autres 403 (clé, réseau non activé, origine interdite), erreurs ambiguës ou
réponses partielles restent bloquantes. Sans secours utilisable, une `RPCError`
est levée et le bloc reste à reprendre. Chaque processus conserve son secours
après succès jusqu'au redémarrage ; le quota mensuel n'est pas sondé en boucle.

Un bloc en cours est enregistré comme `pending_block` avant les appels réseau.
Ce marqueur **ne signifie pas que le bloc est terminé**. Le checkpoint
`next_block` n'avance qu'après tous les appels et traitements du bloc, dans la
transaction qui enregistre sa couverture. `Storage.next_block()` privilégie un
bloc en attente, y compris après redémarrage au premier bloc en mode `latest`.
L'état persistant est prioritaire ; `START_BLOCK` choisit le départ uniquement
quand la base ne contient aucun état de reprise.
Les transactions et événements déjà enregistrés gardent leurs clés d'idempotence
pendant la reprise ; aucun bloc échoué n'est sauté. Un transport indisponible
pour symbol/name/decimals/supply ne peut plus devenir une métadonnée par défaut.

## Optimisation multi-blocs différée

`watched_participants()` envoie deux `eth_getLogs` par bloc et par lot de 100
wallets : un filtre sur `from`, un autre sur `to`. Les topics Ethereum font un
AND entre positions ; les fusionner en un seul filtre perdrait les transferts
dont un seul côté est surveillé. Le résultat actuel fusionne déjà les participants
sans doublonner les transactions.

Une fenêtre de plusieurs blocs réduirait ces appels, mais nécessite une taille
adaptative aux limites du fournisseur, une détection de réponses tronquées,
l'invalidation du cache lors d'un changement de watchlist ou d'une réorganisation,
et un contrôle des hashes entre logs/blocs/receipts. Cette PR conserve le
traitement ordonné bloc par bloc et le checkpoint après traitement complet.
Le scanner demande déjà ses logs par `blockHash` et vérifie que chaque receipt
appartient au bloc chargé : un désaccord entre providers bloque le checkpoint.
La vérification du chain id d'un fallback ne prouve pas à elle seule l'absence
de réorganisation ou une histoire identique entre fournisseurs.

## Vérification PowerShell

Arrêtez le `run_v4.py` existant avec Ctrl+C avant de récupérer le code, puis :

```powershell
git switch fix/issue-1-v4-audit
git pull --ff-only origin fix/issue-1-v4-audit
.\.venv\Scripts\python.exe -m pytest tests/test_rpc_transport.py -q
.\.venv\Scripts\python.exe rpc_check.py
.\.venv\Scripts\python.exe run_v4.py
```

`rpc_check.py` lit votre configuration, respecte le budget partagé, vérifie la
chaîne, un bloc récent, les logs USDC, un receipt si disponible, le code et les
métadonnées USDC. Il ne démarre aucun scanner, n'envoie aucune alerte et ne touche
pas la base métier. Il affiche `RPC OK` ou sort avec le code 1 en cas d'échec.

Dans un autre terminal du même répertoire :

```powershell
.\.venv\Scripts\python.exe status.py
```

Tests publics isolés de `.env` et de votre base, y compris `launch_indexer.py
--once` sur des bases temporaires :

```powershell
$env:ARC_RUN_NETWORK = "1"
.\.venv\Scripts\python.exe -m pytest -m network -v
Remove-Item Env:ARC_RUN_NETWORK
```

## Résultats observés le 16 septembre 2026

- 243 tests déterministes passent, dont 53 tests dédiés au RPC.
- Les contrôles live du nouveau transport passent sur les deux RPC publics,
  y compris les filtres `blockHash`, receipts, code et métadonnées USDC.
- Un passage complet des 13 tests publics a réussi. Le dernier passage termine
  à 12/13 : `launch_indexer --once` sans curseur épuise trois timeouts Arcscan
  REST sur la page 2. Le contrôle ciblé suivant reçoit HTTP 501 sur cette page.
  Le curseur de la dernière page valide reste conservé ; aucune validation n'a
  été assouplie. Les deux scénarios `--once` avec curseur sauvegardé passent.
- Le premier ancien contrôle RPC direct avait reçu HTTP 503 ; il passe maintenant
  par le transport protégé, comme l'application. Les tests publics dépendent de
  la disponibilité des fournisseurs et ne garantissent pas une absence future de 429.
- 56 fichiers Python compilés ; `git diff --check` passe. Avertissement existant
  de dépréciation `websockets.legacy`. Aucun processus de production lancé et
  aucune modification de `.env`, de la base utilisateur ou du registre DEX local.

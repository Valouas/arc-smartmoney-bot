# Alchemy Address Activity : voie live complementaire

Le receiver Python ecoute par defaut sur `127.0.0.1:8000` :

- `GET http://127.0.0.1:8000/health` : 200, sans cle necessaire ;
- `POST http://127.0.0.1:8000/webhook/alchemy` : authentification, validation,
  insertion SQLite durable, puis 200. Aucun RPC n'est effectue dans la requete HTTP.

## Installation et configuration Windows

Aucune nouvelle dependance Python. Les prerequis sont ceux du bot existant.
Ajouter au `.env` local (ne pas remplacer le reste du fichier) :

```dotenv
ALCHEMY_WEBHOOK_ENABLED=true
ALCHEMY_WEBHOOK_HOST=127.0.0.1
ALCHEMY_WEBHOOK_PORT=8000
ALCHEMY_WEBHOOK_SIGNING_KEY=REMPLACER_PAR_LA_SIGNING_KEY_DU_WEBHOOK
```

La signing key vient des details du webhook dans Alchemy. Ce n'est ni la cle
API du RPC ni l'Auth Token de gestion des webhooks. L'exemple du depot garde
`ALCHEMY_WEBHOOK_ENABLED=false` et une cle vide. Si la cle manque ou si le flux
est desactive, les POST retournent 503 : aucun mode public non signe implicite.
`/health` reste disponible avec `ingestion_ready=false` pendant la configuration.

Verifier dans le dashboard Alchemy que **Address Activity / Arc Mainnet** est
bien propose pour votre compte. Le support RPC ne suffit pas a garantir celui
des webhooks. Le parseur attend le format Address Activity documente et
`event.network=ARC_MAINNET` ; un autre reseau/type est refuse. La compatibilite
avec une livraison reelle Arc doit encore etre confirmee avec votre webhook.
La chaine est controlee independamment par le RPC du bot : chain_id 5042.

Arreter le superviseur avec Ctrl+C, puis dans PowerShell :

```powershell
Set-Location "C:\Users\Valen\Desktop\bot\arc_smartmoney_bot\audit-issue-1"
git pull --ff-only origin fix/issue-1-v4-audit
notepad .env
.\.venv\Scripts\python.exe run_v4.py
```

Le superviseur lance le serveur en plus des processus existants lorsque le flag
est actif. Ne pas lancer un second receiver sur le meme port. Pour un diagnostic
manuel seulement, hors receiver du superviseur :

```powershell
.\.venv\Scripts\python.exe alchemy_webhook.py
```

Le receiver seul accepte et persiste ; **bot.py doit tourner** pour traiter la file.

Dans une autre fenetre, apres installation de ngrok et connexion a votre compte :

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"
ngrok config add-authtoken "TON_AUTHTOKEN_NGROK"
ngrok http http://127.0.0.1:8000
```

Dans Alchemy, utiliser l'URL HTTPS affichee par ngrok suivie de `/webhook/alchemy`,
par exemple `https://TON-DOMAINE.ngrok-free.app/webhook/alchemy`.
Ne pas coller `/health` comme URL de destination. Le tunnel expose le service
local `http://127.0.0.1:8000`, et l'endpoint cible exact est
`http://127.0.0.1:8000/webhook/alchemy`. L'URL du tunnel peut changer au redemarrage.

Configurer les adresses surveillees dans Alchemy a partir de :

```powershell
.\.venv\Scripts\python.exe alchemy_webhook.py --addresses
```

Cette commande fournit l'union de la watchlist effective du V4 (seed + generated
+ candidats QUARANTINE observes avec poids zero, dans la limite du scanner) et
des wallets admis pour paper-copy / sorties de positions paper existantes.
Pour obtenir uniquement la liste paper : `python alchemy_webhook.py --paper-addresses`.
Les infrastructures DEX reviewed sont exclues. Ce fichier n'accorde pas d'autorite
aux candidats. Les changements de liste doivent egalement etre reportes dans
Alchemy ; il n'y a pas de synchronisation distante automatique ni de token de
gestion Alchemy stocke ici.

## Authentification et limites

- HMAC SHA-256 sur les **octets bruts** du body, compare en temps constant a
  `X-Alchemy-Signature`. Une reserialisation JSON ne reproduit pas la signature.
- Signature absente/invalide : 401 ; JSON/schema/reseau invalide : 400.
- Content-Length obligatoire, corps limite a 1 MiB et 2 000 activites ; 413 si
  trop volumineux. Compression et transfert chunked non acceptes.
- Au maximum 8 requetes simultanees et delai socket de 5 secondes.
- Cle, signature, URL de requete et corps ne sont jamais journalises. Le body
  complet n'est pas conserve ; seuls digest, identifiants et jobs minimaux le sont.
- SQLite indisponible/occupe : 503, jamais un faux 200 avant persistance.

## Reprise, deduplication et traitement

Tables additives dans la base V4 :

- `alchemy_deliveries` : ID d'evenement Alchemy (`id`, pas `webhookId`), digest,
  date de reception ; un ID reutilise avec un body different est refuse.
- `alchemy_items` : event ID + transaction + log index, ou -1 si absent.
- `alchemy_jobs` : transaction + wallet, statut PENDING/DONE/IGNORED et retry.
- `alchemy_paper_triggers` : transaction + wallet + reception, pour le consommateur
  paper independant. Un wallet admis en paper uniquement n'entre pas pour autant
  dans `alchemy_jobs` ou dans l'autorite du moteur V4.

Les logs multiples d'un swap et les redeliveries ne multiplient pas les jobs.
Le consommateur recupere ensuite le recu **complet** via RPC. Un log notifie ne
constitue donc jamais une partie de swap acceptee comme un swap complet.

`bot.py`, proprietaire du verrou scanner existant, prend au maximum 3 jobs avant
ou entre les blocs du backfill. Aucun second moteur concurrent n'est lance par
le receiver. L'eligibilite du wallet est reverifiee au traitement ; le recu,
la transaction et le hash canonique doivent correspondre. Deux confirmations
sont attendues. Un trigger vieux de plus de 120 secondes est ignore pour cette
voie live : le backfill reste responsable de l'historique. Une erreur RPC garde
le job en attente avec un delai de retry borne a 60 secondes.

Seuls les swaps confirmes par le decodeur/trusted DEX alimentent l'evenement
de trading live. Un transfert simple, un emitter non revu ou un flux ambigu ne
devient pas un BUY/SELL confirme. Les scores, seuils, controles de risque,
anti-Sybil, exclusions infrastructure et PaperTrader existants restent utilises.

La cle existante `processed_events(transaction,wallet,token,side)` est partagee
avec le backfill et persiste avec les evenements/signaux/fills. Une transaction
recue par les deux voies n'est pas traitee deux fois. Le webhook ne marque jamais
`seen_txs`, `wallet_coverage`, `pending_block` ou `next_block` : il ne peut pas faire
sauter l'historique. Une reprise apres commit d'evenement mais avant fin du job
reste idempotente.

Avec le flux active, les clusters sont reconstruits depuis les evenements
persistes dans la fenetre et avant le bloc/index de la transaction courante.
Un achat live ne devient pas une preuve du futur pour un ancien bloc de backfill.
Les anciens evenements sans index ne servent pas de confirmation intra-bloc.

Si le receiver tombe, le superviseur le signale et garde le scanner/backfill
vivant. Relancer le receiver ou le superviseur pour retablir l'entree HTTP.
Ctrl+C laisse le temps au serveur et a ses handlers de fermer proprement avant
le recours eventuel a terminate/kill. Les appels RPC utilisent le quota partage
existant ; la voie live est priorisee entre blocs, sans garantie de latence si
un appel RPC est bloque ou si le budget est epuise.

Le processus independant `python -m paper_copy` lit les triggers paper en lecture
seule et conserve son propre scanner ordonne et ses portefeuilles separes. Il doit
etre lance dans une autre fenetre ; `run_v4.py` ne lance pas ce bot automatiquement.
Voir [fonctionnement paper-copy](PAPER_COPY.md#webhook-alchemy-et-fallback-rpc).
Aucun signer, aucune cle privee ni methode d'envoi de transaction n'est ajoute.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_alchemy_webhook.py -q
.\.venv\Scripts\python.exe status.py
```

`status.py` affiche le nombre de livraisons acceptees et de jobs par statut.
Un 200 HTTP prouve l'acceptation durable, pas une execution paper. Une execution
reste conditionnee aux preuves de swap et aux regles existantes du moteur.
Les tests deterministes utilisent des signatures fictives et une base temporaire.

References officielles consultees :
- https://www.alchemy.com/docs/reference/address-activity-webhook
- https://www.alchemy.com/docs/reference/notify-api-quickstart
- https://www.alchemy.com/docs/reference/feature-support-by-chain
- https://ngrok.com/docs/start

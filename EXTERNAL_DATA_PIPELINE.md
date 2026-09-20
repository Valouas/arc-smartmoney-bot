# Données externes Arc : découverte et comparaison

`Dune -> discovery -> Zerion enrichment -> local policy -> Alchemy live paper`

**EXTERNAL DATA != LIVE SIGNAL AUTHORITY**

Ce changement ajoute une voie facultative. Il ne désactive ni le scanner ni
l'analyse historique, ne change aucun seuil, et ne supprime aucune donnée ou
checkpoint. Pas de clé privée, signer, ordre réel ou broadcast. Les flags sont
désactivés par défaut : aucun coût Dune/Zerion tant qu'ils restent désactivés.

## Sources et contrat attendu

Dune lit uniquement `GET /api/v1/query/{id}/results`, puis les pages de cette
même `execution_id`. Il ne lance jamais `/execute`. La requête existante doit
être revue pour analyser Arc Mainnet uniquement et produire une ligne par
wallet avec ces colonnes :

| Colonne | Format |
|---|---|
| wallet | adresse EVM 0x + 40 hex, non nulle |
| chain_id | entier 5042 obligatoire, jamais déduit d'un nom de requête |
| first_seen_at, last_seen_at | secondes Unix entières ou ISO 8601 avec timezone |
| buy_count, sell_count, trade_count, distinct_tokens | entiers positifs ou nuls |
| volume_usd | nombre fini positif ou nul |
| realized_pnl, roi, win_rate | nombres finis facultatifs ; NULL reste inconnu |

Les dates doivent être ordonnées et antérieures à la fin de l'exécution.
Les compteurs doivent satisfaire `buy_count + sell_count <= trade_count`.
Le SQL doit expliciter les unités de ROI/win_rate ; ces colonnes ne deviennent
jamais des critères de promotion dans le moteur. Il doit trier les résultats
dans l'ordre de découverte souhaité, car le collecteur lit au maximum
`DUNE_MAX_CANDIDATES` lignes par exécution (y compris les lignes rejetées).

La pagination est bornée à 100 pages de 100 lignes maximum. Un plafond volontaire
est indiqué par `selection_limited` ; il ne prétend pas couvrir tout Arc.
Une pagination malformée ou une page échouée annule l'import entier du cycle.
Les lignes avec adresse, chaîne, dates ou nombres invalides sont rejetées et
comptées par catégorie. Les réponses tronquées par le fournisseur sont refusées.
Une lecture répétée du même résultat ne le rend pas plus récent : le rapport
montre sa provenance et utilise la date d'exécution pour signaler son ancienneté.

Zerion interroge `GET /v1/chains/` sans environnement testnet. Un unique objet
doit déclarer `attributes.external_id` égal à **5042** (décimal ou hexadécimal).
Son `id` retourné est utilisé dans `filter[chain_ids]` de
`GET /v1/wallets/{wallet}/pnl?currency=usd`. Aucun slug Arc n'est codé en dur.
Une chaîne absente, ambiguë ou un filtre rejeté produit un échec ; aucune
requête PnL multichaîne de remplacement n'est envoyée.

Authentification HTTP Basic : clé API comme username, password vide.
Le schéma officiel utilise `data.type = pnl`, avec l'adresse dans `data.id`.
Les valeurs sont conservées sans substituer zéro à une absence. Les alias
`realized_gain_percentage`, `unrealized_gain_percentage`, `received`, `sent`
correspondent respectivement à `relative_realized_gain_percentage`,
`relative_unrealized_gain_percentage`, `received_external`, `sent_external`.
`realized_cost_basis`, `net_invested` et un éventuel `cost_basis` restent distincts.
La décomposition par asset est conservée lorsqu'elle est fournie ; son absence
n'est pas remplacée par une liste de positions vides. Le connecteur n'affirme
pas reconstituer des quantités de positions que l'endpoint PnL ne fournit pas.

`data_complete` décrit la présence des six métriques principales (gain total,
réalisé, latent, ROI total, net investi, frais) sans exclusion signalée par le
fournisseur. Cela ne prouve ni une couverture exhaustive des transactions,
ni la rentabilité de copie future. Les champs optionnels absents sont listés.
Les erreurs laissent visibles les dernières métriques, marquées périmées.

Les deux clients sont GET-only, sans redirection, timeout connexion 5 s / lecture
20 s, réponse bornée à 2 MiB, trois tentatives au maximum sur 429/5xx/timeout/
connexion. Backoff exponentiel avec jitter et Retry-After. Si Retry-After dépasse
30 s, le cycle reporte le prochain essai en base au lieu de bloquer ou de retenter
trop tôt. Les catégories d'erreur ne contiennent ni corps, ni URL, ni clé.

## Persistance, connaissance dans le temps et sélection

Trois tables additives dans la DB d'analyse existante :

- `external_wallet_candidates` : identité/provenance Dune et première observation.
- `external_wallet_metrics` : snapshots externes et erreurs, dédupliqués.
- `external_fetch_state` : cadence persistée et reprise après échec.

`observed_at` est l'instant local d'acceptation après réception, jamais le
`first_seen_at` ancien du wallet. `retrieved_at` est la dernière lecture ;
l'exécution Dune et son hash restent conservés. Une observation ne devient
pas rétroactivement une preuve forward.

Quand `EXTERNAL_DISCOVERY_ENABLED=true`, une adresse valide peut être ajoutée
par `INSERT OR IGNORE` à la file `discovery_candidates`, avec statut
`EXTERNAL_CANDIDATE`, zéro winner source et aucun trade/profil/score importé.
Une adresse déjà connue conserve sa date, ses sources et son statut. La file
historique existante peut alors auditer ce dossier selon ses règles actuelles.
Les données externes ne remplacent pas les payloads reconstruits localement.

Le rapport fusionne les sources par adresse normalisée sans additionner leurs
profits. Il affiche `PAPER_SELECTED` uniquement pour les wallets du contrat de
sélection accepté par le consommateur paper existant. Un PnL externe positif ne
produit ni ce contrat, ni un profil autorisé, ni une promotion. Le classement
combiné est descriptif et n'écrit aucun fichier de sélection moteur.

`alchemy_sync.py` reste l'unique synchroniseur. Les nouvelles entrées proviennent
de la sélection paper locale validée, pas de Dune/Zerion. Les wallets déjà admis
ayant encore des sorties paper à surveiller restent suivis selon le comportement
existant. Les entrées distantes manuelles ne sont pas supprimées. Aucun nouveau
webhook n'est créé : `ALCHEMY_WEBHOOK_ID`, `ALCHEMY_WEBHOOK_EXPECTED_URL`, chaîne,
type, activation et signing key doivent correspondre avant tout ajout.

## Configuration Windows

Dans le `.env` local (jamais dans Git) :

```dotenv
EXTERNAL_DISCOVERY_ENABLED=false
DUNE_ENABLED=false
DUNE_API_KEY=
DUNE_QUERY_ID=
DUNE_REFRESH_SECONDS=900
DUNE_MAX_CANDIDATES=500
ZERION_ENABLED=false
ZERION_API_KEY=
ZERION_REFRESH_SECONDS=3600
ZERION_MAX_WALLETS_PER_CYCLE=25
```

Renseigner les clés dans `.env`, pas dans le chat ou une commande PowerShell.
`DUNE_QUERY_ID` doit être l'identifiant numérique de votre requête existante,
pas l'URL du dashboard. Aucune requête Dune Arc n'est inventée par le bot.
Les téléchargements Dune consomment aussi des crédits même sans exécution SQL.
Zerion dépend du plan et des droits de la clé. Les clients n'ont pas été validés
avec vos comptes tant que les clés et la requête ne sont pas configurées.

Pour tester les connecteurs séparément, mettre `DUNE_ENABLED=true` et/ou
`ZERION_ENABLED=true`, mais garder le mode global désactivé :

```powershell
Set-Location "C:\Users\Valen\Desktop\bot\arc_smartmoney_bot\audit-issue-1"
git pull --ff-only origin fix/issue-1-v4-audit
.\.venv\Scripts\python.exe -m pytest tests/test_external_providers.py tests/test_external_pipeline.py -q
.\.venv\Scripts\python.exe zerion_check.py 0x447d965a66e419c4a0a912eaad50a2ca9faf954b
.\.venv\Scripts\python.exe dune_discovery.py --once
.\.venv\Scripts\python.exe external_wallet_report.py --top 20
```

`zerion_check.py` est un diagnostic en lecture seule, sans sauvegarde de métriques.
`dune_discovery.py --once` sauvegarde les observations Dune ; le mode global
désactivé empêche leur ajout à la file locale d'audit. Une DB d'analyse existante
est nécessaire ; une DB paper ou étrangère est refusée avant toute migration.

Pour intégrer ensuite les observations et enrichissements, activer le mode
global dans `.env`, puis exécuter un cycle borné :

```powershell
.\.venv\Scripts\python.exe external_wallet_pipeline.py --once
.\.venv\Scripts\python.exe external_wallet_report.py --top 20
```

Les sorties complètes sont `data/external_wallet_ranking.csv` et
`data/external_wallet_ranking.json`. Le top console ne tronque pas les exports.
Le rapport reste utilisable en lecture seule avec la DB actuelle même si aucune
table externe n'existe encore. N/A veut dire inconnu, pas zéro.

Pour le fonctionnement continu, le lanceur `LANCER_BOT.bat`/`run_all.py` ajoute
le processus externe seulement si le mode global est activé. Arrêter d'abord
avec `ARRETER_BOT.bat`, puis relancer `LANCER_BOT.bat` une seule fois. Ne pas
lancer un second pipeline manuel en parallèle : un verrou partagé le refuse.
Un échec externe ne coupe pas le backfill ni le paper. Désactiver le flag puis
redémarrer le lanceur suffit pour arrêter cette voie, sans effacer les tables.

## Références officielles vérifiées

- [Dune : derniers résultats, coût et pagination](https://docs.dune.com/api-reference/executions/endpoint/get-query-result)
- [Zerion : authentification Basic](https://developers.zerion.io/authentication)
- [Zerion : registre des chaînes et external_id](https://developers.zerion.io/api-reference/chains/get-list-of-all-chains)
- [Zerion : schéma PnL et filtres](https://developers.zerion.io/api-reference/wallets/get-wallet-pnl)

L'implémentation vérifie la disponibilité effective d'Arc via l'API du compte.
Elle ne transforme pas une annonce de support en preuve de couverture complète.

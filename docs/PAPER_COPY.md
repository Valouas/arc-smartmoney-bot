# Bot de copie paper independant

Ce processus observe les nouveaux trades des wallets admis en paper. Il est distinct
du scanner historique et du paper trader historique existant : son portefeuille,
ses positions, ses ordres et son checkpoint sont dans `data/paper_copy.sqlite3`.
Il ne modifie pas la base de l'analyseur et n'est pas ajoute a `run_v4.py`.
Le code commun de transport RPC et de preuve trusted DEX reste reutilise.

## Regles confirmees

- 1 000 USDC virtuels independants par wallet. Avec 5 wallets, la simulation a
  5 portefeuilles de 1 000 USDC, pas un portefeuille partage de 1 000 USDC.
- Achat habituel : 50 USDC. Formule :
  `budget = clamp(50 * achat_source / mediane_achats_precedents, 10, 100)`.
  Un achat 2x plus gros que la mediane donne 100, et un achat 5x plus petit donne 10.
- La mediane provient de `buy_details` de l'export historique lors de l'activation,
  puis des achats source verifies observes avant le nouveau trade. Le trade courant
  n'entre pas dans sa propre reference. Le rafraichissement de selection ne remplace
  pas cette reference par un historique qui pourrait inclure des observations futures.
- Moins de 5 achats exploitables : 10 USDC et confiance LOW ; de 5 a 9 : MEDIUM ;
  a partir de 10 : HIGH. La qualification actuelle devrait deja fournir au moins
  8 achats. Ce fallback ne donne aucune autorite live a un wallet.
- Le budget de 10 a 100 inclut les frais et le gas simules. Si le cash restant
  est inferieur au budget, le debit est reduit a ce cash, mais jamais sous 10 USDC.
  Sinon l'achat est ignore explicitement. Aucun emprunt ni rechargement automatique.
- Ventes proportionnelles : si le wallet source vend 25 % de sa quantite avant
  transaction, le paper vend 25 % de sa propre position copiee. Un solde source
  au bloc precedent, corrige des transferts anterieurs du meme bloc, donne cette
  proportion. Le RPC doit pouvoir lire ce solde historique recent.
- Les achats repetes du meme token s'ajoutent a la position du meme portefeuille.
  Les ventes deduisent leur part du cout d'acquisition. Aucun stop-loss/take-profit.
  Les transferts et airdrops ne sont pas des achats/ventes a copier. Le modele de
  proportion suppose des soldes ERC-20 representes par les evenements Transfer ;
  les tokens a rebase/reflection atypiques ne sont pas modelises avec precision.

## Selection et retrait

Source par defaut : `data/paper_review_selection.json`, produit par l'analyseur
et le ranking historique. Schema 2, `admission_policy=paper_review_v1`, classe
de simulation `PAPER_REVIEW` : cette admission ne change pas le statut historique
WATCH/PROMISING et ne signifie pas HISTORICALLY_QUALIFIED.

Tous les seuils de performance precedents sont conserves, depuis la meme fonction
de regles : 8 achats, 5 positions fermees, 2 tokens profitables, 2 periodes de 6 h
profitables, fermetures etalees sur au moins 6 h, PnL prudent positif et contribution
maximale d'une position de 80 % (valeurs par defaut de la configuration existante).
Le consommateur les recalcule au chargement ; un flag eligible seul ne suffit pas.

Seulement ces cinq motifs deviennent des avertissements pour la selection paper :

- `external_or_unexplained_token_movement` ;
- `gas_cost_unknown` ;
- `unsupported_or_ambiguous_reconstruction` ;
- `insufficient_independent_profitable_tokens` ;
- `profitable_deployer_identity_unknown`.

Leurs metriques historiques restent marquees INCOMPLETE. Le gas inconnu n'est
pas declare nul et les transactions ambigues ne sont pas declarees sans perte.
Le PnL historique sert a choisir une experience, pas a prouver un rendement fiable.
Le rapport paper conserve la classe historique, les reserves et le cluster.

Le parcours du snapshot doit toujours etre termine, sans erreur de collecte,
avec un cutoff d'au plus 48 h et un export non expire. Les nouvelles erreurs,
ventes sans cout d'achat complet, autres identites inconnues/services partages
et infrastructures DEX restent bloquantes. Aucune exclusion generale des smart accounts.
Tous les nouveaux swaps copies restent strictement verifies par le decodeur
trusted DEX et les donnees/prix necessaires a la simulation doivent etre presents.

Plusieurs wallets lies peuvent etre testes dans leurs portefeuilles separes.
Leur cluster est conserve et ils ne deviennent pas des confirmations independantes.
Le faux lien via `0x000...000` est corrige : ce n'est pas un financeur. Les preuves
d'identite anciennes contaminees sont invalidees en lecture seule dans le rapport.
Le cas specifique reste admissible en paper avec `null_funder_identity_unknown`,
mais `identity_known=false`, sans autorite de signal. Les vraies relations de
financement ne sont pas supprimees.

L'export strict `data/historical_selection.json` (schema 1) garde ses exigences
de completude, identite et unicite de cluster. Pour le choisir explicitement :
`python -m paper_copy --selection data/historical_selection.json`.
Dans ce cas, donner aussi ce chemin au receiver via `--paper-selection`.

Une liste vide ne cree aucun portefeuille et ne fait aucun appel RPC. Le processus
reste en attente et relit le fichier toutes les 2 secondes. Un fichier absent,
malforme ou expire desactive les nouveaux achats. Les positions paper existantes
restent dans la base et les ventes de leurs wallets sont encore suivies pour
permettre les sorties. Une selection rafraichie ne remet jamais le cash a 1 000.

Le premier demarrage part du voisinage de la tete de chaine avec 2 confirmations.
Un nouveau wallet n'est suivi qu'a partir du bloc suivant sa premiere activation.
Un retour dans la selection, ou un renouvellement apres expiration, fixe egalement
une nouvelle borne live pour les achats. Les anciens achats en attente ne sont
pas remplis apres cette reactivation ; les positions et le suivi des sorties restent.
Les positions source deja ouvertes ne sont pas achetees retroactivement. Apres
une interruption, la reprise repart du checkpoint propre au paper, jamais de
celui de l'analyseur. Les confirmations ne garantissent pas l'absence de reorg.

## Prix, couts et donnees absentes

Les remplissages sont une simulation, pas des devis de swap executables.
Ils utilisent une nouvelle observation DexScreener apres detection du signal ;
jamais le prix d'execution du wallet copie. Les schemas ne fournissent pas ici
une garantie de fraicheur du prix de marche a la source. L'heure du fetch est
enregistree. La latence de detection et les trades ignores sont visibles dans
la base (`source_at`, `detected_at`, `filled_at`, `reason`).

Hypotheses par defaut :

- frais de swap : 30 bps (0,30 %) ;
- slippage fixe : 50 bps (0,50 %), plus impact estime `notional / liquidite` ;
- gas : 0,01 USDC par remplissage, une hypothese et non une mesure du futur gas ;
- capacite : minimum de 0,1 % de la liquidite et 1 % du volume sur 5 minutes ;
- prix, liquidite et volume doivent etre presents, finis et strictement positifs ;
- delai maximal de copie : 60 secondes apres le bloc source.

Sans prix/capacite exploitable, aucune execution fictive au prix source. Une
cotation absente est retentee jusqu'a expiration. Une vente sans capacite attend
egalement, puis devient ignoree si le signal expire ; sa position reste ouverte.
Un achat trop gros pour la capacite est ignore. Les ordres suivants du meme wallet
attendent un ordre precedent en attente pour conserver l'ordre et son allocation.
Un signal trop ancien est enregistre comme ignore, jamais rempli retroactivement.

Ces hypotheses sont configurables via `--policy chemin.json`, avec les champs
de `paper_copy/policy.py`. Elles sont figees dans chaque base : changer les
hypotheses demande une nouvelle base `--db`, pour ne pas melanger les experiences.
Exemple de fichier JSON partiel : `{"fee_bps":"30","slippage_bps":"50","gas_usdc":"0.01"}`.

## Quotas, reprise et limites

Le bot utilise le RPC `.env` et son fallback, avec les retries et limites partages
existants. Deux processus avec la meme cle partagent toujours le meme quota.
Les recherches de transferts regroupent jusqu'a 40 blocs et 100 wallets par lot
(2 eth_getLogs par lot/fenetre), puis reconstruisent dans l'ordre bloc/transaction.
Les remplissages sont traites apres chaque bloc, sans attendre la fin de fenetre.
Le debit reel depend du RPC, des wallets actifs et des cotations disponibles ;
aucune garantie de rattrapage ou de latence n'est annoncee.

Toutes les donnees requises d'un bloc sont recuperees avant d'enregistrer ses
observations et son checkpoint dans une transaction SQLite. Un echec RPC conserve
le bloc a retenter. Les observations et remplissages ont des identifiants uniques ;
les reprises ne doivent ni dupliquer des achats ni recrediter le portefeuille.
Les swaps ambigus/non verifies sont enregistres comme lacunes sans copie.
Un hash de bloc incoherent arrete la simulation avec un motif persistant a faire
examiner : un redemarrage ne masque pas une reorganisation deja detectee.

Le reporting affiche cash, PnL realise, cout des positions ouvertes et PnL prudent
si leur valeur residuelle etait nulle. Valeur de marche et PnL latent restent
inconnus : une position non vendue ne disparait jamais du rapport. Les prix
indicatifs ne prouvent pas qu'une vente est executable sur un token illiquide.

Aucune cle privee, signature, approbation de token, transaction de swap ou
notification Telegram n'est ajoutee. Les seuls appels reseau lisent les donnees.

## Webhook Alchemy et fallback RPC

Avec `ALCHEMY_WEBHOOK_ENABLED=true`, le meme receiver signe peut maintenant
alimenter le V4 et le paper-copy. Sa file `alchemy_paper_triggers` est distincte
de celle du V4 : seuls les wallets de la selection paper valide et les
wallets ayant encore des sorties/observations paper a traiter y sont routes.
Cela ne promeut aucun WATCH/QUARANTINE et ne donne aucune autorite live au V4.

Le paper-copy lit la base du receiver **en lecture seule**. Il importe les hints
dans sa propre table `webhook_triggers` avec un curseur de lecture durable,
independant des checkpoints blockchain. Il ne consomme pas `events` ou `signals`
du V4 : leurs cours, leurs scores et leur portefeuille ne sont pas ceux de cette
simulation. Les tables sont creees de facon additive au demarrage.

Une notification reveille le scanner au prochain cycle local (2 secondes par
defaut). Le scanner verifie les blocs avec les confirmations existantes, les
recus complets et trusted DEX, puis appelle le moteur de remplissage existant.
Il parcourt **tous les blocs depuis son checkpoint**, dans l'ordre. Il faut aussi
retrouver les transactions dont Alchemy n'a pas envoye de notification : ignorer
un transfert precedent pourrait fausser la proportion d'une vente.

La notification ne remplace donc pas `eth_getLogs`, et ne permet pas de sauter
un retard du scanner paper. Elle permet de limiter les sondages sans activite :
un scan de secours a lieu au plus tard 15 secondes apres le dernier cycle acheve
(hors duree des appels RPC). Pendant un rattrapage ou un trigger en attente,
le scanner continue a chaque cycle local. Si la file est absente, illisible ou
provient d'un ancien receiver, le polling RPC original reste actif.

Les IDs de transaction/wallet sont persistants. Une transaction deja scannee,
un doublon, une relivraison apres redemarrage ou un second passage du scanner ne
cree pas de nouveau remplissage ni de changement de baseline. Les hints expires
ne sont pas des ordres : le scanner de secours reste responsable de la couverture.
Un transfert simple peut etre marque `SCANNED` sans aucun ordre paper.

Le rapport `python -m paper_copy --status` affiche `webhook_triggers` avec
`PENDING`, `SCANNED`, `EXPIRED` ou `IGNORED_NOT_WATCHED`. `SCANNED` signifie que la
transaction a ete reconstruite, pas qu'un achat a ete execute. Le statut des
observations et des fills indique le resultat de la simulation.

Apres cette mise a jour, redemarrer le receiver via Ctrl+C puis `run_v4.py`, en
gardant ngrok ouvert. Mettre a jour manuellement les adresses dans Alchemy avec :

```powershell
.\.venv\Scripts\python.exe alchemy_webhook.py --addresses
# Sous-ensemble paper seulement, pour diagnostic :
.\.venv\Scripts\python.exe alchemy_webhook.py --paper-addresses
```

Puis lancer le paper-copy separement avec la commande habituelle ci-dessous.
Une selection vide affiche `PAPER_WALLETS=0` et attend sans RPC ; aucun
wallet n'est ajoute pour forcer un trade de demonstration.

Options : `--no-webhook` conserve le polling original ; `--webhook-db chemin`
change la source de triggers, sans pouvoir viser la base paper elle-meme. Si
vous utilisez des chemins paper non standards, lancer le receiver avec
`--paper-db chemin --paper-selection chemin` correspondants. Ces options ne
changent pas les criteres de selection ni les hypotheses financieres de la base.

## Commandes PowerShell

Dans une nouvelle fenetre, en laissant l'analyseur tourner :

```powershell
Set-Location "C:\Users\Valen\Desktop\bot\arc_smartmoney_bot\audit-issue-1"
git pull --ff-only origin fix/issue-1-v4-audit
.\.venv\Scripts\python.exe historical_analysis.py --report-only --top 4
.\.venv\Scripts\python.exe -m paper_copy
```

Etat du bot paper, sans reseau ni modification de sa base :

```powershell
.\.venv\Scripts\python.exe -m paper_copy --status
```

Un seul cycle pour diagnostic (ne pas lancer en parallele du worker sur la meme base) :

```powershell
.\.venv\Scripts\python.exe -m paper_copy --once
```

Tests du bot :

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_paper_copy.py -q
```
# RPC log query compatibility

The persisted 40-block processing window is fetched in inclusive subranges of
at most 10 blocks, for both Transfer directions and every wallet batch. This is
a transport bound, not a change to the stored simulation policy or buy sizing.
All subrequests must succeed before processing any block in the window. A later
subrequest failure discards the staged results and preserves the checkpoint;
the next cycle retries the same window. Generic HTTP 400 failures still fail
closed and are not retried by the transport or interpreted as empty logs.

Live reproduction on 2026-09-18: the configured Alchemy Arc mainnet endpoint
(chain ID 5042) rejected a 40-block request at checkpoint 21395350 with HTTP 400,
JSON-RPC -32600, explicitly stating a 10-block free-plan limit. The same filter
over ten blocks succeeded. See `tests/fixtures/alchemy_free_log_range.json` for
the minimal sanitized diagnostic. No endpoint or credentials are stored there.

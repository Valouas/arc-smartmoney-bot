# Analyse historique et selection pour un second bot

Ce bot recherche des wallets regulierement profitables sur un historique termine.
Son retard sur le live n'invalide pas cette mission. Le second bot doit observer
les nouveaux trades et evaluer la copie en paper avec ses propres delais et prix.

## Activation

Ajouter uniquement cette valeur au `.env` local, puis arreter et relancer tout
`run_v4.py` (ne pas ecraser le fichier avec `.env.example`) :

```dotenv
HISTORICAL_ANALYSIS_ENABLED=true
```

Le superviseur ajoute `historical_analysis.py` aux quatre processus existants.
`auto_discovery.py` continue de decouvrir des candidats, mais delegue leur collecte
et qualification au nouveau worker. Son ancienne hydration et promotion forward
ne tournent pas dans ce mode. Les algorithmes de promotion/scoring forward restent
disponibles en mode historique desactive. Aucun ancien statut, evenement, wallet
utilisateur, checkpoint ou profil n'est supprime. Le scanner peut continuer sa
reconstruction sequentielle ; il ne conditionne pas la qualification historique.

## Collecte et completude

Deux tables additives, `historical_runs` et `historical_transactions`, conservent
un snapshot versionne par wallet. Les anciens `history_complete=1` ne suffisent
pas : leur collecte utilisait le seuil de 25 USDC. Une nouvelle collecte est
necessaire, y compris pour les wallets precedemment complets.

- Cutoff fige au minimum de head-2 et du dernier bloc couvert par la premiere
  page Arcscan, date et hash RPC du bloc conserves. Le retard Arcscan est ainsi
  accepte sans pretendre couvrir des blocs encore absents de son index.
- Toutes les pages d'activite depuis le debut doivent etre parcourues, avec
  couverture depuis le bloc 0 jusqu'au cutoff, sans gap, cap ni troncature.
- Une page par passage : curseurs et file de hashes persistent. Les doublons ne
  creent pas de trades supplementaires. Un curseur cyclique ou une ligne sans
  hash bloque le passage sans faire avancer le curseur.
- Jusqu'a `HISTORICAL_TRANSACTIONS_PER_STEP=10` transactions reconstruites par
  wallet et passage. La rotation persistante evite qu'un wallet difficile
  monopolise les premieres places. Les transports echoues restent a reprendre.
- Seuil de collecte historique 1e-18 USDC : les petits achats/ventes positifs sont
  conserves. Le seuil des signaux live reste inchange.
- Meme decodeur, preuves trusted DEX et exigences de reconstruction. Les transferts
  de tokens non expliques par les trades sont signales comme inventaire incomplet.
  Les schemas/swaps ambigus sont conserves comme lacunes, pas comme zero transaction.
- Le hash du cutoff est reverifie avant completion ; une reorganisation invalide
  la generation et provoque une nouvelle collecte. Les erreurs ne completent pas
  un snapshot. `COMPLETE` signifie toutes les transactions examinees, pas absence
  de lacunes : la qualification exige egalement zero lacune.

Le snapshot complet est rafraichi toutes les 6 h (`HISTORICAL_REFRESH_SECONDS`).
Seules les lignes de cette nouvelle collecte sont remplacees lors du rafraichissement.
Une collecte en cours n'est jamais exportee comme qualifiee. Les faits d'identite
sont controles avec l'IdentityGraph existant ; inconnus et services partages ne
deviennent pas des confirmations independantes. Les smart accounts ne sont pas
exclus en fonction de leur code : seules les infrastructures reviewed sont exclues.
Un cout de gas inconnu reste toutefois une lacune pour tout type de wallet.

## Calculs

L'inventaire utilise FIFO, dans l'ordre bloc/index de transaction, avec prise en
charge des achats multiples et ventes partielles. Les transactions dupliquees
sont dedupliquees. Une vente sans cout d'achat complet ne produit pas un profit
suppose. Une position est fermee lorsque la quantite achetee a ete revendue.
Les positions ouvertes gardent leur quantite et cout d'acquisition ; valeur de
marche et PnL latent restent `null` car aucun historique de prix fiable n'est
suppose. Aucun maximum futur de prix n'est utilise.

Les montants nets reconstruits incluent deja les effets des frais/slippage du swap.
Le gas observe est calcule par `gasUsed * effectiveGasPrice / 1e18` lorsque le
wallet est l'emetteur payeur de la transaction Arc. Les transferts uniquement
entrants, payes par un autre emetteur, ne lui imputent pas le gas de ce tiers.
Un swap ou mouvement sortant relaye ne
permet pas ici de connaitre le cout final supporte par le wallet : gas inconnu,
qualification bloquee, observation conservee. Les frais de transactions echouees
et hors trades sont inclus dans la charge globale.

- PnL realise brut : ventes moins cout FIFO des quantites vendues, avant gas.
- PnL realise moins tout le gas observe : estimation conservative affectant aussi
  les frais des positions ouvertes et des transactions hors trades.
- PnL prudent : toutes ventes reconstruites moins tous achats et tout gas observe.
  L'inventaire restant est considere sans valeur pour ce seul test prudent.
- Positions fermees : resultat apres gas observe de leurs achats et ventes.
  Rendement pondere, hit-rate pondere et profit factor sont calcules sur celles-ci.
  Sans pertes realisees, le profit factor reste `null`, avec indicateur explicite.
- Concentration : meilleur resultat de position / somme des resultats positifs,
  afin que les pertes ne produisent pas un denominateur trompeur.
- Capital share : taille de chaque achat / somme des achats du snapshot.

Les quantites viennent du decodeur existant (flottants) ; les calculs FIFO utilisent
Decimal et une tolerance de 1e-12 relative pour les residus de fermeture. Ce rapport
est une analyse de recherche, pas une comptabilite fiscale ni un backtest de copie.

## Qualification explicite, sans autorite forward

Statuts : `WATCH`, `PROMISING`, `HISTORICALLY_QUALIFIED`, `EXCLUDED`.
L'ancien statut discovery est affiche separement et peut rester `QUARANTINE`.
Un manque de trades ou une mauvaise hit-rate ne retire jamais un candidat.

La qualification exige :

- Snapshot versionne complet, sans erreur/lacune et vieux d'au plus 48 h.
- Identite eligible connue, aucune infrastructure DEX reviewed.
- Au moins 8 achats et 5 positions fermees (valeurs des reglages existants
  `PROMOTION_MIN_LAUNCH_BUYS` et `PROFILE_MIN_EVALUABLE_TRADES`).
- Au moins 2 tokens profitables nets et 2 identites de deployeurs independantes
  (`PROMOTION_MIN_WINNERS`). Leurs deploiements viennent de `launch_candidates`,
  source contracts ; les identites de deployeurs inconnues bloquent.
- PnL prudent positif, pas simplement un meilleur prix theorique positif.
- Au moins 2 periodes UTC de 6 h profitables et des fermetures etalees sur au moins
  6 h. Reglages nouveaux explicites : `HISTORICAL_PERIOD_HOURS=6` et
  `HISTORICAL_MIN_PROFITABLE_PERIODS=2`.
- Contribution maximale d'une position de 80 % (`HISTORICAL_MAX_CONCENTRATION=0.8`).

Ces criteres historiques sont distincts des anciens scores forward, qui ne sont
ni abaisses ni presentes comme des preuves historiques. `PROMISING` peut signaler
un resultat realise positif encore incomplet ; les raisons restent affichees.
Ce statut ne donne pas droit a la selection automatique.

## Exports et contrat du second bot

`data/historical_wallet_ranking.csv` et `.json` contiennent tous les candidats et
wallets profiles, avec dates, metriques, positions ouvertes, identites et blocages.
`data/historical_selection.json` est remplace atomiquement a chaque rapport et
contient seulement les wallets qualifies, un maximum par composante d'identite.
La liste complete conserve tous les wallets lies.

Le consommateur doit verifier `schema_version=1`, `chain_id=5042`, le statut
`HISTORICALLY_QUALIFIED`, `paper_selection_eligible`, `expires_at`, le registre
d'infrastructure actuel et ses propres controles. Une entree expire au plus tard
a la prochaine echeance de rafraichissement ou a l'age maximal du snapshot.
Un fichier absent, invalide, une selection vide ou une entree expiree ne donne
aucune autorisation. L'export annonce `purpose=historical_selection_for_paper`,
`live_signal_authority=false`, `requires_consumer_validation=true`.

Il ne remplit jamais `generated_watchlist.json`, `promoted_wallets` ou les profils
forward et n'envoie aucune alerte Telegram. Le second bot n'est pas implemente ici.
La rentabilite historique peut etre selectionnee pour le paper, jamais assimilee
a une performance future garantie ou a une autorisation d'execution reelle.

## Commandes PowerShell

```powershell
.\.venv\Scripts\python.exe historical_analysis.py --once
.\.venv\Scripts\python.exe wallet_ranking.py --historical
.\.venv\Scripts\python.exe wallet_ranking.py --historical --top 20
.\.venv\Scripts\python.exe wallet_ranking.py --historical --top 20 --diverse
.\.venv\Scripts\python.exe status.py
.\.venv\Scripts\python.exe run_v4.py
```

Les commandes de ranking lisent seulement la base et regenerent les exports sans
reseau. `--once` realise un passage borne, pas une analyse instantanee de tous les
wallets. Un verrou interdit deux workers historiques sur la meme base. Ne lancez
pas `--once` en parallele du worker gere par `run_v4.py`.

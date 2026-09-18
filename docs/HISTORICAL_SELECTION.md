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

Les tables additives `historical_runs` et `historical_transactions` conservent
un snapshot versionne par wallet ; `historical_scheduler` persiste la rotation
de travail, sans toucher aux checkpoints du scanner. Les anciens `history_complete=1` ne suffisent
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
  wallet et passage. La rotation reserve deux passages a la reconstruction,
  un a la pagination en cours et un aux nouveaux snapshots/rafraichissements,
  puis recommence. Si une file est vide, sa place va a une autre file active.
  Le maximum de wallets par cycle reste `DISCOVERY_MAX_WALLETS_PER_CYCLE`.
  Dans chaque file, le wallet tente le moins recemment passe en premier, meme
  apres une erreur. La rotation persiste entre cycles et redemarrages, y compris
  avec un seul wallet par cycle. Aucun wallet n'est traite deux fois par cycle.
  Cela termine des historiques sans attendre une premiere page de tous les
  nouveaux wallets. Ce partage porte sur les passages, pas sur un nombre egal
  de requetes : les limitations RPC/Arcscan restent actives et inchangees.
  Les transports echoues restent a reprendre, sans perdre leurs transactions.
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

### Positions perdantes jamais revendues : controle du biais

Un wallet peut vendre seulement ses gains et conserver ses pertes. Le hit rate,
le rendement pondere et le profit factor des positions fermees ne suffisent donc
pas a mesurer sa rentabilite globale. Le PnL prudent existant deduit deja TOUS les
achats et le gas, y compris les achats encore ouverts. Ce controle de qualification
est conserve et devient explicite dans le rapport, sans nouveau seuil arbitraire.

Indicateurs ajoutes dans les exports et la console :

- `open_positions_count` : nombre de tokens avec inventaire restant reconstruit.
- `open_cost_basis_usdc` : cout FIFO des quantites encore detenues, pas leur valeur
  de marche et pas necessairement du capital non recupere (une vente partielle
  peut avoir rembourse tout l'achat).
- `open_capital_share` : ce cout restant / somme de tous les achats observes.
  Ce denominateur est un volume cumule, pas le solde ni les depots du wallet.
- `capital_recovery_ratio` : toutes les ventes / (tous les achats + gas observe).
- `conservative_return` : PnL prudent / (tous les achats + gas observe). C'est un
  scenario de stress avec valeur residuelle nulle, pas un rendement de marche.
- `oldest_open_position_age_hours` et `open_capital_weighted_age_hours` : age des
  lots FIFO restants a la date du cutoff, et age pondere par leur cout restant.
  Une vente retire bien les lots les plus anciens ; l'age n'avance pas simplement
  parce que le meme rapport est relu plus tard. `position_age_as_of` donne la date.
- `open_inventory_risk` : `PROFITS_COVER_OPEN_COST` si le PnL prudent reste
  strictement positif avec inventaire ouvert ; `OPEN_COST_NOT_COVERED` sinon ;
  `NO_OPEN_POSITION` si rien ne reste. Les lacunes donnent `INCOMPLETE_DATA` et
  une analyse en cours `NOT_EVALUATED` (ou null sans metriques exploitables).
- `closed_only_bias` : gains nets positifs sur les positions fermees, mais PnL
  prudent non positif avec inventaire ouvert. Un avertissement explique ce biais.
  Inconnu tant que l'analyse est en cours ou que des lacunes existent.

Exemple : 80 USDC d'achats revendus 96, puis un achat de 100 jamais revendu.
Les positions fermees peuvent afficher 100 % de reussite et +16 USDC. Le resultat
prudent est pourtant -84 USDC hors gas, avec 100 USDC de cout restant. La raison
`open_inventory_not_covered_by_realized_profit` explicite le blocage prudent deja
present. Ce wallet reste observe. Un gros gain de 2 000 USDC avec dix achats ouverts
de 1 USDC laisse au contraire +1 990 USDC dans ce scenario : le nombre de positions
ouvertes n'est pas un filtre. Les autres preuves de repetition restent necessaires.

Prix de marche et pertes latentes restent inconnus sans valorisation fiable.
Une position ancienne ou non revendue n'est pas automatiquement declaree perdante.
Ces indicateurs n'ajoutent aucun appel reseau, ne suppriment aucun candidat et ne
changent ni le scoring forward, ni l'autorite de signal, ni les seuils historiques.

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
ni abaisses ni presentes comme des preuves historiques. La qualification
`HISTORICALLY_QUALIFIED` garde exactement ces exigences.

Le reporting separe maintenant trois dimensions :

- `analysis_status` : `WAITING` (EN ATTENTE), `IN_PROGRESS` (EN ANALYSE),
  `ANALYZED` (ANALYSE). Un snapshot invalide doit recommencer et revient en attente.
- `metrics_status` : `UNAVAILABLE`, `PARTIAL`, `INCOMPLETE` ou `COMPLETE`.
  Des transactions examinees peuvent contenir des lacunes ; ANALYSE ne signifie
  donc pas automatiquement donnees completes ni qualification.
- `performance_stage` : `NOT_EVALUATED` pendant la collecte ; apres analyse,
  `NO_CLOSED_POSITION`, `NO_NET_PROFIT`, `PRELIMINARY_POSITIVE` ou
  `REPEATED_POSITIVE`. Une premiere performance positive apres gas reste
  preliminaire tant que les exigences de repetition, volume de preuves,
  resultat prudent et concentration ci-dessus ne sont pas satisfaites.

Un gain isole de 12 dollars est ainsi `WATCH / PRELIMINARY_POSITIVE`, pas une
preuve de rentabilite reguliere. `PROMISING` est desormais reserve aux snapshots
termines sans erreur de collecte, avec une performance `REPEATED_POSITIVE`
sans lacunes de reconstruction. Il peut encore manquer des controles d'identite
ou de fraicheur pour la qualification. C'est uniquement une etiquette de rapport :
aucun seuil moteur, statut discovery ou droit de signal n'est change. Tous les
wallets WATCH restent suivis, y compris ceux avec pertes ou peu de trades.

Sans transactions exploitables, les metriques sont `null` en JSON, cellules
vides en CSV et `N/D` en console. Un historique termine et reellement vide conserve
de vrais zeros. Les metriques partielles restent marquees `PARTIAL`, sans
evaluation de performance. Le rapport affiche les transactions identifiees,
reconstruites et restantes. Le pourcentage de reconstruction n'est disponible
qu'apres la fin de pagination : avant cela, le nombre final de hashes est inconnu.

`report_reasons` explique l'attente/collecte pour un snapshot non termine ; les
insuffisances de performance ne sont affichees qu'apres analyse. Les raisons
techniques de qualification restent dans `blocking_reasons`, avec
`qualification_evaluated=false` tant que l'analyse n'est pas terminee.
Le resume distingue l'avancement, la qualite des donnees et la performance.

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
forward et n'envoie aucune alerte Telegram. Le consommateur paper independant
est disponible via `python -m paper_copy` ; voir [sa documentation](PAPER_COPY.md).
La rentabilite historique peut etre selectionnee pour le paper, jamais assimilee
a une performance future garantie ou a une autorisation d'execution reelle.

### Admission paper avec reserves (export distinct)

Le rapport produit aussi `data/paper_review_selection.json`, schema 2, utilise
par defaut par le paper-copy. Il conserve exactement les seuils de performance
ci-dessus et accepte uniquement une liste explicite de reserves de reconstruction
et d'identite des deployeurs. Il accepte plusieurs wallets du meme cluster dans
des portefeuilles separes, sans les compter comme preuves independantes.
Voir [regles detaillees](PAPER_COPY.md#selection-et-retrait).

Le classement strict et `paper_selection_eligible` ne sont pas detournes.
Les champs distincts `paper_review_eligible`, `paper_review_warnings` et
`paper_review_blockers` expliquent l'admission de recherche. Aucun seuil de
scoring forward, statut de promotion, formule de sizing, frais ou stop n'est modifie.

L'adresse nulle n'est plus acceptee comme financeur dans IdentityGraph. Les anciens
snapshots contenant une composante fusionnee via cette adresse sont marques
identite non etablie dans le rapport, sans modifier leurs transactions conservees.
Ce correctif ne prouve pas que les wallets ainsi separes sont independants.

## Commandes PowerShell

### Debit de reconstruction

`HISTORICAL_RPC_WORKERS` (1 par defaut, maximum 16) permet de reconstruire
plusieurs transactions independantes en parallele. Chaque thread possede son
client RPC et sa session HTTP : transactions, recus, blocs, metadata et preuves
DEX passent toujours par le budget RPC partage et les retries existants.
Pour un RPC payant, 8 lecteurs constituent un point de depart ; conserver 1 pour
un RPC public. Le fallback conserve son propre budget conservateur.

Le nombre de transactions en vol est borne au nombre de lecteurs. SQLite reste
accessible uniquement depuis le thread principal : les resultats sont enregistres
dans l'ordre de la file existante. Apres un echec, seul le prefixe entierement
valide reste enregistre ; la transaction echouee et les suivantes sont reprises.
La fin du snapshot exige toujours tous les payloads, la verification du bloc de
coupure et les controles d'identite. Le calcul FIFO reste trie par bloc et index
de transaction, independamment de l'ordre d'arrivee des reponses RPC.

`HISTORICAL_POLL_SECONDS` (60 par defaut, minimum 1) regle uniquement la pause
historique entre cycles. Pour accelerer un rattrapage sur un fournisseur payant,
essayer 10 secondes et `HISTORICAL_TRANSACTIONS_PER_STEP=50`. Cela peut aussi
augmenter le nombre de pages Arcscan traitees par minute ; les controles de
pagination et de transport restent inchanges. Les journaux distinguent le temps
de collecte, le temps de rapport et la pause. Les seuils de selection ne changent pas.

Mesure locale du 19 septembre 2026, sur les memes 24 transactions et sans ecriture
en base : 12,688 s avec 1 lecteur, 5,875 s avec 4, 5,688 s avec 8. Les payloads et
metriques sont identiques. Ce petit echantillon n'est pas une garantie de debit
global : latence Arcscan, taille des historiques et activite concurrente restent
variables. Le renouvellement des snapshots existant n'est pas modifie ici.

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

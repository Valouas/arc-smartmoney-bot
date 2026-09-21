# Essai Nansen Arc — recherche uniquement

Le connecteur interroge uniquement les deux routes de lecture PnL Nansen, avec
`chain=arc`. Il ne modifie ni les statuts des wallets, ni le scoring, ni les
selections paper, ni les checkpoints. Aucun achat de credits, signer ou broadcast.
L'essai reste manuel. Le collecteur par lots est une option du superviseur decrite ci-dessous.

## Campagne par lots (apres rechargement et autorisation)

Configuration du nouveau collecteur `nansen_bulk.py` :

```dotenv
NANSEN_BULK_ENABLED=true
NANSEN_BULK_MAX_REQUESTS=20000
```

Le superviseur `run_all.py` / `LANCER_BOT.bat` lance ce collecteur et l'arrete
proprement avec les autres services. Sa panne ne coupe pas le scanner historique.
Le paper reste soumis a `PAPER_COPY_PAUSED`; Nansen ne peut pas le reactiver.

1. Photographier l'univers des wallets connus au demarrage, infrastructure exclue.
   Ne filtrer ni les contrats non listes, ni les wallets peu actifs ou deficitaires.
2. Recuperer le resume de chacun. Une erreur reste visible; elle n'est pas assimilee
   a un wallet non rentable. Trois erreurs de schema consecutives arretent la collecte
   pour verification, plutot que de gaspiller tout le budget.
3. Recuperer les details realises ET ouverts en priorite selon le meilleur PnL deja
   disponible (Nansen realise, Zerion total ou prudent local). C'est seulement un
   ordre de traitement : les autres dossiers restent dans la file.
4. Arreter les appels au plafond persiste ou lorsque le solde tombe a 100 credits.
   Aucun achat, remise a zero quotidienne, recharge ni nouveau tour automatique.

Le scope et la date de fin sont fixes pour reutiliser les 60 pages de l'essai.
Les nouveaux wallets decouverts apres cette photographie ne sont pas ajoutes
silencieusement a son budget. Les compteurs du tableau de bord mesurent cette
campagne; les 19 dossiers precedemment importes restent disponibles pendant la
nouvelle collecte des resumes. Aucun resultat local n'est remplace.

Le cache, la file et les reservations de credits sont persistes dans
`data/nansen_bulk.sqlite3` (WAL, transactions durables). Chaque page est stockee
separement : pas de reecriture d'un enorme JSON a chaque requete. Cette base est
incluse dans les sauvegardes SQLite automatiques. Ne pas la supprimer ni la renommer
pour relancer : cela perdrait le budget cumule et les pages reutilisables.

Les resumes sont importes sous `nansen_summary`, avec positions ouvertes non
verifiees et aucune autorite. Seuls les dossiers dont les deux vues sont entierement
recues sont importes sous `nansen`, toujours comme recherche externe non validee.
Les imports sont idempotents et rejouables apres une interruption entre les deux bases.

```powershell
# Statut local, sans consommer de credits
.\.venv\Scripts\python.exe nansen_bulk.py --status
# Tests locaux de reprise, budgets, sauvegardes et absence d'autorite
.\.venv\Scripts\python.exe -m pytest tests/test_nansen.py tests/test_nansen_bulk.py -q
```

Le tableau de bord affiche les resumes, dossiers detailles, tentatives, erreurs,
solde recu et fraicheur de la campagne. Un processus vivant ne signifie pas qu'un
budget epuise sera depasse : il reste en attente sans nouveaux appels.

Campagne locale du 21 septembre 2026 : 7 554 wallets connus, plafond 20 000
tentatives nouvelles, lancement explicitement autorise par l'utilisateur.
Premiere requete nouvelle facturee 1 credit; solde retourne 20 009. Ces requetes
s'ajoutent aux 60 credits de l'essai. Avec pagination et reserve, ce budget peut ne
pas suffire a recevoir les details de tous les wallets; les dossiers restants
seront conserves en attente.

## Configuration et commandes PowerShell

Enregistrer `NANSEN_API_KEY` dans `.env` (ignore par Git). Ne pas publier la cle.
Depuis le dossier `audit-issue-1` :

```powershell
# Tests deterministes du connecteur, sans API ni cle
.\.venv\Scripts\python.exe -m pytest tests/test_nansen.py -q

# Lire le dernier essai : aucune requete API, aucune ecriture SQLite
.\.venv\Scripts\python.exe nansen_audit.py --report

# Lancement explicite uniquement; une relance reprend le meme essai/cache
.\.venv\Scripts\python.exe nansen_audit.py --run --max-requests 60
```

Le fichier `data/nansen_audit.json` fixe les 20 wallets, la periode et le budget.
Ne pas le supprimer pour relancer : les pages reussies y sont sauvegardees, y
compris celles d'un wallet encore partiel. Le plafond et la periode ne peuvent
pas etre changes accidentellement avec une autre ligne de commande. Aucune
relance automatique apres budget epuise. `data/nansen_audit.report.json` conserve
la comparaison avec les observations Zerion et locales disponibles au debut.

## Garde-fous

- Jusqu'a 60 tentatives, retries compris, et reserve de 5 credits. Prix attendu
  maximal : 1 credit par requete. Arret si les en-tetes de facturation manquent
  ou si le prix change. Aucun rechargement automatique.
- Une tentative est reservee sur disque avant l'appel. Reponse mise en cache
  avant import : une reprise reutilise les pages deja recues.
- Maximum 3 tentatives sur timeout, connexion, 429 et 500/502/503/504. Les longues
  attentes Retry-After sont persistees; les autres erreurs ne sont pas retentees.
- Pagination stricte jusqu'a 10 pages par vue. Doublons de tokens, mauvaise
  chaine, nombres non finis, pages mal formees ou incompletes : rejet.
- Un dossier exige le resume, toutes les pages realisees et toutes les pages des
  positions ouvertes. Aucun dossier partiel n'est importe comme resultat.
- Valeur absente = inconnue, jamais zero presume. Une reponse valide vide signifie
  seulement aucune position renvoyee dans cette couverture.
- Une position ouverte sans prix positif est signalee; sa perte fournie reste
  incluse. Les symboles des tokens ne servent jamais a identifier l'USDC.
- Les emitters DEX reviewed sont exclus; aucun filtre global sur les contrats.
- Import additif dans `external_wallet_metrics`, fournisseur `nansen`, avec
  `data_complete=false` et aucune autorite de signal. Pas de reconstruction
  remplacee, effacee ou reinitialisee.

## Interpretation et limites

Nansen annonce une couverture Arc a partir du **15 juillet 2026**. L'essai ne
pretend donc pas couvrir les transactions anterieures. Les dates, prix, couts
d'acquisition et conventions comptables peuvent differer de Zerion. Le traitement
des frais n'est pas valide par cet essai. Les compteurs fournisseur ne deviennent
pas des achats forward ou des preuves de swaps internes.

Le resume peut etre en cache tandis que les details sont plus recents. Le rapport
signale les ecarts de PnL et de nombre de tokens. Un PnL positif chez Nansen seul
ne valide pas un wallet et ne prouve pas que ses performances sont copiables.

Documentation officielle :

- [Couverture](https://docs.nansen.ai/api/data-coverage)
- [PnL et performance](https://docs.nansen.ai/api/profiler/address-pnl-and-trade-performance)
- [Credits](https://docs.nansen.ai/getting-started/credits)
- [Limites API](https://docs.nansen.ai/getting-started/rate-limits)

## Essai live du 21 septembre 2026

Periode demandee : 2026-07-15 00:00:00 UTC au 2026-09-20 23:10:14 UTC.
Echantillon volontairement diversifie parmi les wallets ayant une reference
Zerion : gains eleves, pertes, pertes ouvertes et faible nombre d'achats Dune.
Ce n'est pas un echantillon aleatoire representatif de tous les candidats.

- 20 wallets selectionnes; **19 dossiers avec toutes les pages recues**.
- **60 requetes reussies / 60 credits observes**, dont les 3 sondages initiaux
  reutilises. Solde declare par Nansen : **10 credits**.
- Deux wallets ont necessite une page supplementaire. Le 20e n'a que son resume
  en cache : arret au plafond, aucune importation de son dossier partiel.
- Parmi les 19 : 15 PnL totaux positifs, 3 negatifs, 1 reponse sans positions.
- **7 des 19 ont un signe de PnL oppose a Zerion**. Il faut expliquer la couverture
  et les conventions avant de remplacer Zerion ou de generaliser la selection.
- 11 ecarts de nombre de tokens entre resume et details, 13 wallets avec au moins
  une position ouverte sans prix positif. Toutes ces reserves restent visibles.

Exemples USD (observations fournisseur, pas performances paper) :

| Wallet | Total Nansen | Total Zerion | Observation |
|---|---:|---:|---|
| `0x447d965a66e419c4a0a912eaad50a2ca9faf954b` | 17 278,12 | 17 299,44 | Proche; prudent local 17 272,10 |
| `0x3381454380d24f6a9913af81e89ab91700aa0012` | 24 668,31 | -45 564,25 | Signe oppose, analyse necessaire |
| `0xf0b443209b93c0c8303f9f46878e3f565e977a90` | 1 946,39 | -21 648,14 | Signe oppose, analyse necessaire |
| `0x02edac14dd500076e92782ad94d59bda9d015127` | 0,00 | 27 689,63 | Nansen ne renvoie aucune position dans la periode |

Conclusion : vitesse utilisable pour une recherche complementaire, mais le test
ne valide pas l'equivalence comptable. Aucun scan massif lance et paper toujours
en pause. Le rapport local conserve les 20 dossiers avec leurs valeurs et reserves.

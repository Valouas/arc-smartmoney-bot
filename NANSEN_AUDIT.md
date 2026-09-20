# Essai Nansen Arc — recherche uniquement

Le connecteur interroge uniquement les deux routes de lecture PnL Nansen, avec
`chain=arc`. Il ne modifie ni les statuts des wallets, ni le scoring, ni les
selections paper, ni les checkpoints. Aucun achat de credits, signer ou broadcast.
Il ne tourne pas automatiquement dans le superviseur.

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

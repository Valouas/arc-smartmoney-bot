# Acheteurs avant une hausse observée d'au moins x10

`dune_history.py` complète la découverte existante. Il exécute explicitement une
requête Dune préparée, collecte toutes ses pages et ajoute les wallets à la file
d'analyse locale. Il ne change aucun seuil de promotion ou d'admission paper.
Un wallet avec un seul achat éligible suffit ; aucune fréquence minimale de
trading n'est imposée. Un gain observé du token n'est pas un profit réalisé.

## Périmètre réel

La sonde du 20 septembre 2026 retrouve des swaps sur le PoolManager Uniswap v4
reviewed depuis le 27 mai 2026. Cela mesure les données disponibles sur cette
source, pas le début garanti de toute la chaîne Arc.

La requête privée `8786953` correspond à `queries/dune_arc_x10_history.sql`.
Les achats sont répartis en périodes mensuelles contiguës `[début, fin[`. Pour
chaque période, les prix observés après les achats sont recherchés jusqu'au
même cutoff UTC fixé au lancement, y compris dans les mois suivants.
Une médiane horaire ultérieure doit atteindre au moins 10 fois le prix d'achat.
La requête n'a ni `LIMIT 500`, ni autre classement qui tronque les wallets.

Couverture déclarée : PoolManager reviewed, paires directes USDC, transaction à
un seul swap, règlement ERC20 USDC identifiable, montants représentables en
DECIMAL(38,0). Les transferts sont additionnés exactement avant conversion en
prix flottants, afin d'éviter des résidus de cancellation artificiels.
Les autres DEX, swaps multi-étapes, paiements natifs sans règlement ERC20 et
pics limités à l'heure de l'achat ne sont pas couverts. Il n'est donc pas exact
de présenter ce résultat comme tous les acheteurs gagnants de toute la chaîne.

## Reprise et conservation

Le manifeste persistant contient chaque période et son execution_id. Toutes les
pages doivent appartenir à cette exécution, avec une pagination cohérente et
un résultat non partiel. Le plafond du collecteur quotidien n'est pas appliqué
au mode historique exhaustif. Une page échouée ne valide pas la période.

Les réponses complètes sont sauvegardées avant import. La reprise d'une période
déjà téléchargée n'exécute pas à nouveau son SQL. Une soumission POST incertaine
reste `SUBMITTING` et exige une revue, afin de ne pas lancer une seconde
exécution payante. Les périodes achevées sont conservées. Une erreur est un
report explicite, jamais une période vide supposée valide.

Les observations sont stockées sous `provider=dune_x10`, séparément de la
requête quotidienne et de Zerion. Toutes les preuves par période sont gardées
dans `external_wallet_metrics`. Une même adresse présente dans plusieurs mois
ne devient qu'un seul candidat local ; un statut existant n'est pas remplacé.
Les infrastructures connues sont exclues. Les sources externes ne créent aucun
trade, signal, winner source de promotion ou autorité smart-money.

Le rapport `.wallets.json` et `.wallets.csv` agrège les périodes achevées et
déduplique les wallets. Les nombres d'achats/ventes portent sur ce sous-ensemble
couvert, pas sur l'historique complet du wallet. Aucun nombre de tokens uniques
inter-mois n'est inventé en additionnant les comptes mensuels.

## Exécution explicite et crédits

Sans `--run`, le script affiche le plan sans appels réseau. `--run` autorise les
exécutions Dune avec la clé locale existante. Le worker quotidien reste un
lecteur de résultats ; il ne lance pas ces requêtes automatiquement.

Le budget par défaut est de 500 crédits observés depuis le début du job ; une
marge de 100 crédits inclus est exigée avant une nouvelle exécution. La
consommation d'une exécution en cours peut dépasser le budget, car son coût
final est connu après calcul. Il ne s'agit pas d'un plafond de facturation
garanti côté fournisseur. Aucun achat de crédits ou changement de plan n'est
effectué. La collecte s'arrête avant de lancer la période suivante si le
budget observé est épuisé.

Reprendre la collecte préparée (même cutoff et même fichier d'état) :

```powershell
Set-Location "C:\Users\Valen\Desktop\bot\arc_smartmoney_bot\audit-issue-1"
.\.venv\Scripts\python.exe dune_history.py --run --query-id 8786953 --start 2026-05-27 --end 2026-09-21 --cutoff "2026-09-20 20:43:20" --max-credits 500
```

Le verrou refuse un second collecteur sur ce manifeste. Les processus d'analyse
et de paper existants peuvent continuer ; leurs checkpoints ne sont pas modifiés.

## Résultat de la première collecte complète

Exécution le 20 septembre, cutoff fixe **20 septembre 2026 à 20:43:20 UTC** :

| Période des transactions du wallet | Wallets retournés |
|---|---:|
| 27–31 mai | 0 |
| Juin | 0 |
| Juillet | 2 |
| Août | 7 |
| Septembre jusqu'au cutoff | 7 211 |

Après déduplication inter-périodes : **7 216 wallets**, dont **6 678 nouveaux
candidats locaux** et 683 avec un seul achat observé dans le périmètre couvert.
Les 72 pages pleines et la dernière page de septembre ont toutes été récupérées
sur la même exécution `01M309VT65F9ZA4H2K2W2PSM4V`. Aucun rejet de ligne n'a été
enregistré sur ces cinq périodes. L'usage du compte Dune est passé de 20,443 à
174,049 crédits, soit environ **153,606 crédits** pendant la collecte, sur
2 500 inclus. La sonde de couverture antérieure est hors de ce delta.

Les 301 snapshots historiques valides présents avant la collecte sont conservés.
Les nouveaux wallets restent des candidats de recherche. La découverte d'un
achat avant x10 n'établit ni profit réalisé, ni répétabilité, ni copie exécutable.

Rapports locaux : `data/dune_x10_history.wallets.json` et
`data/dune_x10_history.wallets.csv`. Les cinq réponses complètes et le manifeste
restent disponibles dans `data/dune_x10_history.*.json`.

Références : [exécuter une requête](https://docs.dune.com/api-reference/executions/endpoint/execute-query),
[pagination des résultats](https://docs.dune.com/api-reference/executions/pagination),
[usage des crédits](https://docs.dune.com/api-reference/usage/endpoint/get-usage).

# Capital allocation et conviction sizing

Cette extension décrit les wallets déjà observés. Elle ne change ni les seuils
de promotion, ni le scoring forward/sécurité, ni les contrôles Sybil, ni la
reconstruction, les DEX de confiance, les exclusions d'infrastructure ou le paper
trading. Elle ne change pas les wallets suivis. Une performance ou un sizing
faible ne supprime aucune ligne et n'empêche aucune observation. Les contrats
smart accounts non listés comme infrastructure restent traités comme avant.

## Données et performances

Les achats exploitables sont les BUY vérifiés de `events`, après
`max(observation_epoch, first_discovered_at)`, avec un montant USDC et une
quantité positifs et finis. Le rapport déduplique transaction/wallet/token.
Les historiques de recherche n'entrent jamais dans ces statistiques.

Les performances utilisent seulement les `trade_outcomes` OBSERVED associés
exactement au trade, à son entrée et à sa fenêtre d'observation. Un résultat
manquant, trop tardif, futur, PENDING ou sans correspondance reste inconnu.
Le rapport ne collecte pas de prix et ne réutilise pas le résultat de la première
entrée pour les achats suivants du même token. Actuellement, le moteur évalue
surtout les premières entrées dans les nouveaux déploiements ; tous les achats
observés ne possèdent donc pas nécessairement un outcome.

Ces résultats sont des variations **brutes à horizon fixe**, pas du PnL réalisé,
ni des rendements nets de frais/slippage. Pour un achat de capital `c_i`, le
rendement `r_i` est `outcome_price / entry_price - 1`, et sa contribution
théorique est `p_i = c_i * r_i`.

| Champ | Définition |
|---|---|
| `capital_total_observed` | Somme des montants de tous les achats forward exploitables, y compris ceux sans résultat. C'est du capital engagé cumulé, pas le solde ou le capital net déposé. |
| `capital_share_per_trade` | Pour chaque achat, `c_i / somme(c_i)` ; calculable immédiatement. Export JSON dans la cellule CSV avec transaction, token, date, taille, résultat et percentile. |
| `capital_weighted_hit_rate` | Capital des trades de rendement strictement positif / capital évalué. Le zéro n'est pas un gain. |
| `capital_weighted_return` | `somme(c_i * r_i) / somme(c_i)` sur les seuls trades évaluables. |
| `outcome_capital_coverage` | Capital évaluable / capital total observé. Un faible taux limite l'interprétation des rendements. Les achats inconnus ne sont pas inventés comme pertes ou gains nuls. |
| `profit_factor` | Somme des contributions positives / valeur absolue des contributions négatives. Sans perte observée, N/A avec `NO_LOSSES_OBSERVED`, jamais un infini assimilé à une preuve. |
| `median_buy_usdc` | Médiane des achats exploitables ; toujours accompagnée de la confiance d'échantillon. |
| `usual_buy_usdc` | Médiane utilisable comme baseline seulement à partir de `SIZING_MEDIUM_BUYS`. |
| `largest_buy_usdc` | Plus gros achat forward exploitable. |
| `conviction_accuracy` | Part du capital gagnant parmi les achats évaluables d'au moins 2 fois la médiane. N/A si baseline insuffisante ou aucun gros pari évaluable. |
| `largest_trade_contribution` | Contribution positive du meilleur trade / somme nette des contributions, si cette somme est positive. Peut dépasser 100 % quand d'autres trades perdent. Sinon N/A. |
| `largest_positive_profit_share` | Contribution du meilleur trade / somme des contributions positives. Mesure bornée utilisée pour le risque de concentration. |
| `concentration_risk` | HIGH si le meilleur trade explique au moins 80 % des gains positifs observés ; LOW sinon, UNKNOWN sans gain positif. Jamais un filtre. |
| `position_size_percentile` | Dans les détails par trade : pourcentage de tailles observées inférieures ou égales, seulement avec une baseline suffisante. |

Les ratios sont exportés en fractions (`0.34 = 34 %`) et affichés en pourcentage.
Le hit rate forward existant garde sa définition de winner du moteur
(`WINNER_RETURN_PCT`) ; le hit rate capital mesure, lui, un rendement positif.
Les champs `capital_forward_winners` et le nombre de winners du résumé cluster
utilisent bien `WINNER_RETURN_PCT`, sans modifier ce seuil.

Exemple : 1 000 USDC à +200 %, puis dix achats de 1 USDC à -100 % donnent
99,01 % de hit rate pondéré, +197,03 % de rendement pondéré et un profit factor
de 200. Le hit rate brut est seulement 1/11. La concentration est HIGH : ce
résultat ne constitue pas une validation statistique ou moteur du wallet.

## Confiance et classement

```dotenv
SIZING_MEDIUM_BUYS=5
SIZING_HIGH_BUYS=10
SIZING_CONCENTRATION_THRESHOLD=0.8
```

Ces paramètres sont exclusivement descriptifs : LOW sous 5 achats, MEDIUM de
5 à 9, HIGH à partir de 10. `sizing_confidence` porte sur la baseline de taille,
pas sur la rentabilité. La médiane évite qu'un achat extrême déforme cette baseline.

`allocation_evidence_confidence` reste LOW si les gains sont concentrés, si les
outcomes ne couvrent pas tout le capital ou si les preuves manquent. Sinon elle
utilise les mêmes paliers sur le nombre de tokens évaluables distincts. Ce label
est une aide de lecture, pas une estimation statistique de rentabilité future.

Un rendement pondéré positif améliore l'ordre d'affichage **à l'intérieur** de
la catégorie actuelle. Les catégories restent fondées sur les critères existants.
Un seul énorme gain peut donc faire remonter un WATCH, avec concentration HIGH,
mais ne peut pas le transformer en VALIDATED. PROMISING ne donne aucune autorité.
Les critères moteur et les raisons de non-promotion restent visibles.

## Identités et ranking diversifié

Le rapport utilise les derniers clusters enregistrés dans `runtime_events` et
les clusters historiques de `data/history_report.json`, sans requête réseau.
Les deux snapshots sont reliés de façon conservative : des liens connus dans
l'un ou l'autre suffisent à regrouper les wallets, même si les labels divergent.
Cela peut regrouper trop largement des relations anciennes ; aucune autorité
opérationnelle n'est calculée depuis ce graphe de reporting.

Un funder brut n'est pas traité automatiquement comme preuve de relation : il
peut s'agir d'un service partagé. Les flags stockés restent affichés. Sans label
de relation disponible, le wallet reçoit `unknown:<adresse>`, clairement distinct
d'une identité indépendante vérifiée. Les événements nouvellement enregistrés
conservent aussi l'indicateur de connaissance d'identité du moteur.

`identity_cluster_size` et `related_wallets_count` décrivent la population du
rapport, pas tous les comptes de la blockchain. `independent_cluster_count`
vaut un par **groupe identifié** (zéro pour un singleton inconnu) : ne jamais
additionner les copies de ce champ sur les lignes wallet. Le résumé déduplique
les groupes ; dix wallets du même groupe donnent un seul groupe identifié.
Ce compteur n'est pas le nombre de confirmations autorisées d'un signal.

Le mode `--diverse` garde par défaut au plus deux wallets par groupe dans le
Top N ; `--per-cluster 1` permet d'en afficher un seul. Tous les wallets restent
dans le CSV brut et dans la base. Les singletons inconnus peuvent être affichés
mais ne sont jamais déclarés indépendants pour autant.

## Résumé cluster

`python wallet_ranking.py --clusters` affiche et exporte les groupes complets
dans `data/cluster_ranking.csv` : wallets, wallets avec achat exploitable,
sources gagnantes distinctes, achats/winners forward, tokens gagnants distincts,
capital observé/évalué, couverture, rendement pondéré, meilleur rendement wallet,
concentration du capital et des gains, flags et confiance.

Les sources distinctes sont dédupliquées par token depuis `candidate_sources`,
jamais additionnées depuis les compteurs wallet. Les sources sans détail sont
signalées via `winner_sources_missing_detail_wallets`. Les rendements sont
agrégés depuis les trades (pondération par capital), jamais par une moyenne
simple des rendements wallet. Les copies d'un token gagnant restent un seul
token gagnant distinct, même si plusieurs wallets du groupe l'ont acheté.

`internal_capital_concentration` est la part du capital observé attribuable au
plus gros wallet du groupe. La confiance cluster reste LOW si l'identité est
inconnue, les données incomplètes, la couverture des outcomes partielle ou les
gains concentrés. Sinon, les paliers de confiance portent sur les tokens évalués
distincts, pas sur le nombre de wallets liés.

## Alertes et compatibilité

Seuls les BUY déjà autorisés par les contrôles existants reçoivent des métadonnées
de sizing. Pour un nouveau trade, `usual_buy_usdc`, le multiplicateur, le
percentile et la confiance utilisent uniquement les achats antérieurs déjà
enregistrés. Le trade courant entre une seule fois dans le dénominateur de
`capital_share`; les achats futurs n'interviennent pas. Avec 2 ou 3 achats,
capital share est disponible mais usual buy/multiplicateur/label restent N/A.

Labels informatifs selon le multiplicateur : TINY < 0,25 ; SMALL < 0,75 ; NORMAL
< 2 ; LARGE < 5 ; EXCEPTIONAL à partir de 5. Ils ne changent aucun score, poids,
seuil ou critère d'éligibilité.

Les nouvelles alertes d'opportunité validée affichent jusqu'à trois wallets
autorisés avec leurs métriques, leur cluster et le nombre d'identités indépendantes
calculé par le moteur existant. Les métadonnées complètes sont conservées dans
les événements/signaux SQLite. Il n'y a aucun nouvel envoi WATCH ni changement
de déduplication des notifications. L'absence d'une donnée de sizing optionnelle
n'interrompt pas l'observation.

Plusieurs wallets WATCH restent observés et visibles ensemble dans un cluster.
Ils contribuent à l'autorité seulement si les règles existantes les autorisent ;
les wallets QUARANTINE non autorisés gardent un poids de confirmation nul.

Aucune migration n'est nécessaire. Le reporting fonctionne pendant le scanner.
Les nouveaux champs des alertes apparaissent après redémarrage du scanner ;
les anciens événements restent lisibles grâce aux valeurs par défaut.

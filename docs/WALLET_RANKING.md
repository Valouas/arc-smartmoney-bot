# Comparer les wallets sans modifier le moteur

```powershell
.\.venv\Scripts\python.exe wallet_ranking.py --top 20
```

Sans `--top`, la console affiche tous les wallets. Le CSV
`data/wallet_ranking.csv` et le résumé portent toujours sur tous les wallets,
même avec `--top 20`. Le CSV UTF-8 avec BOM est utilisable dans Excel.

Le rapport prend un instantané SQLite en lecture seule (`mode=ro`). Il ne lance
aucun appel réseau, migration, profilage, promotion, notification ni calcul
d'outcome. Il peut être lancé pendant `run_v4.py`. Une base absente ou un schéma
incompatible produit une erreur, jamais une base vide nouvellement créée.

## Population et catégories

La population est l'union dédupliquée des `discovery_candidates` et des
`wallet_profiles`. Un profil sans candidat porte le statut `PROFILE_ONLY`.
Les catégories suivantes sont exclusivement descriptives :

- **REJECTED** : infrastructure DEX connue, statut interne REJECTED, ou profil
  forward SCANNER_BOT/IGNORE. Cela ne supprime aucune ligne.
- **VALIDATED** : candidat déjà PROMOTED avec promotion activée, historique
  hydraté/complet, profil forward de la cohorte courante, récent, de qualité
  complète et `confirmation_eligible`. La fraîcheur reprend celle du moteur
  (`max(120, intelligence_reload_seconds * 4)`).
- **PROMISING** : les critères chiffrés forward de promotion sont atteints
  (score, launches bought, capture maximum, trades évaluables et winners), mais
  le wallet ne satisfait pas la définition VALIDATED ci-dessus. Les paramètres
  sont lus dans `SETTINGS`, sans nouveau seuil. Des blocages d'historique,
  de fraîcheur, d'indépendance ou de financement peuvent donc subsister.
- **WATCH** : les autres wallets.

L'ordre est VALIDATED, PROMISING, WATCH, REJECTED, puis le rendement pondéré
positif observé, les nombres de winners forward, trades évaluables, achats
observés et le score forward décroissants. Un rendement pondéré négatif ou
inconnu ne donne aucun bonus ; il ne change aucune classe ni aucun statut.
La complétude, les sources indépendantes et les winner sources départagent
ensuite les égalités, puis l'adresse. Les sources sont un critère d'avancement
de recherche, pas une preuve de rentabilité. Un score rétrospectif ne sert pas au tri.

Même VALIDATED ne constitue pas une permission d'émettre un signal : le scanner
applique toujours ses vérifications courantes de profil, d'identité, de risque
et de cluster. Un seed peut être autorisé par les règles propres aux seeds sans
être une découverte promue ; ce rapport ne le convertit pas en PROMOTED.
Aucune catégorie du rapport n'est consommée par le moteur.

## Lecture des colonnes

- `winner_sources` : compteur de sources gagnantes de discovery. Ce compteur
  ne prouve ni indépendance ni rentabilité.
- `independent_sources` : dernier `independent_winners` du profil forward,
  c'est-à-dire composants indépendants des déployeurs gagnants vérifiés par le
  moteur. Ce n'est pas un simple compte de tokens. Le profiler peut remplacer
  un profil et ne pas conserver ce champ : il apparaît alors N/A, jamais zéro
  ou une indépendance supposée.
- `research_trades` : historique de recherche, exclu des performances forward.
- `forward_buys_observed` : achats vérifiés de `events`, distincts par
  transaction/token, après le début de l'époque et la découverte du wallet.
  Sans époque d'observation, le compteur est inconnu. Il peut inclure des
  achats non admissibles dans l'univers des nouveaux déploiements.
- `forward_launches_bought` : achats uniques de lancements admissibles du
  dernier profil forward, utilisés par les règles de promotion.
- `forward_hit_rate_pct` : winners / trades évaluables du profil forward.
  Sans trade évaluable, N/A ; le prior bayésien de 33,3 % ne devient pas une
  preuve de réussite. Le CSV conserve aussi le hit rate ajusté du moteur.
- `capture_pct` : taux du profil dans son univers d'opportunités observées ;
  sans dénominateur connu et positif, N/A.
- `score` / `current_class` : dernières valeurs stockées, sans recalcul.
  Une cohorte absente/rétrospective est signalée dans les raisons et n'alimente
  aucune statistique de performance forward.
- `data_status` : qualité et fraîcheur du profil et complétude d'hydratation
  du candidat. COMPLETE n'est ni une promotion ni l'affirmation que tous les
  outcomes sont arrivés ; le CSV expose aussi les outcomes pending/missing.
- `promotion_blockers` : diagnostic des critères connus à l'instant du rapport.
  `last_engine_promotion_reason` conserve séparément le dernier verdict stocké,
  qui peut être plus ancien. Le rapport n'exécute pas la vérification réseau
  de financement et ne certifie donc jamais que tous les contrôles sont passés.
- `in_generated_watchlist` : présence effective dans le fichier JSON, pas une
  déduction depuis le score. Fichier absent = N/A. Une entrée seule n'autorise rien.
- Les clusters des derniers événements et les funders/clusters éventuels de
  `data/history_report.json` sont datés et explicitement historiques. Le graphe
  d'identité courant vit en mémoire ; il n'est pas recréé par ce rapport. Les
  flags persistés de relation/funding sont montrés ; absence de flag = UNKNOWN.
- `infrastructure_excluded` : appartenance aux emitters du registre DEX revu.
  Aucune exclusion générale des smart accounts ou des contrats n'est appliquée.

N/A (cellule CSV vide) signifie inconnu, pas zéro. Les dates sont en UTC.
Les seuils et les règles de scoring/promotion restent inchangés ; ces statistiques
ne suffisent pas à établir une rentabilité nette réalisée.

Chemins alternatifs disponibles : `--db`, `--csv`, `--watchlist`, `--history`.
Le scanner peut modifier les fichiers JSON pendant la lecture : ils sont des
instantanés distincts de celui de SQLite, et leur présence ne donne aucune autorité.

## Allocation du capital, conviction et diversité

Voir [les formules, les limites et les alertes de sizing](CAPITAL_ALLOCATION.md).
Le rapport ajoute les performances pondérées, les tailles médianes, la
concentration des gains et les relations d'identité sans exclure les petits
échantillons. Les métriques négatives ne suppriment aucun wallet.

```powershell
# Tous les wallets + CSV wallets et clusters complets
python wallet_ranking.py
# Top 20 individuel
python wallet_ranking.py --top 20
# Top 20 avec au maximum 2 wallets par cluster connu
python wallet_ranking.py --top 20 --diverse
# Variante plus stricte pour l'affichage uniquement
python wallet_ranking.py --top 20 --diverse --per-cluster 1
# Résumé par cluster
python wallet_ranking.py --clusters
```

Les deux CSV `data/wallet_ranking.csv` et `data/cluster_ranking.csv` sont toujours
complets. `--top` limite la console seulement. Un Top 20 diversifié peut contenir
moins de 20 lignes si trop peu de clusters sont connus ; on ne dépasse pas le
plafond pour remplir artificiellement la vue. Le CSV wallet garde aussi
`same_cluster_wallet_count_in_top` correspondant à la vue sélectionnée.

Le résumé affiche le nombre de groupes d'identité, leur taille, les singletons,
les groupes de plusieurs wallets, les identités inconnues et les clusters actifs.
Les groupes `unknown:` restent des cases de reporting, pas des preuves
d'indépendance.

Tests :

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_wallet_ranking.py -q
```

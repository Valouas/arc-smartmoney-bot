# Collecte hybride de la campagne de recherche

La campagne `copy_holding_campaign.py` peut maintenant lire les transactions et
reçus bruts archivés depuis Dune avant de demander ces deux objets au RPC.
Elle continue d'utiliser `HistoricalCollector.transaction` et `classify_trade`.
Ce changement concerne la campagne d'analyse isolée, pas les événements live.

## Sources et garanties

1. Réutiliser les pages Nansen sauvegardées : leurs hashes et dates permettent
   de cibler des requêtes Dune. Aucun nouvel appel Nansen n'est nécessaire.
   Une date est un indice de recherche avec une marge d'un jour, jamais une preuve
   d'absence de transaction. La couverture d'un wallet reste contrôlée par Arcscan.
2. Précharger par lots les données **brutes** `arc.transactions` et `arc.logs`.
   Il ne s'agit pas de fabriquer un reçu à partir de quelques événements Swap ou
   Transfer. Chaque transaction doit avoir son statut, son expéditeur, sa valeur,
   son bloc, son ordre, ses frais et tous ses logs, avec leurs topics et données.
3. Vérifier le chain ID 5042, les entiers exacts, la pagination, le nombre total
   de logs, les doublons et la cohérence des blocs. Une réponse partielle ne
   publie aucun reçu. Une transaction manquante reste à traiter par RPC.
4. Avant utilisation, contrôler le hash, le numéro et l'horodatage du bloc contre
   un en-tête RPC canonique. Les vérifications de code des trusted DEX et les
   métadonnées des tokens restent celles du moteur existant.
5. En cas d'ambiguïté ou de trou d'inventaire, retenter par le chemin RPC habituel.
   Si l'ambiguïté persiste, elle reste bloquante. Un échec RPC ne devient jamais
   un résultat vide ou un checkpoint réussi.

Le collecteur ne remplace que les payloads encore NULL. Les anciennes données
restent intactes. Les phases FETCHING/DECODING/COMPLETE, les curseurs Arcscan et
les contrôles finaux de complétude restent en place. Les données Dune seules
ne donnent pas d'autorité paper/live. Aucun seuil, classement ou filtre de wallet
n'est modifié.

Un tour sur trois privilégie les wallets ayant des reçus Dune prêts. Ce tour ne
complète pas son lot avec des lectures RPC sans rapport ; les autres tours
continuent la collecte normale et les hashes Nansen. Le calcul final des durées
conserve l'ordre bloc/index de transaction et la méthode FIFO existante.

## Limites opérationnelles et coût

Le préchargeur traite uniquement les hashes manquants des wallets PENDING de la
campagne existante. Il ne relance pas une campagne sur les 7 554 wallets.
Les tentatives de requêtes SQL et leurs coûts sont persistés ; un redémarrage
ne réinitialise ni les compteurs ni les jobs.

La première activation est bornée à 20 exécutions SQL, 100 hashes par lot, deux
heures, et un **seuil d'arrêt entre requêtes de 50 crédits Dune**. Elle exige aussi
une réserve sur les crédits inclus. Ce seuil n'est pas un plafond de facturation
garanti par Dune : le coût de la dernière requête/récupération n'est connu qu'après
exécution. Les interrogations longues sont annulées après une attente bornée ou
si leur coût observé dépasse le reliquat. Aucune recharge ni modification de plan.

Le premier lot de 100 transactions contenait 25 189 logs et la variation observée
du compte a été de 28,175 crédits inclus (mesure conservatrice, pouvant inclure
une activité concurrente). Les lots suivants limitent l'extraction aux reçus de
128 logs au maximum : les plus gros restent des exceptions RPC, **leurs wallets
ne sont pas exclus**. Leur nombre réel de logs reste dans la réponse ; un reçu
omis ne peut donc pas devenir un faux reçu vide. Les gros reçus du premier lot,
déjà acquis et validés, sont conservés et réutilisés.

Le plafond Alchemy existant de la campagne est inchangé. Une fois ses réservations
épuisées, les lectures restantes passent par les RPC publics. Les appels aux
en-têtes, métadonnées et identités de code signifient que le débit global reste
à mesurer ; le gain n'est pas celui d'une élimination totale du RPC.

## Conservation et reprise

- `data/dune_hybrid_20260922/history.sqlite3` : reçus bruts normalisés, provenance,
  empreintes, hashes déjà demandés, jobs et dépenses observées.
- `job_*/` : intentions HTTP, SQL, réponses brutes paginées, coûts et manifeste.
- `backups/` : copies SQLite horodatées avec SHA-256 ; copie vérifiée des dossiers
  de jobs finalisés. Aucun nettoyage automatique.
- `campaign_read_sources` dans la campagne : provenance de chaque nouveau décodage
  (`dune_raw_canonical_rpc`, `rpc`, `rpc_after_dune_ambiguity`).
- Les paramètres antérieurs et la base de campagne sont sauvegardés avant
  activation. La base de production n'est pas modifiée par le préchargeur.

Après un arrêt incertain, un job SUBMITTED bloque de nouvelles soumissions.
La commande `resume-results` récupère seulement les pages d'une exécution dont
la fin est déjà prouvée dans l'archive ; elle ne resoumet jamais le SQL payant.
Le consommateur reste fonctionnel si le préchargeur s'arrête : cache puis RPC.

## Commandes PowerShell

Depuis le dépôt, pour reprendre les processus lorsqu'ils sont arrêtés :

```powershell
.\.venv\Scripts\python.exe dune_hybrid_worker.py run --directory data/dune_hybrid_20260922
```

Dans une autre fenêtre :

```powershell
.\.venv\Scripts\python.exe copy_holding_campaign.py run --directory data/copy_holding_campaign_20260922
```

Les verrous empêchent de démarrer deux exemplaires du même collecteur.
Un marqueur STOP conservé correspond à une demande d'arrêt : ne pas l'effacer
automatiquement. Les réglages d'activation sont persistés ; pas besoin de répéter
`enable-hybrid` après un redémarrage normal.

Lire l'état sans appel fournisseur :

```powershell
Get-Content .\data\dune_hybrid_20260922\status.json
.\.venv\Scripts\python.exe copy_holding_campaign.py status --directory data/copy_holding_campaign_20260922
```

Reprendre uniquement un résultat Dune déjà terminé et sauvegardé :

```powershell
.\.venv\Scripts\python.exe dune_hybrid_worker.py resume-results --directory data/dune_hybrid_20260922
```

## Validation du 22 septembre 2026

- Reçu brut de référence : les 12 logs, gasUsed et effectiveGasPrice concordent
  exactement avec le RPC sauvegardé ; le décodeur produit le même payload final.
- Premier lot : 100 reçus complets. Comparaison indépendante avec les caches
  RPC existants pour 35 reçus : aucun écart de logs ou de frais.
- Pagination live : 25 289 lignes récupérées en trois pages sans rejouer le SQL.
- Consommation hybride observée dans le collecteur après reprise.
- Préchargement initial finalisé : 7 requêtes, 700 hashes ciblés, 659 reçus
  complets acceptés. Les 41 autres restent des exceptions RPC ; aucun wallet
  n'est retiré pour cette raison. Les 7 dossiers HTTP ont une seconde copie
  vérifiée dans `backups/`.
- Coût conservateur comptabilisé : 58,451250756 crédits Dune inclus. Le dernier
  lot a franchi le seuil d'arrêt de 50 ; aucune nouvelle requête n'est soumise.
  Aucun crédit Nansen supplémentaire ; aucune augmentation du plafond Alchemy.
  Le consommateur continue à utiliser le cache et les RPC publics. Le préchargeur
  ne s'est donc pas transformé en campagne payante illimitée.
- Tests déterministes : pagination incomplète/cyclique, identité de chaîne,
  canonicalité, erreurs réseau, doublons, frais, reçus volumineux, cache corrompu,
  reprises et budgets persistants, priorité sans validation prématurée.

Les fichiers et processus d'exécution réelle ajoutés séparément dans le dépôt
ne sont pas modifiés par cette intégration.

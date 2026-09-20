# Decouverte depuis les tokens performants

La decouverte de nouveaux wallets utilise maintenant les sources ayant atteint
**au moins x5**, avec priorite aux sources ayant atteint **au moins x10**.
Les seuils sont inclusifs : x12, x50 et x100 sont aussi prioritaires.
x5 correspond a +400 %, x10 a +900 %, pas a +500 % ou +1000 %.

Configuration (decouverte uniquement) :

```dotenv
DISCOVERY_MIN_MULTIPLE=5
DISCOVERY_PRIORITY_MULTIPLE=10
DISCOVERY_SOURCE_TOKENS_PER_CYCLE=3
DISCOVERY_SOURCE_RETRY_SECONDS=300
```

`WINNER_RETURN_PCT` et les seuils de performance forward/promotion ne changent
pas. Un token sous x5 est exclu des nouvelles sources, pas supprime de la base
ni des trades d'un wallet. Les anciens candidats et leurs sources restent
conserves, meme si leurs tokens sources ne satisfont plus le nouveau filtre.

## Mesure et limites

Le multiple est le rapport entre le meilleur prix observe dans les snapshots
du launch et son premier prix enregistre. Les deux prix doivent etre finis,
strictement positifs, avec une liquidite renseignee au moins egale au seuil
`LAUNCH_MIN_LIQUIDITY_USD` existant. Une paire doit etre connue ; le snapshot doit
etre posterieur ou egal a la premiere observation. LaunchIndexer ne conserve
des snapshots que sur la paire initiale. Aucun prix manquant n'est invente.
La jointure utilise l'index des adresses de tokens normalisees par Storage.

Il s'agit de cotations echantillonnees par le bot, pas d'un historique exhaustif
des prix, d'un prix exact de deploiement ou d'une garantie d'execution au sommet.
Le bot ne connait pas tous les tokens d'Arc ayant fait x10 avant son observation.
Le filtre de liquidite ne prouve pas a lui seul l'absence de manipulation.

## Traitement

- AutoDiscovery enregistre les sources eligibles et met a jour leur performance
  maximale, tout en conservant la date de detection et l'etat deja traite.
- Les anciennes sources encore en attente sont revalidees depuis les cotations
  locales avant tout appel de recherche des acheteurs. Un ancien chiffre x10
  sans cotations recevables ne suffit pas.
- Les x10+ passent avant les x5 a moins de x10, avec au maximum trois tokens
  essayes par cycle. Les echecs restent en attente et sont reessayes apres cinq
  minutes. Aucun historique partiel n'est marque traite.
- Les premiers acheteurs sont toujours confirmes par le decodeur trusted DEX ;
  l'infrastructure n'est jamais un candidat trader. Le plafond existant du
  nombre d'acheteurs precoces reste applique : il ne s'agit pas d'enumerer tous
  les detenteurs ni de connaitre instantanement le trader le plus rentable.
- L'ordonnanceur historique donne egalement priorite aux sources x10+ dans une
  ronde, puis aux x5+, tout en conservant sa rotation et un passage equitable
  sur quatre pour les autres candidats.
- L'historique du wallet contient toujours ses pertes, achats sur tokens sous
  x5, ventes et inventaire ouvert. La performance realisee du wallet et sa
  selection paper proviennent des calculs existants, jamais du multiple du token.

Le journal `auto_discovery.log` affiche `minimum_multiple`, `priority_multiple`,
`eligible` et `priority`. Ces chiffres concernent les sources connues du bot.
La nouvelle table `discovery_source_attempts` ne contient que les dates d'essai.
L'empreinte du decodeur et les reconstructions existantes restent intactes.

## Pourquoi le scanner de blocs reste actif

La decouverte utilise deja LaunchIndexer -> cotations -> acheteurs precoces ->
historique par adresse, sans dependre du checkpoint de `bot.py`. Le scanner
alimente encore `events`, la couverture forward, les profils de WalletProfiler,
les clusters et les signaux existants. L'arreter retirerait cette couverture et
changerait le fonctionnement de l'autorite forward. Ce changement ne le fait
pas ; ses fenetres de logs et son budget RPC restent identiques. Le paper live
et les webhooks continuent aussi independamment. Aucune economie precise de
facturation n'est annoncee sans mesure apres activation.

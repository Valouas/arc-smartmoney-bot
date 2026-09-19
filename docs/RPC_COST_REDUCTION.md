# Réduction des appels RPC sans changement de sélection

`SCANNER_LOG_WINDOW_BLOCKS=10` regroupe les deux recherches de transferts du
scanner principal sur dix blocs au maximum, compatible avec la borne déjà utilisée
par le paper scanner. Les blocs complets sont préparés dans l'ordre et leur
continuité vérifiée. Chaque log doit correspondre au bloc, à une transaction du
bloc et aux wallets interrogés. L'ancre finale est relue même si les logs sont vides.
Le hash de chaque bloc est ensuite relu avant son traitement. Une modification de
watchlist provoque une nouvelle préparation des blocs restants. Une erreur ne
transforme jamais une réponse en liste vide et n'avance pas le checkpoint du bloc.

Sur dix blocs, les recherches de logs passent de 20 à 2. Les vérifications ajoutées
coûtent 11 lectures de bloc supplémentaires : le gain net est donc inférieur à
90 % du coût total du scanner. Le paper scanner conserve son regroupement existant.

`ARC_RPC_CACHE_ENABLED=true` active un cache SQLite partagé entre les processus et
conservé après redémarrage. Il contient uniquement des réponses brutes validées :
transactions et reçus par hash, blocs explicitement demandés par hash, sur des
hauteurs déclarées finalisées par un provider dont le chain id a été vérifié.
Les erreurs, réponses nulles, blocs non finalisés, logs et lectures d'état courant
ne sont pas réutilisés. Les lectures de blocs par numéro restent toujours réseau,
notamment celles qui protègent les checkpoints et les reconstructions historiques.

La finalité est rafraîchie toutes les 60 secondes, avec vérification de l'ancienne
ancre. Une incohérence de finalité lève une erreur. Un tag `finalized` indisponible
désactive temporairement le cache ; la requête réelle doit alors réussir normalement.
Une entrée corrompue ou invalide est traitée comme un cache manquant, jamais comme
une preuve valide. Chaque résultat est isolé par chain id, méthode et paramètres.
Les checksums servent à détecter une corruption accidentelle, pas à authentifier
un fichier provenant d'un tiers.

Le fichier par défaut `data/rpc_cache.sqlite3` est inclus dans les sauvegardes
automatiques dès sa création. Un chemin personnalisé doit être sauvegardé
séparément. Aucun historique existant n'est supprimé ou réinitialisé. Les fichiers
définissant l'empreinte du décodage historique ne sont pas modifiés par ce changement.
Le cache se remplit progressivement ; il ne peut pas récupérer des réponses brutes
qui n'avaient pas été sauvegardées auparavant.

Mesures locales : `rpc_cache_stats` contient hits, misses et writes par méthode.
`rpc_usage_totals` dans la base du limiteur compte uniquement les requêtes réseau
admises. Les unités du limiteur sont des unités de débit, pas une facture.
Ces optimisations réduisent les appels par travail effectué, sans fixer de budget
journalier. Un bot qui réinvestit toute sa capacité dans davantage d'historique peut
encore consommer autant par jour : comparer aussi les CU par bloc/transaction traité.

Ni les seuils, ni le scoring, ni les critères de promotion, ni le mode paper ne changent.

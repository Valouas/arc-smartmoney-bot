# Diagnostic du 20 septembre 2026

Le paper est une simulation en lecture seule. Ce changement ne modifie ni la
sélection des wallets, ni la politique de simulation, ni les seuils de capacité.

## Import externe et concurrence SQLite

Un test à deux connexions reproduit `database is locked` : l'import gardait un
curseur SELECT ouvert sur un ancien snapshot WAL pendant sa première insertion.
Une écriture concurrente rendait impossible le passage à l'écriture. La lecture
est désormais entièrement consommée avant la transaction d'insertion.
Les candidats existants, leurs statuts et les checkpoints ne sont pas remplacés.

Le test échoue sur l'ancienne implémentation et passe sur le correctif. L'ancien
log live ne contenait que `OperationalError` : il ne permet pas d'attribuer avec
certitude cet incident particulier à la concurrence reproduite. Les prochains
logs distinguent `sqlite_busy` et `sqlite_operational` et donnent un emplacement
dans le code, sans afficher le contenu de l'exception ni les paramètres SQL.

## Cinq achats refusés pour capacité

Les cinq achats avaient été détectés 6 à 19 secondes après leur bloc. Les
cotations des refus n'étaient pas enregistrées : leur prix/liquidité exacts à
l'instant du refus ne peuvent pas être reconstruits à partir de la base.

La règle existante compare le montant hors frais et gas à la plus petite de
0,1 % de la liquidité et 1 % du volume sur cinq minutes.

| Budget simulé | Liquidité nécessaire | Volume 5 min nécessaire |
|---:|---:|---:|
| 33,04 $ | 32 931,21 $ | 3 293,12 $ |
| 50,00 $ | 49 840,48 $ | 4 984,05 $ |
| 66,96 $ | 66 749,75 $ | 6 674,98 $ |
| 82,35 $ | 82 093,72 $ | 8 209,37 $ |
| 25,00 $ | 24 915,25 $ | 2 491,53 $ |

Au contrôle du 20 septembre vers 22 h 22 (Suisse), quatre tokens n'avaient plus
de cotation via le client DexScreener. Le premier affichait 6 247,17 $ de liquidité
et zéro volume sur cinq minutes. Ce constat actuel n'est pas une preuve des
valeurs historiques. Aucun achat ancien n'a été rejoué ou ajouté au paper.

La nouvelle table additive `execution_checks` conserve chaque contrôle futur :
identifiant d'observation, date, résultat, cotation disponible, capacité calculée,
montant requis et fractions appliquées. Une cotation indisponible est distincte
d'une capacité insuffisante. `CAPACITY_OK` atteste seulement ce contrôle, pas un
fill ; seuls les enregistrements de `fills` attestent une opération simulée.
Les preuves de tentatives antérieures restent disponibles après expiration.

## Parcours live vérifié en lecture seule

Transaction `0x42da2346cb7ff100e343fe2d96ebe95c4c4019cd88186a7117c375108570e61d`,
wallet `0x1f6b39ad0251dc1b2f00079c1f3b0fd897d04d4c`, bloc `21885792` :

- Le RPC confirme Arc 5042, receipt réussi et vente reconnue par le decoder avec
  `confirmation_eligible=true` (environ 25,81 USDC).
- Deux livraisons Alchemy ont été reçues une seconde après le timestamp du bloc.
- Un seul trigger paper et une seule observation de vente ont été conservés.
- L'observation a été détectée huit secondes après le bloc, trigger `SCANNED`.
- Le refus `NO_COPIED_POSITION` est cohérent : aucune position simulée à vendre.

Cette vérification prouve réception, déduplication, classification et décision
sur cette vente. Elle ne prouve pas encore une copie d'achat exécutée en live.
Le parcours achat confirmé → cotation → fill → reprise sans doublon est couvert
par les tests déterministes existants, rejoués avec les nouveaux contrôles.

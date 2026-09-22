# Vérification ciblée des durées de détention

`copy_holding_campaign.py` reprend les historiques déjà enregistrés pour les
1 512 wallets respectant les critères de performance/fréquence mais dont la
durée médiane n'est pas établie. Le périmètre est figé, ordonné par score,
copié dans le répertoire de campagne et protégé par une empreinte SHA-256.

Cette collecte de recherche n'utilise ni Nansen ni Alchemy. Elle n'écrit pas
dans la base de production et ne donne aucune autorité paper/live. Les
seuils, le decoder, les trusted DEX et les promotions restent inchangés.

## Commandes Windows

Depuis le répertoire du projet avec son environnement Python :

```powershell
.\.venv\Scripts\python.exe copy_holding_campaign.py status --directory data/copy_holding_campaign_20260922
.\.venv\Scripts\python.exe copy_holding_campaign.py run --directory data/copy_holding_campaign_20260922
```

Le verrou système refuse deux workers sur cette même campagne. `run` reprend
les données sauvegardées ; il ne recrée ni le périmètre ni le budget.

Pour demander un arrêt propre (sans supprimer de données) :

```powershell
New-Item -ItemType File -Path data/copy_holding_campaign_20260922/STOP
```

Après cet arrêt, conserver le marqueur sous un autre nom avant une reprise :

```powershell
Rename-Item -LiteralPath data/copy_holding_campaign_20260922/STOP -NewName ("STOP_" + (Get-Date -Format yyyyMMdd_HHmmss))
```

## Bornes et reprise

- Endpoints explicitement autorisés : `rpc.mainnet.arc.io`, `rpc.arc-scan.org`
  et l'activité d'adresse d'Arcscan. Aucun provider configuré payant ni autre
  wallet ne peut être substitué par une variable d'environnement.
- Chain ID 5042 vérifié par le transport avant les lectures.
- Au plus une requête HTTP par seconde dans la campagne, avec le limiteur
  RPC public partagé. Les retries comptent dans le budget.
- Plafond cumulé de 200 000 tentatives RPC et 10 000 tentatives Arcscan.
  Réservation SQLite avant chaque appel, conservée après redémarrage.
- Durée maximale de 72 heures à compter de la préparation, également
  conservée après redémarrage. Ce plafond n'est pas une estimation de fin.
- Un tour traite une page ou dix transactions. Les wallets sont parcourus
  équitablement, par score dans chaque tour ; les erreurs sont différées.
- Après trois tours en échec, le wallet reste `BLOCKED` avec sa preuve et sa
  dernière catégorie d'erreur. Un second endpoint public est essayé après
  un échec réel, sans remettre les curseurs à zéro.

Une page invalide ne fait pas avancer son curseur. Chaque transaction décodée
avec succès est persistée ; les suivantes restent à traiter en cas d'échec.
Les hashes du bloc limite sont revérifiés hors cache avant publication du
résultat complet. Les transactions déjà décodées sont réutilisées lorsque
le contexte du decoder et du registre DEX correspond.

## Résultats et conservation

- `VERIFIED` : médiane jusqu'à la première vente partielle ≥120 secondes.
- `TOO_SHORT` : médiane observable <120 secondes.
- `REVIEW_REQUIRED` : preuve invalide, schéma ambigu, inventaire incomplet,
  aucune première vente observable, ou changement du contexte de décodage.
- `BLOCKED` : collecte empêchée après les tentatives bornées.
- `PENDING` : collecte à poursuivre.

Un résultat inconnu n'est jamais transformé en validation ou en wallet vide.
Les cinq générations historiques invalidées sont conservées pour audit ;
la campagne ne les réinitialise pas pour prétendre les rendre complètes.

`status.json` est une vue reconstruisible. `campaign.sqlite3` contient les
transactions et l'état durable ; `rpc_cache.sqlite3` contient les réponses RPC
réutilisables. `requests.jsonl` conserve les tentatives et réponses publiques,
sans secrets. Des sauvegardes SQLite sont faites avant la première requête,
toutes les 30 minutes de progression et à l'arrêt propre. Les anciennes
sauvegardes sont conservées, avec leurs empreintes SHA-256.

Ces résultats doivent ensuite être joints au classement local pour lever
les réserves de durée ; ils ne changent pas seuls la sélection du moteur.

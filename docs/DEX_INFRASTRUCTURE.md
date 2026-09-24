# Exclusion ciblée des infrastructures DEX

Un emitter DEX peut prouver un swap sans être son trader. Le PoolManager
`0x8366a39cc670b4001a1121b8f6a443a643e40951` observé dans le live était devenu
un candidat parce que la découverte examinait les destinataires des transferts,
y compris les contrats d'infrastructure recevant les tokens d'un swap.

La liste d'exclusion vient exclusivement des adresses `emitters[].address` du
fichier désigné par `TRUSTED_DEX_REGISTRY` (défaut `trusted_dex.json`), pour le
chain id configuré. La comparaison ignore la casse. Aucun test EOA/contract,
label d'explorer ou heuristique de bytecode ne sert à exclure des wallets : un
smart account non listé reste soumis exactement aux règles normales.

La vérification de preuve de swap reste distincte : `TrustedDexRegistry.trusts`
exige toujours le hash du runtime code. Même si ce hash manque ou ne correspond
plus, une adresse explicitement listée comme infrastructure reste exclue des
wallets. Cela ne lui accorde jamais une nouvelle confiance comme emitter.

## Garde-fous

- Exclusion avant la recherche de receipts des acheteurs et avant l'hydratation.
- Refus d'insertion comme candidat, de profil persistant et de promotion dans les
  méthodes de stockage, même si une ancienne liste d'acheteurs contient l'adresse.
- Exclusion avant le calcul du profil candidat et le scoring de promotion.
  Les anciennes promotions d'infrastructure ne consomment pas les quotas des vrais wallets.
- Filtrage des exports générés, des seeds chargés en mémoire et de la watchlist
  des candidats en quarantaine. Le fichier seed utilisateur n'est pas modifié.
- Refus de l'autorité provenant d'un ancien profil JSON et exclusion des profils
  recalculés depuis l'historique. Les anciens événements runtime d'infrastructure
  ne sont pas restaurés comme confirmations ; les anciennes entrées en mémoire
  sont retirées des clusters.
- `classify_trade` ignore une infrastructure passée comme **wallet trader**,
  tout en conservant ses logs/emissions comme preuve pour un véritable wallet.

Le registre est relu après modification (cache invalidé par date/taille du
fichier). Un registre existant mais malformé ou d'une autre chaîne bloque les
opérations d'autorité des wallets. L'absence du registre facultatif équivaut à
une liste vide ; la commande de nettoyage exige un registre existant non vide.
N'enlevez pas le registre pour contourner une erreur de configuration.

Les formules de scoring, seuils, critères de promotion des vrais wallets,
read-only et paper mode ne changent pas. Le transport Arcscan reste inchangé.

## Nettoyage des données déjà présentes

Le script `cleanup_dex_infrastructure.py` est une migration de données ciblée,
idempotente, sans changement de version de schéma. Il ne reconstruit pas la base
et n'exécute pas les migrations générales de `Storage`.

Pour les seules adresses du registre, il retire les lignes de :

- `discovery_candidates` et `candidate_sources` ;
- `promoted_wallets`, y compris les anciennes entrées désactivées ;
- `wallet_profiles` ;
- `generated_watchlist.json` et `data/wallet_intelligence.json`.

Les autres wallets et les métadonnées des fichiers sont conservés. **Aucun
événement, research trade, receipt/log de preuve, contexte de trade, signal,
position paper, proposition, marqueur d'idempotence ou checkpoint n'est supprimé.**
Une adresse d'infrastructure apparaissant comme token, pool ou contrepartie
d'un vrai wallet n'entraîne aucune suppression de son historique. D'anciens
événements peuvent donc encore apparaître dans un rapport historique ; ils ne
rendent plus cette infrastructure éligible comme wallet smart-money.

Sans `--apply`, le script ne fait qu'un aperçu en lecture seule. Avec `--apply`,
il sauvegarde la base par l'API SQLite et les fichiers existants sous
`data/dex_cleanup_backups/<identifiant>/`, puis effectue les suppressions SQL dans
une transaction. Chaque fichier est remplacé atomiquement. Si une écriture de
fichier échoue après le commit SQL, la commande sort en erreur ; la relancer
termine le nettoyage. Les filtres runtime bloquent l'autorité des exports anciens
pendant cette fenêtre. Aucun fichier JSON malformé n'est remplacé par une liste vide.

Arrêtez **les quatre processus** avec Ctrl+C dans le terminal de `run_v4.py` avant
d'appliquer. La commande refuse un scanner détenant encore son verrou ; elle ne
remplace pas l'arrêt explicite des processus profiler et auto-discovery.

```powershell
Set-Location "C:\Users\Valen\Desktop\bot\arc_smartmoney_bot\audit-issue-1"
git switch fix/issue-1-v4-audit
git pull --ff-only origin fix/issue-1-v4-audit

# Aperçu facultatif : aucune suppression.
.\.venv\Scripts\python.exe cleanup_dex_infrastructure.py

# Nettoyage ciblé et sauvegarde automatique.
.\.venv\Scripts\python.exe cleanup_dex_infrastructure.py --apply
if ($LASTEXITCODE -ne 0) { throw "Nettoyage non termine ; ne pas relancer les processus." }

.\.venv\Scripts\python.exe run_v4.py
```

Dans un autre terminal :

```powershell
Set-Location "C:\Users\Valen\Desktop\bot\arc_smartmoney_bot\audit-issue-1"
.\.venv\Scripts\python.exe status.py
```

L'aperçu du live lors de cette correction a identifié pour le PoolManager :
1 candidat, 2 liens de source, 1 profil SQLite et 1 profil JSON ; aucune promotion
ni entrée dans la watchlist générée. **Seul l'aperçu a été exécuté sur cette base**,
pour laisser l'application du nettoyage se faire après votre arrêt des processus.
Le nettoyage effectif et sa reprise après interruption sont testés sur des bases
temporaires contenant aussi des wallets et historiques à conserver.

Validation de cette correction : 271 tests déterministes passent, dont 28
régressions dédiées aux infrastructures et au nettoyage ; les 13 tests publics
Arc/Arcscan passent, y compris les trois scénarios `launch_indexer.py --once`.
Les 59 fichiers Python ont été compilés. La disponibilité future des endpoints
reste externe ; aucun garde-fou Arcscan n'a été modifié pour obtenir ces résultats.

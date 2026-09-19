# Sauvegardes automatiques SQLite

Le lanceur `LANCER_BOT.bat` / `run_all.py` sauvegarde les bases d'analyse et de
paper copy avant de lancer les processus, puis supervise `automatic_backup.py`.
Le cache RPC brut `data/rpc_cache.sqlite3` est aussi sauvegardé lorsqu'il existe.
Par défaut, une sauvegarde est créée chaque heure dans `data/automatic_backups`.
Les 24 dernières versions et la plus récente de chacun des sept derniers jours
disponibles sont conservées. Le nettoyage ne touche ni les archives de récupération
ni les anciennes sauvegardes présentes ailleurs dans `data`.

Chaque base est copiée avec l'API SQLite backup, incluant les écritures WAL
validées. La copie passe `quick_check`, puis reçoit une empreinte SHA-256 avant
publication du dossier. Une copie partielle n'est jamais publiée. Les deux bases
ont chacune un état cohérent ; elles ne constituent pas une transaction distribuée.
Les fichiers `.env` et leurs secrets ne sont pas inclus.

Une erreur de sauvegarde initiale empêche le démarrage. Pendant l'exécution,
le service réessaie toutes les 30 secondes. Si la dernière sauvegarde devient
plus vieille que trois heures pendant ces échecs, le service quitte et le lanceur
arrête les processus gérés. `status.py` affiche le dernier état connu. Le délai
de copie et de contrôle SQLite est de 300 secondes par base. Une réserve de
2 Gio d'espace libre est exigée. Une vingtaine de copies de grosses bases peut
occuper plusieurs dizaines de Gio : adapter la rétention à l'espace disponible.

Commandes PowerShell, depuis le dossier du bot :

```powershell
.\.venv\Scripts\python.exe automatic_backup.py --once
$copie = Get-ChildItem .\data\automatic_backups -Directory -Filter 'backup_*' |
    Sort-Object Name -Descending | Select-Object -First 1
.\.venv\Scripts\python.exe automatic_backup.py --verify $copie.FullName
.\.venv\Scripts\python.exe status.py
```

Pour tester une restauration, copier les fichiers de cette génération dans un
**nouveau dossier de test** et ouvrir ces copies avec SQLite. Ne pas écraser une
base active ou mélanger sa base avec des fichiers WAL/SHM d'une autre génération.
Une restauration opérationnelle exige l'arrêt de tous les processus et la
conservation préalable de l'ensemble actuel, base et WAL compris.

Ces copies sont indépendantes de la base active mais restent, par défaut, sur le
même disque. Elles ne protègent pas d'une panne de ce disque. Le chemin configurable
`AUTOMATIC_BACKUP_DIRECTORY` peut désigner un autre disque. Les lancements directs
de scripts individuels ne bénéficient pas du contrôle de démarrage de `run_all.py`.

Le limiteur RPC pondéré conserve aussi `rpc_usage_totals` dans sa base séparée :
requêtes admises et unités **de débit estimées**, par méthode, persistantes entre
redémarrages. Les retries sont comptés. Ce compteur ne représente pas la facture
Alchemy, les appels REST, ni les appels du fournisseur de secours.

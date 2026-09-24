# Conservation des donnees

Instruction du proprietaire : conserver les donnees collectees et ne jamais
reinitialiser une base ou supprimer un cache pour resoudre un probleme du bot.
La conservation ne donne aucune autorite supplementaire aux wallets.

## Protection active

- `AUTOMATIC_BACKUP_KEEP_ALL=true` : aucune suppression automatique de generation
  complete. Les anciens reglages KEEP_LAST/KEEP_DAILY ne s'appliquent plus.
- Sauvegarde SQLite coherente au demarrage et toutes les heures, WAL valide inclus,
  verification `quick_check`, empreinte SHA-256 et publication seulement apres succes.
- Bases protegees : analyse, paper, cache RPC, campagne/cache Nansen, budget Zerion,
  limiteur RPC, validation paper et catalogue des archives de fichiers, si presentes.
- Declencheurs SQLite additifs : les anciennes versions des donnees historiques,
  profils, trades, reponses externes et caches sont conservees avant UPDATE, DELETE
  ou INSERT OR REPLACE dans `retained_evidence_versions`. Les tables operationnelles
  continuent a fonctionner normalement. Le decodeur et ses criteres de reuse ne
  changent pas; aucune reconstruction n'est reinitialisee par cette protection.
- Les exports JSON/CSV publies via `atomic_json` conservent l'ancienne ET la nouvelle
  version avant remplacement, sous forme de blobs gzip adresses par SHA-256. Une
  version identique ne prend pas une nouvelle copie. Un echec d'archivage empeche
  le remplacement du fichier original.
- Avec `PRESERVE_RAW_API_RESPONSES=true`, les reponses JSON de succes Arcscan,
  Zerion et Dune sont archivees avec leur perimetre de requete, sans en-tetes
  d'authentification ni corps d'erreur. Nansen conserve deja ses pages en SQLite;
  le cache RPC conserve les lectures qu'il prend en charge.
- Les fichiers de donnees preexistants sont archives lors de l'activation puis
  controles lors des sauvegardes. Les dossiers de sauvegarde/recuperation existants
  restent intacts; ils ne sont pas recopies recursivement dans eux-memes.

## Emplacements

- `data/automatic_backups/backup_.../` : generations SQLite, sans purge automatique.
- `data/evidence_archive/catalog.sqlite3` et `blobs/` : versions des exports et
  reponses REST; le catalogue relie chemin logique, empreinte et taille.
- `data/automatic_backups/evidence_blobs/` : seconde copie des blobs immuables,
  partagee entre les generations pour eviter de les recopier a chaque heure.
- Dans chaque base protegee : `retained_evidence_versions`, en lecture de recherche
  uniquement. Les versions archivees ne remplacent jamais les snapshots autorises.

Le catalogue d'une generation reference uniquement les blobs presents au moment
de sa sauvegarde. Les bases sont coherentes individuellement, pas une transaction
globale simultanee entre tous les processus. Ne jamais copier directement un
fichier SQLite ouvert en ignorant son WAL.

```powershell
# Verifier les archives de fichiers sans modifier les donnees actives
.\.venv\Scripts\python.exe data_preservation.py --verify

# Creer une sauvegarde supplementaire complete
.\.venv\Scripts\python.exe automatic_backup.py --once

# Verifier la derniere generation, sans restaurer sur les bases actives
$etatSauvegarde = Get-Content .\data\backup_status.json -Raw | ConvertFrom-Json
.\.venv\Scripts\python.exe automatic_backup.py --verify (Join-Path '.\data\automatic_backups' $etatSauvegarde.generation)
```

## Limites et restauration

Ces protections conservent ce qui existe encore et les nouvelles donnees; elles
ne recreent pas les donnees deja perdues avant leur activation. Les fichiers de
statut temporaires et compteurs de debit ne sont pas versionnes a chaque seconde.
Les archives ne sont jamais supprimees pour liberer de l'espace. Une sauvegarde
qui manque d'espace echoue en preservant les precedentes; le superviseur arrete
la collecte si la protection reste indisponible au-dela de l'age maximal configure.

Les copies actuelles sont sur le meme disque : elles protegent notamment contre
les erreurs logicielles, pas contre la perte de ce disque. Une copie sur disque
externe ou stockage distant est necessaire pour ce risque. Pour la rendre portable,
copier une generation complete ET `evidence_blobs`; ne pas copier seulement son
catalogue. Restaurer et verifier d'abord dans un dossier distinct, jamais par-dessus
les bases en cours. Toute restauration/reprise doit aussi reconcilier les budgets
de credits avec leur consommation reelle, et ne change pas la pause du paper.

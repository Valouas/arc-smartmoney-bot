# Telegram : uniquement les opportunités et les nouveaux wallets

Telegram reçoit désormais deux types de messages :

1. **Opportunité d'achat validée** : un cluster BUY satisfait les critères
   existants, atteint `VALIDATED` et crée une nouvelle proposition à examiner.
   Un seul message combine le token, son adresse, le score, les confirmations,
   le prix de référence et la liquidité. Les nouveaux événements sur la même
   proposition en attente ne renvoient pas de message, même après redémarrage.
2. **Nouveau wallet qualifié à suivre** : le candidat vient de franchir tous les
   critères de promotion existants (évidence prospective, historique complet,
   score, gagnants, identité, etc.). Le message présente son adresse et ses
   résultats observés. Une adresse n'est annoncée qu'une fois, même si elle est
   déclassée puis requalifiée ultérieurement. Un profil WATCH ou un candidat
   simplement découvert n'est pas une nouvelle promotion.

Les messages `WATCH`, `BLOCKED`, `EXIT_EARLY`, `EXIT_CLUSTER`, les mises à jour
de scan et les notifications paper séparées ne partent plus sur Telegram.
Les événements, signaux, propositions et opérations paper continuent d'être
enregistrés. Les paper stops et sorties continuent de fonctionner, sans
notification Telegram. Le signal d'achat et la proposition ne génèrent plus
deux messages supplémentaires autour d'un paper buy.

Les seuils, formules, gates de promotion et la simulation paper restent
inchangés. « Qualifié » décrit les critères et observations du scanner, pas une
garantie de rentabilité future. Aucun ordre ni copie automatique n'est ajouté.

## File d'envoi et mise à jour

Une colonne `kind` est ajoutée à `alert_outbox` au démarrage de `Storage`, sans
réinitialiser la base. Seuls `BUY_OPPORTUNITY` et `WALLET_PROMOTED` sont autorisés
à l'envoi. Le texte du message ne décide jamais de son éligibilité.

Les anciens messages en attente, sans catégorie explicite, passent en
`SUPPRESSED`. Leur contenu reste en base, avec `last_error=notification_policy`.
Cela inclut les anciennes notifications validées : elles ne sont pas rejouées
automatiquement comme de nouvelles opportunités. Les propositions existantes
restent consultables localement. `SENT`, `UNKNOWN` et `FAILED` sont conservés ;
un envoi interrompu `SENDING` conserve la reprise prudente en `UNKNOWN`.

La promotion/proposition et l'intention de notification sont enregistrées dans
la même transaction SQLite. Les clés persistantes empêchent les doublons.
Le transport Telegram conserve ses règles actuelles sur les erreurs et les
envois ambigus. `status.py` permet de voir les messages `SUPPRESSED`.

Le scanner en cours doit être redémarré pour charger le correctif. Arrêter
`run_v4.py` avec Ctrl+C arrête ses quatre processus, puis :

```powershell
Set-Location "C:\Users\Valen\Desktop\bot\arc_smartmoney_bot\audit-issue-1"
git switch fix/issue-1-v4-audit
git pull --ff-only origin fix/issue-1-v4-audit
.\.venv\Scripts\python.exe run_v4.py
```

Le traitement reprend depuis son état persistant. Aucune modification de `.env`
ni commande de purge n'est nécessaire. Si aucun achat ne satisfait les critères
et qu'aucun wallet n'est promu, le silence de Telegram est normal.

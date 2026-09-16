"""Persist intent before HTTP; ambiguous delivery never retries automatically."""
import logging
import time

log = logging.getLogger(__name__)


class AlertOutbox:
    def __init__(self, storage, transport):
        self.storage = storage
        self.transport = transport

    def recover(self):
        # Called only by the process holding the scanner ownership lock.
        self.storage.db.execute("UPDATE alert_outbox SET state='UNKNOWN',last_error='interrupted_delivery' WHERE state='SENDING'")
        self.storage.commit()

    def flush(self, limit=10):
        if not self.transport.enabled: return 0
        db = self.storage.db
        sent = 0
        for _ in range(limit):
            with self.storage.transaction():
                row = db.execute("SELECT * FROM alert_outbox WHERE state='PENDING' AND next_attempt<=? ORDER BY created_at,id LIMIT 1", (int(time.time()),)).fetchone()
                if row is None: break
                db.execute("UPDATE alert_outbox SET state='SENDING',attempts=attempts+1 WHERE id=?", (row['id'],))
            try: result = self.transport.send(row['text'])
            except Exception: result = {'state':'UNKNOWN','error':'transport_unknown'}
            state = result['state']
            if state not in {'SENT','PENDING','FAILED','UNKNOWN'}: state = 'UNKNOWN'
            try: retry_after=max(0,int(result.get('retry_after',0)))
            except (ValueError,TypeError): retry_after=60
            delay = max(retry_after, min(3600,2**min(12,row['attempts']+1)))
            with self.storage.transaction():
                db.execute("UPDATE alert_outbox SET state=?,next_attempt=?,last_error=?,message_id=? WHERE id=?",
                           (state,int(time.time())+delay,result.get('error'),result.get('message_id'),row['id']))
            if state == 'SENT': sent += 1
            else: log.warning('Alert %s delivery state=%s',row['id'],state)
        return sent

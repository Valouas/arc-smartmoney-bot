from __future__ import annotations
import requests


class TelegramAlerts:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text: str):
        if not self.enabled:
            return {'state':'PENDING','error':'disabled'}
        # Never surface exception strings: request URLs contain the bot token.
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                json={'chat_id':self.chat_id,'text':text,'disable_web_page_preview':True}, timeout=7)
            data = response.json()
        except (requests.RequestException, ValueError):
            return {'state':'UNKNOWN','error':'transport_or_response_unknown'}
        if not isinstance(data,dict): return {'state':'UNKNOWN','error':'response_shape_unknown'}
        if response.status_code == 200 and data.get('ok') is True:
            return {'state':'SENT','message_id':str(data.get('result',{}).get('message_id',''))}
        if response.status_code == 429 and data.get('ok') is False:
            return {'state':'PENDING','error':'rate_limited',
                    'retry_after':(data.get('parameters') or {}).get('retry_after',60)}
        if 400 <= response.status_code < 500:
            return {'state':'FAILED','error':f'http_{response.status_code}'}
        return {'state':'UNKNOWN','error':'acceptance_unknown'}

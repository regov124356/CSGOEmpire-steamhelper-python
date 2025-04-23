from datetime import datetime

import requests


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id

        self.url = None
        self._set_url()

    def _set_url(self):
        self.url = f"https://api.telegram.org/bot{self._token}/sendMessage"

    async def send_message(self, message):
        payload = {
            'chat_id': self._chat_id,
            'text': message
        }
        try:
            response = requests.post(self.url, data=payload)

            if response.status_code == 200:
                print(f"[{datetime.now()}] - Message sent")
        except requests.exceptions.RequestException as err:
            print(f"[{datetime.now()}] - Error in send_message: {err}")

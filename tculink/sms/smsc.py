import re
from typing import Any

import requests

from tculink import VERSION
from tculink.sms import BaseSMSProvider, SMSType
from django.utils.translation import gettext_lazy as _


class ProviderSMSC(BaseSMSProvider):
    CONFIGURATION_FIELDS = [
        ('login', _("smsc.ru login")),
        ('password', _("smsc.ru password")),
        ('msn', _("TCU Phone Number (international format)")),
    ]
    HELP_TEXT = _("Укажите логин и пароль от вашего аккаунта на сайте smsc.ru (SMS-Центр). Оплатить можно обычной российской картой. Сервис умеет отправлять технические (бинарные) SMS — именно такие нужны блоку телеметрии автомобиля для связи.")
    SUPPORTED_TYPES = [SMSType.TEXT, SMSType.BINARY]

    def send(self, message, configuration):
        if configuration.get('login') and configuration.get('password'):
            payload: dict[str, Any] = {
                'login': configuration['login'],
                'psw': configuration['password'],
            }
        elif configuration.get('apikey'):
            payload = {'apikey': configuration['apikey']}
        else:
            raise Exception("Configuration is incomplete: set login+password or apikey")

        msn = re.sub(r'\D', '', configuration['msn'])
        if len(msn) < 1:
            raise Exception("Phone number is not valid")

        payload['phones'] = msn
        payload['fmt'] = 3

        if isinstance(message, bytes):
            # binary 8-bit SMS (DCS 0x04), raw hex body, single message.
            # smsc requires the leading "00" (UDH marker) for bin=2,
            # and for bin=2 it rejects dcs/pid being set separately
            # ref. support chat (2026-09-09) + https://smsc.ru/api/#menu (param bin)
            payload['bin'] = 2
            payload['mes'] = '00' + message.hex()
        else:
            payload['mes'] = message

        request = requests.post('https://smsc.ru/sys/send.php',
                timeout=15, data=payload, headers={
                "User-Agent": f"OpenCarWings/{VERSION}",
                "Accept": "application/json",
            }
        )

        if request.status_code != 200:
            return False

        try:
            resp = request.json()
        except ValueError:
            return False

        if isinstance(resp, dict) and ('id' in resp or 'cnt' in resp) and 'error_code' not in resp:
            return True
        error_code = -1
        if isinstance(resp, dict):
            error_code = resp.get('error_code', -1)
        raise Exception(f"smsc error_code={error_code} ({resp})")
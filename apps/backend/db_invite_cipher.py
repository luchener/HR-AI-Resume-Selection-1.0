"""Explicit external invitation key; encrypted records bound to kind and identity."""
import json
from cryptography.fernet import Fernet, InvalidToken


class InviteCipher:
    def __init__(self, key):
        try:
            self._fernet = Fernet(key)
        except (TypeError, ValueError):
            raise ValueError('Explicit valid invitation encryption key required') from None

    def encrypt(self, kind, identity, value):
        raw = json.dumps(dict(context='invite-workflow-v1', kind=kind, identity=str(identity), payload=value),
                         ensure_ascii=False, allow_nan=False).encode('utf8')
        return {'v': 1, 'token': self._fernet.encrypt(raw).decode('ascii')}

    def decrypt(self, kind, identity, envelope):
        try:
            if type(envelope) is not dict or set(envelope) != {'v', 'token'} or type(envelope['v']) is not int or envelope['v'] != 1:
                raise ValueError
            value = json.loads(self._fernet.decrypt(envelope['token'].encode('ascii')))
            if (set(value) != {'context', 'kind', 'identity', 'payload'} or value['context'] != 'invite-workflow-v1' or
                    value['kind'] != kind or value['identity'] != str(identity) or type(value['payload']) is not dict):
                raise ValueError
            return value['payload']
        except (InvalidToken, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise ValueError('Invitation record authentication failed') from None

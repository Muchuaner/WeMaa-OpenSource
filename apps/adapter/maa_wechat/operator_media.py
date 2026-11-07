"""An explicit admin request may allow one exact media delivery, not auto replies."""
import math
import re
import sqlite3
import time
import uuid


def permitted(db, item, now=None):
    from private_baseline import scope
    consent = item.get('operator_consent')
    if not isinstance(consent, dict) or consent.get('kind') != 'single_media':
        return False
    now = time.time() if now is None else now
    try:
        if str(uuid.UUID(consent['request_id'])) != consent['request_id']:
            return False
        expiry = consent['expires_at']
        if (type(expiry) not in (int, float) or not math.isfinite(expiry)
                or not now < expiry <= float(item['created_at']) + 600):
            return False
        if (consent['delivery_id'] != item['id'] or consent['conversation_id'] != item['conversation_id']
                or type(consent['asset_id']) is not int or consent['asset_id'] <= 0
                or consent['asset_id'] != item.get('asset_id')
                or not re.fullmatch('[a-f0-9]{64}', consent['sha256'])
                or consent['sha256'] != item.get('sha256')
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', consent['wechat_id'])):
            return False
        scope(db, 'wx:' + consent['wechat_id'], item['conversation_id'], consent['wechat_id'])
        return True
    except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return False

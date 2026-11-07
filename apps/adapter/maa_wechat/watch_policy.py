"""Foreground work always wins; quiet inspection uses China time, not host TZ."""
from datetime import datetime, timedelta, timezone
import time

CHINA_TIME=timezone(timedelta(hours=8))

def quiet_scan_allowed(now=None):
    hour=datetime.fromtimestamp(time.time() if now is None else now,CHINA_TIME).hour
    return 1<=hour<5

class NavigationBackoff:
    """Scheduling hints only. Avatar never grants identity or send permission."""
    def __init__(self): self.failures={}
    def key(self,row): return row.get('avatar_sha') or row['key']
    def eligible(self,row,now=None):
        if row.get('unread'): return True
        return (time.time() if now is None else now)>=self.failures.get(self.key(row),(0,0))[1]
    def failed(self,row,now=None):
        count=self.failures.get(self.key(row),(0,0))[0]+1
        self.failures[self.key(row)]=(count,(time.time() if now is None else now)+min(900,30*2**min(count-1,5)))
    def succeeded(self,row): self.failures.pop(self.key(row),None)


def polling_configuration(root):
    """Only the current top viewport is eligible for automatic inspection."""
    import json
    from pathlib import Path
    try:
        value=json.loads((Path(root)/'data/polling-policy.json').read_text(encoding='utf-8'))
        if not isinstance(value,dict) or type(value.get('enabled')) is not bool or value.get('scope','main_visible')!='main_visible':
            raise ValueError('INVALID_POLLING_POLICY')
        return dict(value,scope='main_visible')
    except FileNotFoundError:
        return {'enabled':True,'scope':'main_visible'}
    except (ValueError,OSError,TypeError):
        return {'enabled':False,'scope':'main_visible','error':'INVALID_POLLING_POLICY'}


def polling_enabled(root):
    return polling_configuration(root)['enabled']


def set_polling(root,enabled):
    from pathlib import Path
    from state_io import atomic_json
    if type(enabled) is not bool: raise ValueError('INVALID_POLLING_ENABLED')
    path=Path(root)/'data/polling-policy.json';path.parent.mkdir(parents=True,exist_ok=True)
    atomic_json(path,{'enabled':enabled,'scope':'main_visible','updated_at':time.time()})


def eligible_visible_rows(root,store,rows):
    """Do not let known excluded groups or system push badges starve people.

    These are exclusion hints only; dispatch and sending still recheck identity
    and the authoritative group allowlist. Unknown rows keep normal title checks.
    """
    import json
    from group_policy import allowed
    system={'腾讯新闻','服务号','订阅号消息','微信团队','微信支付','折叠的聊天'}
    routes={}
    for cid,kind,raw in store.db.execute('SELECT r.conversation_id,p.kind,r.target_json FROM conversation_routes r JOIN conversation_profiles p ON p.conversation_id=r.conversation_id'):
        target=json.loads(raw)
        routes.setdefault((target.get('name'),target.get('avatar_sha')),[]).append((cid,kind,target))
    eligible=[];excluded=[]
    for row in rows:
        reason='system_push' if row.get('name') in system else None
        from name_match import names_match
        known=[]
        for entries in routes.values():
            for cid,kind,target in entries:
                if names_match(row.get('name'),target.get('name')) and (kind=='group' or row.get('avatar_sha')==target.get('avatar_sha')):
                    item=(store.canonical(cid),kind,target)
                    if not any(v[0]==item[0] for v in known):known.append(item)
        if not reason and len(known)==1:
            cid,kind,target=known[0]
            if kind=='group' and not allowed(root,target,cid,'group'):reason='group_not_whitelisted'
        if reason:excluded.append({'name':row.get('name'),'key':row.get('key'),'reason':reason,'unread':row.get('unread',False)})
        else:eligible.append(row)
    return eligible,excluded

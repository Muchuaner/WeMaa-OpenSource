"""Group automation allowlist, checked before dispatch and before sending."""
import json,re,sqlite3
from pathlib import Path

def is_group(target,conversation_type=None):
    return conversation_type=='group' or bool(re.search(r'[（(]\d{1,4}[）)]?$',str(target.get('conversation_title',''))))

def allowed(root,target,conversation_id=None,conversation_type=None):
    if not is_group(target,conversation_type):return True
    path=Path(root)/'data/group-whitelist.json'
    try:
        policy=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'enabled':True,'groups':[{'name':'开发组'}]}
        if policy.get('enabled') is not True or not isinstance(policy.get('groups'),list):return False
        for group in policy['groups']:
            # User-authorized group identity follows names/members, never avatars.
            from name_match import names_match
            if names_match(group.get('name'),target.get('name')):return True
            if group.get('conversation_id') and conversation_id:
                from contextlib import closing
                with closing(sqlite3.connect((Path(root)/'data/wechat.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as db:
                    def canonical(cid):
                        seen=set()
                        while cid not in seen:
                            seen.add(cid)
                            row=db.execute('SELECT canonical FROM group_aliases WHERE alias=?',(cid,)).fetchone()
                            if not row:break
                            cid=row[0]
                        return cid
                    if canonical(group['conversation_id'])==canonical(conversation_id):return True
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):return False
    return False


def configuration(root):
    """Export the exact effective file revision for optimistic admin updates."""
    import hashlib
    path=Path(root)/'data/group-whitelist.json'
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('enabled') is not True or not isinstance(value.get('groups'),list):raise ValueError('GROUP_POLICY_INVALID')
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return {**value,'revision':hashlib.sha256(raw).hexdigest(),'updated_at':path.stat().st_mtime}

def apply_configuration(root,params):
    """Resolve selectors in local group evidence; never trust supplied names."""
    import sqlite3,re
    from admin_agent import atomic_json
    ids=params.get('conversation_ids');expected=params.get('expected_revision')
    if (not isinstance(ids,list) or len(ids)>100 or any(not isinstance(cid,str) or not re.fullmatch('[a-f0-9]{64}',cid) for cid in ids) or len(set(ids))!=len(ids) or not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected)):
        raise ValueError('GROUP_POLICY_PARAMETERS_INVALID')
    root=Path(root)
    if configuration(root)['revision']!=expected:raise ValueError('GROUP_POLICY_CHANGED_REFRESH_REQUIRED')
    groups=[]
    from contextlib import closing
    with closing(sqlite3.connect(root/'data/wechat.sqlite3')) as db:
        for cid in ids:
            row=db.execute("SELECT r.target_json FROM conversation_routes r JOIN conversation_profiles p ON p.conversation_id=r.conversation_id WHERE r.conversation_id=? AND p.kind='group'",(cid,)).fetchone()
            if not row:raise ValueError('GROUP_ROUTE_NOT_OBSERVED')
            target=json.loads(row[0])
            if not isinstance(target.get('avatar_sha'),str) or not re.fullmatch('[a-f0-9]{64}',target['avatar_sha']):raise ValueError('GROUP_AVATAR_NOT_VERIFIED')
            groups.append({'name':target['name'],'conversation_id':cid,'avatar_sha':target['avatar_sha']})
    # Recheck after resolving all selectors, before the atomic replacement.
    if configuration(root)['revision']!=expected:raise ValueError('GROUP_POLICY_CHANGED_REFRESH_REQUIRED')
    atomic_json(root/'data/group-whitelist.json',{'enabled':True,'groups':groups,'private_chats_allowed':True})
    return {'status':'applied','group_whitelist':configuration(root)}

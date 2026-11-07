"""Retired experiments remain disabled; shared identity helpers support greetings."""
import json,re
from zoneinfo import ZoneInfo
from wemaa.services import maa,replies
TZ=ZoneInfo("Asia/Shanghai")
KEY="wechat-maa:experiment:xiaozhou-v1"
SYSTEM_NAMES={"文件传输助手","微信团队","微信支付","服务通知","订阅号消息","折叠的聊天"}
def initial(): return {"enabled":False,"members":{}}
def state(): return json.loads(replies.client().get(KEY) or "null") or initial()
def claim_reason(row,s): return "EXPERIMENT_RETIRED" if row.get("experiment") else None

def candidates(record):
    """Require an entire, position-verified personal scan, not a name inventory."""
    if maa.is_stale(record):
        raise maa.conflict('微信遥测已过期')
    telemetry = record['telemetry']
    scans = [s for s in telemetry.get('scans', []) if s.get('scope') == 'personal'
             and s.get('status') == 'complete' and s.get('coverage_valid') is True
             and s.get('bottom_verified') is True]
    if not scans: raise maa.conflict('尚无完整的个人联系人扫描')
    scan = max(scans, key=lambda s: s.get('finished') or s.get('started') or 0)
    rows = [r for r in telemetry.get('contacts', []) if r.get('scope') == 'personal' and r.get('scan_id') == scan['id']]
    if sorted(r.get('ordinal', -1) for r in rows) != list(range(scan['expected'])):
        raise maa.conflict('个人联系人扫描位置不连续')
    result = {}
    for row in rows:
        if (row.get('wechat_id') and row.get('id') == 'wx:' + row['wechat_id']
                and row.get('display_name') not in SYSTEM_NAMES
                and row.get('list_name') not in SYSTEM_NAMES):
            result[row['id']] = row
    return result

def validate_contact(body):
    cid, proof = body.get('contact_id'), body.get('identity')
    rows = candidates(maa.latest())
    if cid not in rows or not isinstance(proof, dict): raise maa.invalid('实验好友未核验')
    if (proof.get('stable') is not True or proof.get('wechat_id') != rows[cid]['wechat_id']
            or not maa.HEX64.fullmatch(str(proof.get('profile_sha256', '')))):
        raise maa.invalid('实验好友资料身份不一致')
    return cid

def classify(text):
    """Conservative independent recognizer: uncertain phrasing is reviewed."""
    clean = text.strip()
    if re.search(r'别再联系|不要再联系|别给我发|不要给我发|退出实验|停止联系|不想参加', clean): return 'exit'
    # Quoted or third-person reports never count as a direct accusation.
    if re.search(r'[“”「」『』"\n]|他说|她说|别人说|有人说|引用|转发', clean): return 'ordinary'
    if re.fullmatch(r'(?:我觉得|我感觉|我怀疑|感觉|原来)?(?:你|小周)(?:到底|究竟|其实|难道|真的|该不会|不会|好像|看起来|感觉|是|是不是|不|个|一|很像|像|真|就|\s){0,15}(?:AI|ai|ＡＩ|人工智能|机器人|语言模型|真人|人类|人)(?:[？?！!。,.，吧嘛吗呢呀啊 ]|对不对|是不是|没错吧|了)*', clean) or re.fullmatch(r'(?:AI|ai|人工智能|机器人)[？?吗吧 ]*',clean) or re.fullmatch(r'are you (?:an? )?(?:ai|bot|human)[?.! ]*',clean,re.I):
        return 'identity'
    if re.search(r'AI|ai|人工智能|机器人|语言模型|真人|人类', clean): return 'review'
    return 'ordinary'

"""离线迁移旧网关的合成人物/会话 ID，禁止按昵称关联。"""
from pathlib import Path

import json
import sqlite3


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _replace(value, mapping):
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        if value.startswith('chat_history:') and value[13:] in mapping:
            return 'chat_history:' + mapping[value[13:]]
    if isinstance(value, list):
        return [_replace(item, mapping) for item in value]
    if isinstance(value, dict):
        return {mapping.get(key, key): _replace(item, mapping) for key, item in value.items()}
    return value


def migrate_records(core_path: Path, memory_path: Path, mappings: list[dict]) -> dict:
    """调用方先备份、停止核心，并通过 chat_manager 创建目标会话。

    同一连接事务更新两个库。正文、向量、原始微信 contact_id 均保持原样。
    只允许合并尚未学习的目标占位人物；任何其他碰撞都停止迁移。
    """
    db = sqlite3.connect(core_path)
    db.row_factory = sqlite3.Row
    db.execute('ATTACH DATABASE ? AS memory', (str(memory_path),))
    changes = 0
    replacements = {}
    active = []
    try:
        with db:
            for item in mappings:
                old = db.execute('SELECT * FROM person_info WHERE platform=? AND user_id=? AND person_id=?',
                                 ('wechat', item['contact_id'], item['old_person_id'])).fetchone()
                if old is None:
                    continue
                target = db.execute('SELECT * FROM person_info WHERE person_id=?',
                                    (item['person_id'],)).fetchone()
                if target:
                    if (target['platform'] != 'wechat' or target['user_id'] != item['user_id']
                            or target['memory_points'] not in ('[]', '', None)
                            or (target['know_counts'] or 0) > 1):
                        raise ValueError('NONEMPTY_PERSON_COLLISION_REQUIRES_REVIEW')
                    db.execute('DELETE FROM person_info WHERE id=?', (target['id'],))
                db.execute('UPDATE person_info SET person_id=?,user_id=? WHERE id=?',
                           (item['person_id'], item['user_id'], old['id']))
                previous = db.execute('SELECT * FROM chat_sessions WHERE platform=? AND session_id=? AND user_id=?',
                                      ('wechat',item['old_session_id'],item['contact_id'])).fetchone()
                if previous:
                    destination = db.execute('SELECT * FROM chat_sessions WHERE session_id=?',
                                             (item['session_id'],)).fetchone()
                    if destination is None or destination['user_id'] != item['user_id']:
                        raise ValueError('OFFICIAL_SESSION_NOT_REGISTERED')
                    db.execute('UPDATE chat_sessions SET created_timestamp=?,last_active_timestamp=? WHERE id=?',
                               (previous['created_timestamp'], previous['last_active_timestamp'],destination['id']))
                    db.execute('DELETE FROM chat_sessions WHERE id=?',(previous['id'],))
                replacements[item['old_person_id']] = item['person_id']
                replacements[item['old_session_id']] = item['session_id']
                active.append(item)

            for schema in ('main', 'memory'):
                tables = db.execute(f"SELECT name,sql FROM {schema}.sqlite_master WHERE type='table'").fetchall()
                for table in tables:
                    name = table['name']
                    # FTS contents/indexes are managed by their existing triggers.
                    if name.startswith('sqlite_') or 'fts' in name or 'VIRTUAL TABLE' in (table['sql'] or ''):
                        continue
                    columns = db.execute(f'PRAGMA {schema}.table_info({_identifier(name)})').fetchall()
                    text_columns = [c['name'] for c in columns if any(t in c['type'].upper() for t in ('TEXT','CHAR','JSON'))]
                    for column in text_columns:
                        qualified = f'{schema}.{_identifier(name)}'
                        qcol = _identifier(column)
                        if column in ('person_id','scope_id','session_id','chat_id','source','source_key'):
                            for old, new in replacements.items():
                                changes += db.execute(f'UPDATE {qualified} SET {qcol}=? WHERE {qcol}=?', (new,old)).rowcount
                                if old.startswith('session_'):
                                    changes += db.execute(f'UPDATE {qualified} SET {qcol}=? WHERE {qcol}=?',
                                                          ('chat_history:'+new,'chat_history:'+old)).rowcount
                        # Update structured identity references, never unstructured text or captions.
                        if column.endswith('_json') or column in ('metadata','session_id_dict','additional_config'):
                            for row in db.execute(f'SELECT rowid AS __row,{qcol} AS value FROM {qualified} WHERE {qcol} IS NOT NULL').fetchall():
                                try:
                                    data = json.loads(row['value'])
                                except (ValueError, TypeError):
                                    continue
                                changed = _replace(data, replacements)
                                if changed != data:
                                    db.execute(f'UPDATE {qualified} SET {qcol}=? WHERE rowid=?',
                                               (json.dumps(changed,ensure_ascii=False),row['__row']))
                                    changes += 1
            # Original received-message authors use the exact old contact ID.
            for item in active:
                cols = {r['name'] for r in db.execute('PRAGMA table_info(mai_messages)')}
                if 'user_id' in cols:
                    db.execute('UPDATE mai_messages SET user_id=? WHERE user_id=? AND session_id=?',
                               (item['user_id'],item['contact_id'],item['session_id']))
        return {'migrated_people':len(active),'updated_references':changes,'nickname_matching':False}
    finally:
        db.close()

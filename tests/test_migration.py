import json
import sqlite3

import pytest

from wemaa.migration import migrate_records


@pytest.fixture
def databases(tmp_path):
    core, memory = tmp_path/'core.db',tmp_path/'memory.db'
    with sqlite3.connect(core) as db:
        db.executescript('''
          CREATE TABLE person_info(id INTEGER PRIMARY KEY,person_id TEXT UNIQUE,user_id TEXT,platform TEXT,
                                   memory_points TEXT,know_counts INTEGER);
          CREATE TABLE chat_sessions(id INTEGER PRIMARY KEY,session_id TEXT UNIQUE,user_id TEXT,platform TEXT,
                                     created_timestamp TEXT,last_active_timestamp TEXT);
          CREATE TABLE mai_messages(id INTEGER PRIMARY KEY,session_id TEXT,user_id TEXT,processed_plain_text TEXT);
          INSERT INTO person_info VALUES(1,'wx_old','wx:exact_id','wechat','["历史偏好"]',8);
          INSERT INTO person_info VALUES(2,'official_person','stable_user','wechat','[]',1);
          INSERT INTO chat_sessions VALUES(1,'session_wx_old','wx:exact_id','wechat','old_created','old_active');
          INSERT INTO chat_sessions VALUES(2,'official_session','stable_user','wechat','new_created','new_active');
          INSERT INTO mai_messages VALUES(1,'session_wx_old','wx:exact_id','历史正文 session_wx_old');
        ''')
    with sqlite3.connect(memory) as db:
        db.executescript('''CREATE TABLE person_profile_snapshots(person_id TEXT,profile_text TEXT);
                           CREATE TABLE paragraphs(source TEXT,metadata TEXT,content TEXT);''')
        db.execute('INSERT INTO person_profile_snapshots VALUES(?,?)',('wx_old','原始画像'))
        db.execute('INSERT INTO paragraphs VALUES(?,?,?)',('chat_history:session_wx_old',
            json.dumps({'person_id':'wx_old','session_id':'session_wx_old','contact_id':'wx:exact_id'}),'原文'))
    mapping=[{'contact_id':'wx:exact_id','old_person_id':'wx_old','person_id':'official_person',
              'user_id':'stable_user','old_session_id':'session_wx_old','session_id':'official_session'}]
    return core,memory,mapping


def test_exact_identity_keeps_history_and_memory(databases):
    core,memory,mapping=databases
    assert migrate_records(*databases)['migrated_people'] == 1
    with sqlite3.connect(core) as db:
        assert db.execute('SELECT person_id,user_id,memory_points,know_counts FROM person_info').fetchall() == [
            ('official_person','stable_user','["历史偏好"]',8)]
        assert db.execute('SELECT session_id,user_id,processed_plain_text FROM mai_messages').fetchone() == (
            'official_session','stable_user','历史正文 session_wx_old')
        assert db.execute('SELECT created_timestamp FROM chat_sessions').fetchone()[0] == 'old_created'
    with sqlite3.connect(memory) as db:
        assert db.execute('SELECT * FROM person_profile_snapshots').fetchone() == ('official_person','原始画像')
        source,metadata,content=db.execute('SELECT * FROM paragraphs').fetchone()
        assert source == 'chat_history:official_session'
        assert json.loads(metadata) == {'person_id':'official_person','session_id':'official_session','contact_id':'wx:exact_id'}
        assert content == '原文'
    assert migrate_records(core,memory,mapping)['migrated_people'] == 0


def test_unknown_contact_is_never_merged(databases):
    core,memory,mapping=databases
    mapping[0]['contact_id']='wx:different'
    assert migrate_records(core,memory,mapping)['migrated_people'] == 0
    with sqlite3.connect(core) as db:
        assert db.execute('SELECT count(*) FROM person_info').fetchone()[0] == 2


def test_nonempty_destination_rolls_back_both_databases(databases):
    core,memory,mapping=databases
    with sqlite3.connect(core) as db:
        db.execute('UPDATE person_info SET memory_points=? WHERE id=2',('["新学习数据"]',))
    with pytest.raises(ValueError,match='NONEMPTY_PERSON_COLLISION'):
        migrate_records(core,memory,mapping)
    with sqlite3.connect(core) as db:
        assert db.execute('SELECT count(*) FROM person_info').fetchone()[0] == 2
    with sqlite3.connect(memory) as db:
        assert db.execute('SELECT person_id FROM person_profile_snapshots').fetchone()[0] == 'wx_old'

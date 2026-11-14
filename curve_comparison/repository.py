import hashlib
import json
from pathlib import Path
import sqlite3

from .domain import validate


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Conflict(ValueError):
    pass


class Repository:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS revisions(
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts(
                    request_key TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    revision INTEGER NOT NULL REFERENCES revisions(revision)
                );
            ''')

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def import_data(self, data, request_key):
        if not isinstance(request_key, str) or not request_key or len(request_key) > 200:
            raise ValueError('request_key 必须是长度不超过 200 的非空字符串')
        if not isinstance(data, dict):
            raise ValueError('资料必须是对象')
        try:
            validate(data)
        except (KeyError, TypeError, IndexError) as error:
            raise ValueError('资料结构不完整或字段类型错误') from error
        payload = canonical(data)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = connection.execute('SELECT digest, revision FROM receipts WHERE request_key=?', (request_key,)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise Conflict('相同请求号不能导入不同资料')
                return {'revision': receipt[1], 'digest': digest, 'replayed': True}
            connection.execute('INSERT OR IGNORE INTO revisions(digest,payload) VALUES (?,?)', (digest, payload))
            revision = connection.execute('SELECT revision FROM revisions WHERE digest=?', (digest,)).fetchone()[0]
            connection.execute('INSERT INTO receipts VALUES(?,?,?)', (request_key, digest, revision))
        return {'revision': revision, 'digest': digest, 'replayed': False}

    def get(self, revision=None):
        with self.connect() as connection:
            if revision is None:
                row = connection.execute('SELECT revision,digest,payload FROM revisions ORDER BY revision DESC LIMIT 1').fetchone()
            else:
                row = connection.execute('SELECT revision,digest,payload FROM revisions WHERE revision=?', (revision,)).fetchone()
        if row is None:
            raise LookupError('资料版本不存在')
        return {'revision': row[0], 'digest': row[1], 'data': json.loads(row[2])}

    def history(self):
        with self.connect() as connection:
            return [{'revision': row[0], 'digest': row[1]} for row in connection.execute('SELECT revision,digest FROM revisions ORDER BY revision')]

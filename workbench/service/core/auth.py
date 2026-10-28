"""Staff accounts and revocable opaque sessions. No password hashes reach clients."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash


STAFF_ROLES = ('sales', 'purchase', 'stock_packing', 'arrival_packing')
_ROLE_SET = frozenset(STAFF_ROLES)
_USERNAME = re.compile(r'^[\w.@+-]+$', re.UNICODE)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Sessions:
    """Own staff identities and issue revocable bearer sessions.

    The initial administrator is bootstrapped from the existing configured
    password hash. Omitting ``username`` at login continues to authenticate
    that account so already released Android clients remain compatible.
    """

    def __init__(self, store, password_hash, lifetime=30 * 86400):
        if not password_hash or not password_hash.startswith(('scrypt:', 'pbkdf2:')):
            raise ValueError('公网模式必须配置管理员密码哈希')
        self.store, self.password_hash, self.lifetime = store, password_hash, lifetime
        self.on_staff_changed = None
        with store.connect(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS staff_users(
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    roles TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    packing_limit INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS staff_users_active_role
                    ON staff_users(active,is_admin);
                CREATE TABLE IF NOT EXISTS auth_sessions(
                    token_hash TEXT PRIMARY KEY,
                    expires REAL NOT NULL,
                    user_id TEXT
                );
                CREATE TABLE IF NOT EXISTS auth_attempts(
                    address TEXT PRIMARY KEY,
                    failures INTEGER,
                    until REAL
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(auth_sessions)')}
            if 'user_id' not in columns:
                db.execute('ALTER TABLE auth_sessions ADD COLUMN user_id TEXT')
            created = _now()
            db.execute('''INSERT OR IGNORE INTO staff_users(
                id,username,display_name,password_hash,roles,is_admin,active,
                packing_limit,created_at,updated_at
            ) VALUES (?,?,?,?,?,1,1,3,?,?)''', (
                'admin', 'admin', '管理员', password_hash,
                json.dumps([], separators=(',', ':')), created, created))
            # Earlier sessions stay usable and now have a concrete audit actor.
            db.execute("UPDATE auth_sessions SET user_id='admin' WHERE user_id IS NULL OR user_id='' ")

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _public(row):
        if row is None:
            return None
        try:
            roles = json.loads(row['roles'])
        except (TypeError, ValueError, json.JSONDecodeError):
            roles = []
        return {
            'id': str(row['id']),
            'username': str(row['username']),
            'display_name': str(row['display_name']),
            'roles': [role for role in STAFF_ROLES if role in roles],
            'is_admin': bool(row['is_admin']),
            'active': bool(row['active']),
            'packing_limit': int(row['packing_limit']),
            'created_at': str(row['created_at']),
            'updated_at': str(row['updated_at']),
        }

    def current(self, token):
        """Return the sanitized active user for an unexpired session."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return None
        with self.store.connect() as db:
            row = db.execute('''SELECT u.* FROM auth_sessions s
                JOIN staff_users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires>? AND u.active=1''',
                (self.digest(token), time.time())).fetchone()
        return self._public(row)

    def valid(self, token):
        return self.current(token) is not None

    def login(self, password, address, username=None):
        now = time.time()
        if (not isinstance(password, str) or len(password) > 1024 or
                username is not None and (not isinstance(username, str) or len(username) > 64)):
            return None, 'invalid_credentials'
        normalized = username.strip().casefold() if isinstance(username, str) else None
        if username is not None and not normalized:
            return None, 'invalid_credentials'
        # The service is behind one fixed reverse proxy, so remote_addr may be
        # identical for every phone. Scope throttling to the attempted account
        # as well as that address to prevent one mistyped login locking out all
        # employees, without trusting spoofable forwarding headers.
        throttle_key = f"{address}|{normalized or 'admin'}"
        address_key = f"{address}|*"
        with self.store.connect(write=True) as db:
            db.execute('DELETE FROM auth_attempts WHERE until < ?', (now,))
            attempt = db.execute('SELECT failures FROM auth_attempts WHERE address=?', (throttle_key,)).fetchone()
            address_attempt = db.execute(
                'SELECT failures FROM auth_attempts WHERE address=?', (address_key,)).fetchone()
            if (attempt and attempt[0] >= 5) or (address_attempt and address_attempt[0] >= 50):
                return None, 'login_rate_limited'
            if normalized is None:
                user = db.execute("SELECT * FROM staff_users WHERE id='admin' AND active=1").fetchone()
            else:
                user = db.execute('SELECT * FROM staff_users WHERE username=? AND active=1',
                                  (normalized,)).fetchone()
            if not user or not check_password_hash(user['password_hash'], password):
                db.execute('''INSERT INTO auth_attempts VALUES (?,1,?) ON CONFLICT(address)
                    DO UPDATE SET failures=failures+1,until=excluded.until''', (throttle_key, now + 300))
                db.execute('''INSERT INTO auth_attempts VALUES (?,1,?) ON CONFLICT(address)
                    DO UPDATE SET failures=failures+1,until=excluded.until''', (address_key, now + 300))
                return None, 'invalid_credentials'
            db.execute('DELETE FROM auth_attempts WHERE address=?', (throttle_key,))
            db.execute('DELETE FROM auth_sessions WHERE expires <= ?', (now,))
            token = secrets.token_urlsafe(48)
            db.execute('INSERT INTO auth_sessions(token_hash,expires,user_id) VALUES (?,?,?)',
                       (self.digest(token), now + self.lifetime, user['id']))
        return token, None

    def revoke(self, token):
        if not isinstance(token, str):
            return
        with self.store.connect(write=True) as db:
            db.execute('DELETE FROM auth_sessions WHERE token_hash=?', (self.digest(token),))

    @staticmethod
    def _username(value):
        if not isinstance(value, str):
            raise ValueError('用户名必须是文本')
        value = value.strip().casefold()
        if not 1 <= len(value) <= 64 or not _USERNAME.fullmatch(value):
            raise ValueError('用户名须为1至64位字母、数字或 . _ @ + -')
        return value

    @staticmethod
    def _display_name(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
            raise ValueError('姓名须为1至80个字符')
        return value.strip()

    @staticmethod
    def _roles(value):
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError('roles必须是岗位数组')
        unknown = set(value) - _ROLE_SET
        if unknown:
            raise ValueError('未知岗位：' + '、'.join(sorted(unknown)))
        return [role for role in STAFF_ROLES if role in set(value)]

    @staticmethod
    def _boolean(value, field):
        if not isinstance(value, bool):
            raise ValueError(field + '必须是布尔值')
        return value

    @staticmethod
    def _packing_limit(value):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 99:
            raise ValueError('packing_limit须为0至99的整数')
        return value

    @staticmethod
    def _password(value):
        if not isinstance(value, str) or not 8 <= len(value) <= 1024:
            raise ValueError('密码须为8至1024个字符')
        return value

    def _admin(self, token):
        user = self.current(token)
        return user if user and user['is_admin'] else None

    @staticmethod
    def _response(error, message, status):
        return jsonify(ok=False, error=error, message=message), status

    def _notify_staff_changed(self, actor_id):
        """Reconcile assignments after the staff transaction has committed.

        Account maintenance must remain successful even when the derived task
        projection needs manual repair; callers receive that state explicitly
        and can use the administrator reconcile action.
        """
        if not callable(self.on_staff_changed):
            return None
        try:
            return {'ok': True, **(self.on_staff_changed(actor_id) or {})}
        except Exception:
            return {'ok': False, 'message': '人员已保存，但任务派工刷新失败，请在任务页重新核对'}

    def routes(self):
        bp = Blueprint('sessions', __name__)

        @bp.post('/api/v2/auth/login')
        def login():
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                return self._response('invalid_credentials', '登录信息无效', 400)
            token, error = self.login(body.get('password'), request.remote_addr or 'unknown',
                                      body.get('username') if 'username' in body else None)
            if error:
                return self._response(error,
                    '请稍后重试' if error == 'login_rate_limited' else '用户名或密码不正确',
                    429 if error == 'login_rate_limited' else 401)
            return jsonify(ok=True, token=token, expires_in=self.lifetime,
                           user=self.current(token))

        @bp.post('/api/v2/auth/logout')
        def logout():
            self.revoke(bearer())
            return jsonify(ok=True)

        @bp.get('/api/v2/me')
        def me():
            return jsonify(ok=True, user=self.current(bearer()))

        @bp.get('/api/v2/staff')
        def list_staff():
            if not self._admin(bearer()):
                return self._response('admin_required', '仅管理员可以管理人员', 403)
            with self.store.connect() as db:
                rows = db.execute('SELECT * FROM staff_users ORDER BY active DESC,display_name,username').fetchall()
            return jsonify(ok=True, staff=[self._public(row) for row in rows])

        @bp.post('/api/v2/staff')
        def create_staff():
            actor = self._admin(bearer())
            if not actor:
                return self._response('admin_required', '仅管理员可以管理人员', 403)
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return self._response('invalid_request', '请求必须为JSON对象', 400)
            allowed = {'username', 'display_name', 'password', 'roles', 'is_admin', 'admin',
                       'active', 'packing_limit'}
            if set(body) - allowed:
                return self._response('invalid_request', '包含不支持的人员字段', 400)
            try:
                username = self._username(body.get('username'))
                display_name = self._display_name(body.get('display_name'))
                password = self._password(body.get('password'))
                roles = self._roles(body.get('roles', []))
                if 'admin' in body and 'is_admin' in body and body['admin'] != body['is_admin']:
                    raise ValueError('admin与is_admin不能冲突')
                is_admin = self._boolean(body.get('is_admin', body.get('admin', False)), 'is_admin')
                active = self._boolean(body.get('active', True), 'active')
                packing_limit = self._packing_limit(body.get('packing_limit', 3))
            except ValueError as error:
                return self._response('invalid_request', str(error), 400)
            roles_json = json.dumps(roles, separators=(',', ':'))
            with self.store.connect(write=True) as db:
                existing = db.execute('SELECT * FROM staff_users WHERE username=?', (username,)).fetchone()
                if existing:
                    unchanged = (
                        existing['display_name'] == display_name and
                        json.loads(existing['roles']) == roles and
                        bool(existing['is_admin']) == is_admin and
                        bool(existing['active']) == active and
                        int(existing['packing_limit']) == packing_limit and
                        check_password_hash(existing['password_hash'], password)
                    )
                    if unchanged:
                        return jsonify(ok=True, staff=self._public(existing), created=False)
                    return self._response('username_exists', '用户名已存在', 409)
                identity, created = str(uuid.uuid4()), _now()
                try:
                    db.execute('''INSERT INTO staff_users(
                        id,username,display_name,password_hash,roles,is_admin,active,
                        packing_limit,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)''', (
                        identity, username, display_name, generate_password_hash(password), roles_json,
                        int(is_admin), int(active), packing_limit, created, created))
                except sqlite3.IntegrityError:
                    return self._response('username_exists', '用户名已存在', 409)
                row = db.execute('SELECT * FROM staff_users WHERE id=?', (identity,)).fetchone()
            reconcile = self._notify_staff_changed(actor['id'])
            return jsonify(ok=True, staff=self._public(row), created=True,
                           task_reconcile=reconcile), 201

        @bp.patch('/api/v2/staff/<user_id>')
        def update_staff(user_id):
            actor = self._admin(bearer())
            if not actor:
                return self._response('admin_required', '仅管理员可以管理人员', 403)
            body = request.get_json(silent=True)
            allowed = {'username', 'display_name', 'roles', 'is_admin', 'admin', 'active', 'packing_limit'}
            if not isinstance(body, dict) or not body or set(body) - allowed:
                return self._response('invalid_request', '没有可更新的人员字段', 400)
            try:
                values = {}
                if 'username' in body:
                    values['username'] = self._username(body['username'])
                if 'display_name' in body:
                    values['display_name'] = self._display_name(body['display_name'])
                if 'roles' in body:
                    values['roles'] = json.dumps(self._roles(body['roles']), separators=(',', ':'))
                if 'admin' in body and 'is_admin' in body and body['admin'] != body['is_admin']:
                    raise ValueError('admin与is_admin不能冲突')
                if 'admin' in body or 'is_admin' in body:
                    values['is_admin'] = int(self._boolean(body.get('is_admin', body.get('admin')), 'is_admin'))
                if 'active' in body:
                    values['active'] = int(self._boolean(body['active'], 'active'))
                if 'packing_limit' in body:
                    values['packing_limit'] = self._packing_limit(body['packing_limit'])
            except ValueError as error:
                return self._response('invalid_request', str(error), 400)
            with self.store.connect(write=True) as db:
                existing = db.execute('SELECT * FROM staff_users WHERE id=?', (user_id,)).fetchone()
                if not existing:
                    return self._response('staff_not_found', '人员不存在', 404)
                removing_admin = bool(existing['is_admin']) and (
                    values.get('is_admin', 1) == 0 or values.get('active', 1) == 0)
                if removing_admin:
                    others = db.execute('''SELECT COUNT(*) FROM staff_users
                        WHERE is_admin=1 AND active=1 AND id<>?''', (user_id,)).fetchone()[0]
                    if not others:
                        return self._response('last_admin_required', '必须保留至少一名启用的管理员', 409)
                values['updated_at'] = _now()
                assignments = ','.join(f'{field}=?' for field in values)
                try:
                    db.execute(f'UPDATE staff_users SET {assignments} WHERE id=?',
                               (*values.values(), user_id))
                except sqlite3.IntegrityError:
                    return self._response('username_exists', '用户名已存在', 409)
                if values.get('active') == 0:
                    db.execute('DELETE FROM auth_sessions WHERE user_id=?', (user_id,))
                row = db.execute('SELECT * FROM staff_users WHERE id=?', (user_id,)).fetchone()
            reconcile = self._notify_staff_changed(actor['id'])
            return jsonify(ok=True, staff=self._public(row), task_reconcile=reconcile)

        @bp.post('/api/v2/staff/<user_id>/password')
        def reset_password(user_id):
            if not self._admin(bearer()):
                return self._response('admin_required', '仅管理员可以管理人员', 403)
            body = request.get_json(silent=True)
            try:
                if not isinstance(body, dict) or set(body) != {'password'}:
                    raise ValueError('仅接受password字段')
                password = self._password(body['password'])
            except (KeyError, ValueError) as error:
                return self._response('invalid_request', str(error), 400)
            with self.store.connect(write=True) as db:
                if not db.execute('SELECT 1 FROM staff_users WHERE id=?', (user_id,)).fetchone():
                    return self._response('staff_not_found', '人员不存在', 404)
                db.execute('UPDATE staff_users SET password_hash=?,updated_at=? WHERE id=?',
                           (generate_password_hash(password), _now(), user_id))
                db.execute('DELETE FROM auth_sessions WHERE user_id=?', (user_id,))
            return jsonify(ok=True, sessions_revoked=True)

        return bp


def bearer():
    prefix, _, value = request.headers.get('Authorization', '').partition(' ')
    return value if prefix == 'Bearer' else ''

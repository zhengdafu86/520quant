"""
用户鉴权存储
- 独立 SQLite 库：~/.520quant/auth.db（与各用户业务数据物理隔离）
- 密码使用 werkzeug 加盐哈希，绝不存明文
- 仅负责"账号是谁/密码对不对"，不涉及持仓/账户数据
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash


BASE_DIR = Path.home() / ".520quant"
AUTH_DB  = BASE_DIR / "auth.db"

# 用户名：3-32 位字母/数字/下划线（同时作为 DB 目录名，须文件系统安全）
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
_MIN_PASSWORD_LEN = 6


def _conn() -> sqlite3.Connection:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUTH_DB), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT,
            is_admin      INTEGER DEFAULT 0
        )
    """)
    # 迁移：旧 auth.db 可能没有 is_admin 列
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    except Exception:
        pass   # 列已存在，忽略
    conn.commit()

    # 引导：若当前没有任何管理员，把最早创建的用户提升为管理员
    # （首个用户 = 系统拥有者，如 zhengdafu86；线上部署后自动生效，无需手动）
    try:
        has_admin = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin=1"
        ).fetchone()[0]
        if has_admin == 0:
            first = conn.execute(
                "SELECT id FROM users ORDER BY id LIMIT 1"
            ).fetchone()
            if first:
                conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (first[0],))
                conn.commit()
    except Exception:
        pass

    return conn


def valid_username(username: str) -> bool:
    """用户名合法性校验（兼作目录名，需文件系统安全）"""
    return bool(_USERNAME_RE.match(username or ""))


def user_exists(username: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM users WHERE username=?", (username,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def create_user(username: str, password: str,
                is_admin: bool = False) -> tuple[bool, str]:
    """创建用户。返回 (成功, 消息)"""
    if not valid_username(username):
        return False, "用户名须为 3-32 位字母 / 数字 / 下划线"
    if not password or len(password) < _MIN_PASSWORD_LEN:
        return False, f"密码至少 {_MIN_PASSWORD_LEN} 位"

    conn = _conn()
    try:
        if conn.execute(
            "SELECT 1 FROM users WHERE username=?", (username,)
        ).fetchone():
            return False, f"用户 {username} 已存在"
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at, is_admin) "
            "VALUES(?,?,?,?)",
            (username, generate_password_hash(password, method="pbkdf2:sha256"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             1 if is_admin else 0),
        )
        conn.commit()
        return True, f"用户 {username} 创建成功"
    finally:
        conn.close()


def is_admin(username: str) -> bool:
    """该用户是否为管理员"""
    if not username:
        return False
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT is_admin FROM users WHERE username=?", (username,)
        ).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def set_admin(username: str, admin: bool = True) -> tuple[bool, str]:
    """设置 / 取消管理员"""
    conn = _conn()
    try:
        if not conn.execute(
            "SELECT 1 FROM users WHERE username=?", (username,)
        ).fetchone():
            return False, f"用户 {username} 不存在"
        conn.execute(
            "UPDATE users SET is_admin=? WHERE username=?",
            (1 if admin else 0, username),
        )
        conn.commit()
        return True, f"用户 {username} 已{'设为管理员' if admin else '取消管理员'}"
    finally:
        conn.close()


def delete_user(username: str) -> tuple[bool, str]:
    """删除用户（仅删 auth 记录；业务数据目录由调用方另行清理）"""
    conn = _conn()
    try:
        if not conn.execute(
            "SELECT 1 FROM users WHERE username=?", (username,)
        ).fetchone():
            return False, f"用户 {username} 不存在"
        conn.execute("DELETE FROM users WHERE username=?", (username,))
        conn.commit()
        return True, f"用户 {username} 已删除"
    finally:
        conn.close()


def verify_user(username: str, password: str) -> bool:
    """校验用户名 + 密码"""
    if not username or not password:
        return False
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username=?", (username,)
        ).fetchone()
        if not row:
            return False
        return check_password_hash(row[0], password)
    finally:
        conn.close()


def set_password(username: str, password: str) -> tuple[bool, str]:
    """重置密码"""
    if not password or len(password) < _MIN_PASSWORD_LEN:
        return False, f"密码至少 {_MIN_PASSWORD_LEN} 位"
    conn = _conn()
    try:
        if not conn.execute(
            "SELECT 1 FROM users WHERE username=?", (username,)
        ).fetchone():
            return False, f"用户 {username} 不存在"
        conn.execute(
            "UPDATE users SET password_hash=? WHERE username=?",
            (generate_password_hash(password, method="pbkdf2:sha256"), username),
        )
        conn.commit()
        return True, f"用户 {username} 密码已更新"
    finally:
        conn.close()


def list_users() -> list[dict]:
    """列出所有用户（按创建顺序），含角色与创建时间"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT username, COALESCE(is_admin,0), COALESCE(created_at,'') "
            "FROM users ORDER BY id"
        ).fetchall()
        return [
            {"username": r[0], "is_admin": bool(r[1]), "created_at": r[2]}
            for r in rows
        ]
    finally:
        conn.close()

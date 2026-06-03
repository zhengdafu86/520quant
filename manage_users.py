"""
用户管理 CLI
用法：
  python manage_users.py create <用户名> <密码>     # 创建用户（普通）
  python manage_users.py create <用户名> <密码> admin  # 创建管理员
  python manage_users.py passwd <用户名> <新密码>   # 重置密码
  python manage_users.py admin  <用户名>            # 设为管理员
  python manage_users.py unadmin <用户名>           # 取消管理员
  python manage_users.py list                       # 列出所有用户
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from auth.users import create_user, set_password, set_admin, list_users
from trader.paper import USERS_DIR


def _ensure_user_dir(username: str):
    """预创建用户账户库目录（首次访问时也会自动建，这里提前建好更直观）"""
    (USERS_DIR / username).mkdir(parents=True, exist_ok=True)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "create" and len(args) >= 3:
        username, password = args[1], args[2]
        make_admin = len(args) >= 4 and args[3] == "admin"
        ok, msg = create_user(username, password, is_admin=make_admin)
        if ok:
            _ensure_user_dir(username)
            if make_admin:
                msg += "（管理员）"
        print(f"{'✅' if ok else '❌'} {msg}")

    elif cmd == "passwd" and len(args) >= 3:
        username, password = args[1], args[2]
        ok, msg = set_password(username, password)
        print(f"{'✅' if ok else '❌'} {msg}")

    elif cmd == "admin" and len(args) >= 2:
        ok, msg = set_admin(args[1], True)
        print(f"{'✅' if ok else '❌'} {msg}")

    elif cmd == "unadmin" and len(args) >= 2:
        ok, msg = set_admin(args[1], False)
        print(f"{'✅' if ok else '❌'} {msg}")

    elif cmd == "list":
        users = list_users()
        if not users:
            print("（暂无用户）")
        else:
            print("用户列表：")
            for u in users:
                role = "管理员" if u["is_admin"] else "普通"
                print(f"  - {u['username']:<20} [{role}]  {u['created_at']}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()

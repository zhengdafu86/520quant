"""
多用户化迁移脚本（一次性运行，幂等可重复执行）

把多用户化之前的单一账户库 ~/.520quant/paper_trade.db 迁移给指定用户，
并把其中的扫描结果导入共享库 ~/.520quant/market.db。

用法：
  python migrate_multiuser.py                       # 迁移给默认用户 zhengdafu86
  python migrate_multiuser.py <用户名> <密码>       # 迁移给指定用户

执行内容：
  1. 创建用户（已存在则跳过）
  2. 将 legacy paper_trade.db 移动到 users/<用户名>/paper_trade.db（目标已存在则跳过，避免覆盖）
  3. 将 legacy 库里的 scan_results 复制进共享 market.db（供所有用户读取）
"""
from __future__ import annotations

import sys
import shutil
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from auth.users import create_user, user_exists
from trader.paper import LEGACY_DB, USERS_DIR, MARKET_DB, _init_scan_db

DEFAULT_USER = "zhengdafu86"
DEFAULT_PASS = "861227"

_SCAN_COLS = ("scan_date", "code", "name", "price", "signal", "reason",
              "score", "stop_price", "rs_score", "sector_dir",
              "cross_date", "sector_name", "score_detail")


def _copy_scan_results(src_db: Path):
    """把源库的 scan_results 拷进共享 market.db（容错：缺列用默认值）"""
    if not src_db.exists():
        return
    try:
        src = sqlite3.connect(str(src_db))
        # 源库可能列不全，按存在的列读取
        existing = {r[1] for r in src.execute("PRAGMA table_info(scan_results)").fetchall()}
        if not existing:
            src.close()
            return
        read_cols = [c for c in _SCAN_COLS if c in existing]
        rows = src.execute(
            f"SELECT {','.join(read_cols)} FROM scan_results"
        ).fetchall()
        src.close()
        if not rows:
            print("  · legacy 库无扫描结果，跳过")
            return

        MARKET_DB.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(MARKET_DB))
        _init_scan_db(dst)
        # 防重复：先清掉相同 scan_date 的记录
        dates = {r[read_cols.index("scan_date")] for r in rows if "scan_date" in read_cols}
        for d in dates:
            dst.execute("DELETE FROM scan_results WHERE scan_date=?", (d,))
        placeholders = ",".join("?" * len(read_cols))
        dst.executemany(
            f"INSERT INTO scan_results ({','.join(read_cols)}) VALUES ({placeholders})",
            rows,
        )
        dst.commit()
        dst.close()
        print(f"  · 已复制 {len(rows)} 条扫描结果到共享库 market.db")
    except Exception as e:
        print(f"  · 扫描结果复制失败（不影响迁移，扫描可重跑）：{e}")


def migrate(username: str, password: str):
    print(f"\n=== 多用户化迁移 → {username} ===\n")

    # 1) 创建用户
    if user_exists(username):
        print(f"① 用户 {username} 已存在，跳过创建")
    else:
        ok, msg = create_user(username, password)
        print(f"① {'✅' if ok else '❌'} {msg}")
        if not ok:
            print("用户创建失败，终止迁移")
            return

    target_db = USERS_DIR / username / "paper_trade.db"
    target_db.parent.mkdir(parents=True, exist_ok=True)

    # 2) 迁移 legacy 账户库
    if target_db.exists():
        print(f"② 目标账户库已存在（{target_db}），跳过迁移以防覆盖")
    elif LEGACY_DB.exists():
        # 先复制扫描结果到共享库（移动前读取 legacy）
        _copy_scan_results(LEGACY_DB)
        shutil.move(str(LEGACY_DB), str(target_db))
        print(f"② ✅ legacy 账户库已迁移到 {target_db}")
    else:
        print(f"② legacy 账户库不存在（{LEGACY_DB}），按全新空账户初始化")

    print("\n=== 迁移完成 ===")
    print(f"现在可用 {username} 登录 Web，原持仓/记录/自选/账户均已归属该用户。\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    user = args[0] if len(args) >= 1 else DEFAULT_USER
    pwd  = args[1] if len(args) >= 2 else DEFAULT_PASS
    migrate(user, pwd)

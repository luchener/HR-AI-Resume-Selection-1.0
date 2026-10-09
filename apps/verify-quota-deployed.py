# -*- coding: utf-8 -*-
"""在部署容器内验证配额逻辑（不触碰真实数据：只 patch auth 查询 + 用临时目录）。"""
import os, sys, json, shutil
import config as C
import auth as A
import quota as Q

BASE = "/app/data/.verify-quota"
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(os.path.join(BASE, "usage"), exist_ok=True)
os.makedirs(os.path.join(BASE, "rate"), exist_ok=True)
C.QUOTA_LIMITS_PATH = os.path.join(BASE, "limits.json")
C.USER_USAGE_DIR = os.path.join(BASE, "usage")
C.RATE_LIMITS_DIR = os.path.join(BASE, "rate")
A.USER_USAGE_DIR = C.USER_USAGE_DIR

USER = {"user_id": "u-1", "username": "verify_user", "email": "u@t.local", "is_admin": False}
ADMIN = {"user_id": "u-2", "username": "verify_admin", "email": "a@t.local", "is_admin": True}
A.find_user_by_id = lambda uid: USER if uid == "u-1" else (ADMIN if uid == "u-2" else None)
A.find_user_by_username = lambda name: USER if name == "verify_user" else (ADMIN if name == "verify_admin" else None)
A.record_user_usage = lambda uid, kind: None  # 由脚本自行写 usage

fails = []
def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "  <- " + str(extra)))
    if not cond:
        fails.append(name)

def set_used(uid, n):
    p = os.path.join(C.USER_USAGE_DIR, uid + ".json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({Q._today_key(): {"analysis": n}}, fh)

# 基线：默认不限
check("默认不限", Q.check_analysis_quota("u-1", 3) is None)

# ① 逐账号 -1 必须覆盖全局默认（历史 bug：-1 被折叠成 None）
Q.update_settings({"quota_enabled": True, "default_daily": 3})
set_used("u-1", 10)
check("全局默认 3 拦截", Q.check_analysis_quota("u-1", 1) is not None)
Q.set_user_quota("verify_user", {"daily_limit": -1})
check("显式 -1 真正不限", Q.check_analysis_quota("u-1", 3) is None)
check("-1 落盘未被折叠", Q._load()["users"]["verify_user"]["daily_limit"] == -1)

# ② 账号禁用优先于全局开关
Q.update_settings({"quota_enabled": False})
Q.set_user_quota("verify_user", {"analysis_enabled": False})
err = Q.check_analysis_quota("u-1", 1)
check("全局关时禁用仍生效(403)", err is not None and err[1] == 403, err)
me = Q.get_my_quota("u-1")
check("quota/me 反映禁用", me.get("analysis_enabled") is False, me)

# ③ 管理员：未配置豁免；显式配置后生效
Q.update_settings({"quota_enabled": True, "default_daily": 1})
set_used("u-2", 5)
check("管理员未配置豁免额度", Q.check_analysis_quota("u-2", 3) is None)
check("管理员 me 不限", Q.get_my_quota("u-2").get("unlimited") is True)
Q.set_user_quota("verify_admin", {"daily_limit": 2})
set_used("u-2", 0)
check("管理员显式 2 次放行", Q.check_analysis_quota("u-2", 1) is None)
err = Q.check_analysis_quota("u-2", 5)
check("管理员显式限额生效(429)", err is not None and err[1] == 429, err)
me = Q.get_my_quota("u-2")
check("管理员 me 显示限额", me.get("unlimited") is False and me.get("daily_limit") == 2, me)

# ④ 管理员被禁用同样拦截 + me 反映
Q.set_user_quota("verify_admin", {"analysis_enabled": False})
err = Q.check_analysis_quota("u-2", 1)
check("管理员禁用拦截(403)", err is not None and err[1] == 403, err)
check("管理员 me 反映禁用", Q.get_my_quota("u-2").get("analysis_enabled") is False)

# ⑤ 管理端明细 raw/生效 字段
Q.update_settings({"quota_enabled": True, "default_daily": 7})
Q.set_user_quota("verify_user", {"analysis_enabled": True})
s = Q.get_settings()
row = next(u for u in s["users"] if u["username"] == "verify_user")
check("未配置: custom False", row["custom"] is False, row)
check("未配置: raw None", row["raw_daily_limit"] is None, row)
check("未配置: 生效 7", row["daily_limit"] == 7, row)
Q.set_user_quota("verify_user", {"daily_limit": -1})
row = next(u for u in Q.get_settings()["users"] if u["username"] == "verify_user")
check("配置 -1: raw=-1 生效=-1", row["raw_daily_limit"] == -1 and row["daily_limit"] == -1, row)

# ⑥ 边界值
try:
    Q._coerce_limit(1001); check("越界 1001 报错", False)
except ValueError: check("越界 1001 报错", True)
try:
    Q._coerce_limit("abc"); check("非数字报错", False)
except ValueError: check("非数字报错", True)
check("空串 = 未配置(None)", Q._coerce_limit("") is None)
check("None = 未配置(None)", Q._coerce_limit(None) is None)
check("0 保留为 0", Q._coerce_limit(0) == 0)

shutil.rmtree(BASE, ignore_errors=True)
print("")
print("RESULT: " + ("ALL PASS" if not fails else ("FAILED: " + ", ".join(fails))))
sys.exit(1 if fails else 0)

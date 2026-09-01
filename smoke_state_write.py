"""验证 tkinter 回退 UI 的 state 整写 bug（ui.py 登录成功分支）。

模拟场景：全新目录 → 首次运行落盘 first_run_at → 守护写入 events →
UI 登录成功写 userIndex/loginTime。分别用「旧的 save_state 整写」与
「新的 update_state 合并写」跑一遍，对比 state.json 字段与
first_deploy()（决定"空文件夹提醒"是否重现）。
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import campusnet as cn  # noqa: E402

BIN = tempfile.mkdtemp(prefix="_t_state_")
state = os.path.join(BIN, "state.json")

cn.BASE_DIR = BIN
cn.STATE_PATH = state

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))


def reset():
    if os.path.exists(state):
        os.remove(state)


def scenario(mode):
    """mode='old' 整写（bug 行为） / 'new' 合并写（修复后）"""
    reset()
    # 1) 首次运行：落盘 first_run_at
    cn.FIRST_DEPLOY = True
    cn.mark_first_run()
    # 2) 守护写入事件（掉线/恢复/登录）
    cn.record_event("restored", "网络已恢复")
    cn.record_event("login_ok", "认证成功")
    # 3) 致命登录失败提示（登录成功后应被清掉）
    cn.update_state({"last_error": {"msg": "账号或密码错误"}})
    before = json.load(open(state, encoding="utf-8"))

    # 4) UI 登录成功写会话字段
    patch = {"userIndex": "u123", "loginTime": "2026-09-01 13:00:00"}
    if mode == "old":
        cn.save_state(dict(patch))          # 旧：整体替换
    else:
        cn.update_state(patch, drop=("last_error",))  # 新：合并写

    after = json.load(open(state, encoding="utf-8"))
    return before, after


print("=== 场景 A：旧行为（save_state 整写）===")
b, a = scenario("old")
print("  写入前字段:", sorted(b))
print("  写入后字段:", sorted(a))
ck("旧行为确实丢 events", "events" not in a, "（bug 存在）")
ck("旧行为确实丢 first_run_at", "first_run_at" not in a)
ck("旧行为写入了 userIndex/loginTime",
   a.get("userIndex") == "u123" and "loginTime" in a)
os.path.exists(state) and ck("旧行为 state.json 仍在", True)

print("\n=== 场景 B：新行为（update_state 合并写）===")
b2, a2 = scenario("new")
print("  写入前字段:", sorted(b2))
print("  写入后字段:", sorted(a2))
ck("events 保留", a2.get("events") == b2.get("events"),
   "%d 条" % len(a2.get("events") or []))
ck("first_run_at 保留", a2.get("first_run_at") == b2.get("first_run_at"))
ck("userIndex/loginTime 写入", a2.get("userIndex") == "u123"
   and a2.get("loginTime") == "2026-09-01 13:00:00")
ck("last_error 被清除（drop 生效）", "last_error" not in a2)

print("\n=== 场景 C：全新部署判定（空文件夹提醒是否重现）===")
# first_deploy() 只认文件存在性，不认内容
cn.FIRST_DEPLOY = cn.first_deploy()
print("  state.json 存在=%s → first_deploy()=%s"
      % (os.path.exists(state), cn.first_deploy()))
ck("整写后首页横幅不会重现（first_deploy 只看文件存在）",
   cn.first_deploy() is False)
reset()
cn.FIRST_DEPLOY = cn.first_deploy()
ck("删掉 state.json 才视为全新部署", cn.first_deploy() is True)

print("\n===== 结论 =====")
print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
print("失败项:", FAIL or "无")
sys.exit(0 if not FAIL else 1)

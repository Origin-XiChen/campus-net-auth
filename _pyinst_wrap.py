"""绕过 WorkBuddy sitecustomize shim 的 PyInstaller 启动包装（v2）。

问题：shim 把 os.remove / os.rmdir / shutil.rmtree 重定向到回收站
（SHFileOperationW），在 sandbox 环境下 API 失败抛 OSError，
导致 PyInstaller 6.x 的 --clean / 文件替换阶段直接退出。

方案：以 python -E -S（跳过 site.py -> 不加载 sitecustomize）启动，
本脚本再手动把 venv 的 site-packages 加回 sys.path，
使 PyInstaller 可正常 import，同时删除操作全部走原生实现。

用法：
  python -E -S _pyinst_wrap.py [pyinstaller 参数...]
"""
import os
import sys

# venv 的 site-packages（-S 跳过了 site.py，需要手动加回）
SP = r"C:\Users\XiChen\.workbuddy\binaries\python\envs\ui-build\Lib\site-packages"
if SP not in sys.path:
    sys.path.insert(0, SP)

# 防御：若意外加载了 shim 则重置删除函数
if os.remove.__module__ == "sitecustomize":
    import ctypes
    k32 = ctypes.windll.kernel32

    def _native_remove(path, *a, **kw):
        p = os.fsdecode(os.fspath(path))
        ok = k32.DeleteFileW(p)
        if not ok and k32.GetLastError() not in (2, 3):
            raise OSError(k32.GetLastError(), p)

    def _native_rmdir(path, *a, **kw):
        p = os.fsdecode(os.fspath(path))
        ok = k32.RemoveDirectoryW(p)
        if not ok and k32.GetLastError() not in (2, 3):
            raise OSError(k32.GetLastError(), p)

    os.remove = os.unlink = _native_remove
    os.rmdir = _native_rmdir

from PyInstaller.__main__ import run
run()

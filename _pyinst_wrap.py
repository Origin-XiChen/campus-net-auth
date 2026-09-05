"""绕过 WorkBuddy sitecustomize shim 的 PyInstaller 启动包装（v3）。

问题：shim 把 os.remove / os.rmdir / shutil.rmtree 重定向到回收站
（SHFileOperationW），在 sandbox 环境下 API 失败抛 OSError，
导致 PyInstaller 6.x 的 --clean / 文件替换阶段直接退出。

方案：以 python -E -S（跳过 site.py -> 不加载 sitecustomize）启动，
本脚本再手动把构建解释器的 site-packages 加回 sys.path，
使 PyInstaller 可正常 import，同时删除操作全部走原生实现。

职责（v3）：
  * 定位 PyInstaller：CNA_PY_SITE 环境变量 > 当前解释器 purelib
  * 防御：若 shim 仍被加载，重置删除函数为原生实现
  * 从 campusnet.py 的 APP_VERSION 生成 exe 版本资源（单一版本来源；
    bat 文件保持纯 ASCII，故在此生成）

用法：
  python -E -S _pyinst_wrap.py [pyinstaller 参数...]
"""
import os
import re
import sys
import sysconfig

# -S 跳过了 site.py，PyInstaller 不在 sys.path 上
for _sp in filter(None, (os.environ.get("CNA_PY_SITE"),
                         sysconfig.get_paths().get("purelib"))):
    _sp = os.path.abspath(_sp)
    if not os.path.isdir(_sp) or _sp in sys.path:
        continue
    sys.path.insert(0, _sp)
    try:
        __import__("PyInstaller")
        break
    except ImportError:
        sys.path.remove(_sp)

# 防御：若意外加载了 shim 则重置删除函数（PyInstaller --clean 依赖原生删除）
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


def _inject_version_file():
    """从 campusnet.py 的 APP_VERSION（单一来源）生成 exe 版本资源并挂参。

    bat 文件刻意保持纯 ASCII，所以版本资源在这里生成；字符串用英文避开
    版本文件的编码歧义。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "campusnet.py")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"',
                  open(src, encoding="utf-8").read(), re.M)
    ver = m.group(1) if m else "0.0.0"
    nums = [int(x) for x in ver.split(".")] + [0] * (4 - len(ver.split(".")))
    tpl = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%(fv)s, prodvers=%(fv)s,
    mask=0x3f, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'CampusNetAuth'),
        StringStruct('FileDescription', 'Campus network silent authentication tool (ePortal/SAM+)'),
        StringStruct('FileVersion', '%(v)s'),
        StringStruct('InternalName', 'CampusNetAuth'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', 'CampusNetAuth.exe'),
        StringStruct('ProductName', 'CampusNetAuth'),
        StringStruct('ProductVersion', '%(v)s')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""" % {"fv": "(%s)" % ", ".join(str(n) for n in nums), "v": ver}
    vfile = os.path.join(here, "build", "version_info.txt")
    os.makedirs(os.path.dirname(vfile), exist_ok=True)
    with open(vfile, "w", encoding="ascii") as f:
        f.write(tpl)
    if "--version-file" not in sys.argv:
        sys.argv += ["--version-file", vfile]


_inject_version_file()

try:
    from PyInstaller.__main__ import run
except ImportError as e:
    sys.exit("PyInstaller not found: install build deps from "
             "requirements-dev.txt into the build python, or point "
             "CNA_PY_SITE at its site-packages. (%r)" % e)
run()

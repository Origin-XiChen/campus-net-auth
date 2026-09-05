#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CampusNetAuth 管理界面
=====================
仅使用 Python 标准库（tkinter），零第三方依赖，保持项目"零依赖"的初衷。

可设置：门户地址、运营商、账号、密码、开机自动认证、后台守护，
        以及各种检测 / 重试周期。

原 6 个 .bat 的功能已全部内置到本界面（状态查看、日志、自检、
一键体检、安装/卸载自启、抓包诊断、打开目录），
不再需要双击 .bat（双击 .bat 会闪 cmd 黑窗口）。

启动：
    python ui.py
或：
    python campusnet.py ui
"""

import os
import sys
import time
import math
import ctypes
import queue
import threading
import subprocess
try:
    import tkinter as tk
    from tkinter import font as tkfont
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "当前 Python 未包含 tkinter，无法启动管理界面。\n"
        "请改用带 tkinter 的解释器（python.org 官方安装包自带）后重试。\n")
    raise SystemExit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

try:
    import campusnet as cn  # 开发态/打包态均可解析
except ImportError:  # pragma: no cover
    # 极端打包情形：主脚本未被作为独立模块打入，则复用 __main__
    import __main__ as cn

# ============================ 主题 ============================
# 浅色、柔和的 Apple 风格配色

BG = "#F5F5F7"          # 窗口底色（Apple 系统灰）
SIDEBAR = "#EFEFF4"     # 侧栏
CARD = "#FFFFFF"        # 卡片
LINE = "#E6E6EB"        # 分隔线 / 描边
LINE_SOFT = "#EDEDF2"   # 更淡的描边
SHADOW = "#EDEDF1"      # 卡片柔和阴影
TEXT = "#1C1C1E"        # 主文字
SUB = "#8A8A8E"         # 次要文字
SUB2 = "#AEAEB2"        # 更次要（提示、单位）
BLUE = "#007AFF"        # 主色
BLUE_SOFT = "#E8F2FF"   # 主色浅底（选中态）
GREEN = "#34C759"
ORANGE = "#FF9500"
RED = "#FF3B30"
TRACK = "#E5E5EA"       # 开关关闭态轨道
TRACK_ON = GREEN
FIELD = "#FAFAFC"       # 输入区 / 日志区底色

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"

WIN_W, WIN_H = 1060, 700
SIDEBAR_W = 212
LOG_TAIL = 200          # 日志展示行数

# 自绘标题栏（无边框窗口用）
TITLE_H = 38            # 标题栏高度
TB_BG = "#E9E9EE"       # 标题栏底色（比窗口底色略深，形成层次）

RADIUS = 14             # 卡片圆角
RADIUS_SM = 10          # 小控件圆角


def F(size=13, weight="normal"):
    return (FONT_FAMILY, size, weight)


def FM(size=10, weight="normal"):
    return (MONO_FAMILY, size, weight)


# ============================ 绘图工具 ============================

def _arc_points(cx, cy, r, t0, t1, steps=8):
    """按数学角度（逆时针为正）采样圆弧点，返回 [(x, y), ...]"""
    pts = []
    for i in range(steps + 1):
        t = math.radians(t0 + (t1 - t0) * i / float(steps))
        pts.append((cx + r * math.cos(t), cy - r * math.sin(t)))
    return pts


def round_rect(canvas, x1, y1, x2, y2, r=12, **kw):
    """在 canvas 上画圆角矩形，返回 item id"""
    r = min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0)
    pts = []
    pts += _arc_points(x1 + r, y1 + r, r, 180, 90)   # 左上
    pts += _arc_points(x2 - r, y1 + r, r, 90, 0)     # 右上
    pts += _arc_points(x2 - r, y2 - r, r, 360, 270)  # 右下
    pts += _arc_points(x1 + r, y2 - r, r, 270, 180)  # 左下
    flat = [v for p in pts for v in p]
    return canvas.create_polygon(flat, smooth=False, **kw)


def _mix(c1, c2, t):
    """两个 #RRGGBB 颜色按 t(0..1) 插值，用于渐变与动效"""
    def rgb(c):
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    r1, g1, b1 = rgb(c1)
    r2, g2, b2 = rgb(c2)
    return "#%02X%02X%02X" % (int(r1 + (r2 - r1) * t),
                              int(g1 + (g2 - g1) * t),
                              int(b1 + (b2 - b1) * t))


# ============================ 基础控件 ============================

class Card(tk.Frame):
    """圆角 + 柔和阴影的白卡片（内容区自动留出内边距）"""

    PAD = 18

    def __init__(self, master, pad=18, radius=RADIUS, expand=False,
                 fill=None):
        tk.Frame.__init__(self, master, bg=BG)
        self.pad = pad
        self.radius = radius
        self.expand = expand
        self.fill = fill or CARD

        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0,
                            height=1)
        if expand:
            self.cv.pack(fill="both", expand=True)
        else:
            self.cv.pack(fill="x")

        self.body = tk.Frame(self.cv, bg=self.fill)
        self._win = self.cv.create_window(pad, pad, anchor="nw",
                                          window=self.body)
        self.cv.bind("<Configure>", self._redraw)
        self.body.bind("<Configure>", lambda e: self._redraw())

    def _redraw(self, event=None):
        w = max(self.cv.winfo_width(), 2)
        p = self.pad
        if self.expand:
            # expand 模式：父容器把空间给到 cv，body 高度同步 = cv - 2*pad，
            # 这样 body 内 fill="both" expand=True 的子控件才能拿到空间。
            h = max(self.cv.winfo_height(), 2)
            h_body = max(h - p * 2, 1)
            self.cv.itemconfig(self._win, width=max(w - p * 2, 1),
                               height=h_body)
        else:
            # 静态模式：高度由 body 内容决定，保底让 body 至少有一行高度
            req_h = self.body.winfo_reqheight()
            h = max(req_h + p * 2, p * 2 + 20)
            if abs(self.cv.winfo_height() - h) > 0.5:
                self.cv.configure(height=h)
            self.cv.itemconfig(self._win, width=max(w - p * 2, 1))
        self.cv.delete("bg")
        # 阴影：向外扩散两层淡灰，模拟柔和投影
        round_rect(self.cv, 2, 5, w - 2, h + 1, r=self.radius,
                   fill=SHADOW, outline="", tags="bg")
        round_rect(self.cv, 1, 3, w - 1, h - 1, r=self.radius,
                   fill=_mix(SHADOW, LINE, 0.35), outline="", tags="bg")
        # 主体
        round_rect(self.cv, 1, 1, w - 1, h - 2, r=self.radius,
                   fill=self.fill, outline=LINE_SOFT, tags="bg")
        self.cv.coords(self._win, p, p)

    def pack_content(self):
        return self.body


def make_card(parent, pad=18, expand=False):
    """工厂函数：返回 (card, body)，与旧版接口一致，便于布局代码复用"""
    c = Card(parent, pad=pad, expand=expand)
    return c, c.body


class RoundedEntry(tk.Frame):
    """圆角输入框：白色圆角底 + 聚焦时蓝色描边"""

    H = 36

    def __init__(self, master, var=None, show=None, width=None,
                 placeholder=""):
        tk.Frame.__init__(self, master, bg=CARD)
        self.focused = False
        self.show = show
        self.placeholder = placeholder

        self.cv = tk.Canvas(self, bg=CARD, highlightthickness=0, bd=0,
                            height=self.H)
        self.cv.pack(fill="x")
        self.entry = tk.Entry(self.cv, textvariable=var, relief="flat",
                              bd=0, bg="#FFFFFF", fg=TEXT,
                              insertbackground=BLUE, font=F(12),
                              show=show,
                              **({"width": width} if width else {}))
        self._win = self.cv.create_window(12, self.H // 2, anchor="w",
                                          window=self.entry)
        self.cv.bind("<Configure>", self._redraw)
        for w in (self.cv, self.entry):
            w.bind("<FocusIn>", lambda e: self._focus(True))
            w.bind("<FocusOut>", lambda e: self._focus(False))

    def _redraw(self, event=None):
        w = max(self.cv.winfo_width(), 2)
        h = self.H
        self.cv.delete("bg")
        outline = BLUE if self.focused else LINE
        ow = 2 if self.focused else 1
        round_rect(self.cv, 1, 1, w - 1, h - 1, r=RADIUS_SM,
                   fill="#FFFFFF", outline=outline, width=ow, tags="bg")
        self.cv.itemconfig(self._win, width=max(w - 24, 1))

    def _focus(self, on):
        self.focused = on
        self._redraw()

    def configure(self, **kw):
        if "show" in kw:
            self.entry.configure(show=kw.pop("show"))
        if kw:
            self.entry.configure(**kw)


class Spinner(tk.Canvas):
    """等待指示器：旋转的圆环"""

    def __init__(self, master, size=16, color=BLUE):
        tk.Canvas.__init__(self, master, width=size, height=size,
                           highlightthickness=0, bg=master.cget("bg"))
        self.size = size
        self.angle = 0
        self.job = None
        self.arc = self.create_arc(2, 2, size - 2, size - 2, start=0,
                                   extent=300, style=tk.ARC,
                                   outline=color, width=2)

    def start(self):
        self.stop()
        self._tick()

    def _tick(self):
        self.angle = (self.angle + 30) % 360
        self.itemconfig(self.arc, start=self.angle)
        self.job = self.after(80, self._tick)

    def stop(self):
        if self.job:
            self.after_cancel(self.job)
            self.job = None


class BreathingDot(tk.Canvas):
    """状态指示灯：中心圆点 + 向外呼吸的光晕"""

    def __init__(self, master, size=12, color=SUB):
        tk.Canvas.__init__(self, master, width=size * 3, height=size * 3,
                           highlightthickness=0, bg=master.cget("bg"))
        self.size = size
        self.color = color
        self.job = None
        self.phase = 0
        self._draw()

    def _draw(self):
        self.delete("all")
        s, c = self.size * 1.5, self.color
        # 光晕：随相位向外扩散并变淡
        for i in range(2):
            t = ((self.phase + i * 0.5) % 1.0)
            r = s * 0.45 + s * 0.55 * t
            col = _mix(_mix(c, "#FFFFFF", 0.35), "#FFFFFF", t)
            self.create_oval(s - r, s - r, s + r, s + r, fill=col,
                             outline="")
        # 中心点
        r0 = s * 0.42
        self.create_oval(s - r0, s - r0, s + r0, s + r0, fill=c,
                         outline="#FFFFFF", width=1.5)

    def set_color(self, color):
        self.color = color
        self._draw()

    def start(self):
        self.stop()
        self._tick()

    def _tick(self):
        self.phase = (self.phase + 0.06) % 1.0
        self._draw()
        self.job = self.after(48, self._tick)

    def stop(self):
        if self.job:
            self.after_cancel(self.job)
            self.job = None


class Switch(tk.Canvas):
    """iOS 风格开关"""

    W, H = 50, 30

    def __init__(self, master, value=False, command=None):
        tk.Canvas.__init__(self, master, width=self.W, height=self.H,
                           highlightthickness=0, bg=master.cget("bg"))
        self.value = bool(value)
        self.command = command
        self.config(cursor="hand2")
        self.bind("<Button-1>", self._on_click)
        self.draw()

    def draw(self):
        self.delete("all")
        fill = TRACK_ON if self.value else TRACK
        round_rect(self, 0, 0, self.W, self.H, r=self.H / 2.0,
                   fill=fill, outline="")
        kx = self.W - self.H / 2.0 - 3 if self.value else self.H / 2.0 + 3
        # 滑块带一点阴影，更立体
        self.create_oval(kx - 12, 3, kx + 12, self.H - 3,
                         fill=_mix(SHADOW, "#FFFFFF", 0.25), outline="")
        self.create_oval(kx - 11, 4, kx + 11, self.H - 4,
                         fill="#FFFFFF", outline="")

    def _on_click(self, event=None):
        self.value = not self.value
        self.draw()
        if self.command:
            self.command(self.value)

    def set(self, value):
        self.value = bool(value)
        self.draw()


class Segmented(tk.Canvas):
    """分段控件（选运营商，不用下拉框，完整显示所有选项）"""

    H = 36
    PAD = 3

    def __init__(self, master, options, value=None, command=None, seg_w=78):
        self.options = list(options)      # [(label, value), ...]
        self.seg_w = seg_w
        self.command = command
        width = seg_w * len(self.options) + self.PAD * 2
        tk.Canvas.__init__(self, master, width=width, height=self.H,
                           highlightthickness=0, bg=master.cget("bg"))
        self.value = value if value is not None else self.options[0][1]
        self.config(cursor="hand2")
        self.bind("<Button-1>", self._on_click)
        self.draw()

    def draw(self):
        self.delete("all")
        n = len(self.options)
        w = self.seg_w * n + self.PAD * 2
        round_rect(self, 0, 0, w, self.H, r=self.H / 2.0,
                   fill="#EDEDF2", outline="")
        for i, (label, val) in enumerate(self.options):
            x = self.PAD + i * self.seg_w
            on = (val == self.value)
            if on:
                # 选中态：白色滑块 + 淡投影
                round_rect(self, x + 1, self.PAD + 1, x + self.seg_w,
                           self.H - self.PAD + 1,
                           r=(self.H - self.PAD * 2) / 2.0,
                           fill=SHADOW, outline="")
                round_rect(self, x, self.PAD, x + self.seg_w,
                           self.H - self.PAD,
                           r=(self.H - self.PAD * 2) / 2.0,
                           fill="#FFFFFF", outline=LINE_SOFT)
            self.create_text(
                x + self.seg_w / 2.0, self.H / 2.0, text=label,
                fill=TEXT if on else SUB,
                font=F(11, "bold" if on else "normal"))

    def _on_click(self, event):
        i = int((event.x - self.PAD) // self.seg_w)
        if 0 <= i < len(self.options):
            val = self.options[i][1]
            if val != self.value:
                self.value = val
                self.draw()
                if self.command:
                    self.command(val)

    def set(self, value):
        self.value = value
        self.draw()


class PillButton(tk.Canvas):
    """胶囊按钮：宽度自适应文字，鼠标手型，按下有回弹动效"""

    H = 34

    STYLES = {
        "primary": (BLUE, "#FFFFFF", "#0A84FF", "#0060DF"),
        "ghost": ("#FFFFFF", TEXT, "#F2F2F7", "#E5E5EA"),
        "danger": ("#FDECEA", RED, "#FBD8D4", "#F8C6C0"),
    }

    def __init__(self, master, text="", command=None, kind="primary"):
        self.text = text
        self.command = command
        self.kind = kind
        self.enabled = True
        self._pressed = False
        fill, fg, hover, press = self.STYLES.get(kind, self.STYLES["primary"])
        self.fill, self.fg, self.hover, self.press = fill, fg, hover, press

        probe = tkfont.Font(family=FONT_FAMILY, size=12, weight="bold")
        w = int(probe.measure(text)) + 36
        tk.Canvas.__init__(self, master, width=w, height=self.H,
                           highlightthickness=0, bg=master.cget("bg"))
        self.config(cursor="hand2")
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda e: self._draw(True))
        self.bind("<Leave>", lambda e: self._draw(False))
        self.draw()

    def draw(self, hover=False):
        self._draw(hover)

    def _draw(self, hover=False, pressed=False):
        self.delete("all")
        w = int(self.cget("width"))
        h = self.H
        if not self.enabled:
            fill, fg = "#F2F2F7", "#C7C7CC"
        else:
            fill = self.press if pressed else (self.hover if hover
                                               else self.fill)
            fg = self.fg
        dy = 1 if pressed else 0
        if self.enabled and self.kind in ("primary", "ghost"):
            # 轻微投影，增加层次
            round_rect(self, 1, 2 + dy, w - 1, h + dy, r=h / 2.0,
                       fill=SHADOW, outline="")
        round_rect(self, 1, dy, w - 1, h - 2 + dy, r=(h - 2) / 2.0,
                   fill=fill, outline="")
        self.create_text(w / 2.0, (h - 2) / 2.0 + dy, text=self.text,
                         fill=fg, font=F(12, "bold"))

    def _on_press(self, event=None):
        if not self.enabled:
            return
        self._pressed = True
        self._draw(True, True)

    def _on_release(self, event=None):
        if not self.enabled or not self._pressed:
            return
        self._pressed = False
        self._draw(True, False)
        if self.command:
            self.command()

    def set_enabled(self, on):
        self.enabled = bool(on)
        self.config(cursor="hand2" if on else "")
        self._draw(False)


# ============================ 滚动区 ============================

class ScrollArea(tk.Frame):
    """内容滚动区（Canvas + 自绘细滚动条）。

    Tk 的滚轮事件只派发给光标下的控件、不会自动冒泡到 Canvas，
    所以必须在页面构建完成后调用 bind_children() 递归挂接。
    """

    def __init__(self, master, bg=BG):
        tk.Frame.__init__(self, master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.bar = tk.Canvas(self, width=8, bg=bg, highlightthickness=0,
                             bd=0, cursor="hand2")
        self.bar.place(relx=1.0, x=-8, rely=0, relheight=1.0, width=8)
        self._thumb = None
        self._dragging = False
        self._hover = False

        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.inner.bind("<Configure>", self._on_inner_cfg)
        self.canvas.bind("<Configure>", self._on_canvas_cfg)
        self.bar.bind("<Button-1>", self._bar_press)
        self.bar.bind("<B1-Motion>", self._bar_drag)
        self.bar.bind("<ButtonRelease-1>", self._bar_release)
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.bar)

    # ---- 布局同步 ----
    def _on_inner_cfg(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.after_idle(self._sync)

    def _on_canvas_cfg(self, event):
        # inner 高度 = max(canvas 可视高, 子内容需求高)：
        # 不足时撑满让 expand 卡片拿到空间；超出时让滚动条出现。
        self.canvas.itemconfigure(self._win, width=event.width)
        self.inner.update_idletasks()
        req_h = self.inner.winfo_reqheight()
        target_h = max(event.height, req_h)
        self.canvas.itemconfigure(self._win, height=target_h)
        self.after_idle(self._sync)

    def _on_scroll(self, first, last):
        self.after_idle(self._sync)

    def reset(self):
        """切换页面时回到顶部"""
        self.canvas.yview_moveto(0.0)
        self.after_idle(self._sync)

    # ---- 滚轮 ----
    def _wheel(self, event):
        try:
            delta = int(getattr(event, "delta", 0))
        except Exception:
            delta = 0
        if delta:
            self.canvas.yview_scroll(int(-1 * (delta / 120.0)) or
                                     (-1 if delta > 0 else 1), "units")
        elif getattr(event, "num", 0) == 4:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", 0) == 5:
            self.canvas.yview_scroll(1, "units")
        self.after_idle(self._sync)
        return "break"

    def _bind_wheel(self, w):
        try:
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-4>", self._wheel)
            w.bind("<Button-5>", self._wheel)
        except Exception:
            pass

    def bind_children(self):
        """页面构建完成后调用：递归挂接滚轮事件到所有层级子控件。

        跳过 Text —— 日志区有自己的滚动条，滚它时应滚动日志本身。
        """
        stack = [self.inner]
        while stack:
            w = stack.pop()
            try:
                cls = w.winfo_class()
                children = w.winfo_children()
            except Exception:
                continue
            if cls not in ("Text", "Entry", "Spinbox", "Listbox"):
                self._bind_wheel(w)
            stack.extend(children)

    # ---- 自绘滚动条 ----
    def _bar_press(self, e):
        self._dragging = True
        self._bar_drag(e)

    def _bar_drag(self, e):
        if not self._dragging:
            return
        h = self.bar.winfo_height()
        first, last = self.canvas.yview()
        span = max(last - first, 0.0001)
        th = max(int(h * span), 28)
        if h <= th:
            return
        y = min(max(e.y - th / 2.0, 0.0), float(h - th))
        self.canvas.yview_moveto(y / float(h - th))
        self.after_idle(self._sync)

    def _bar_release(self, e):
        self._dragging = False

    def _sync(self):
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            if self._thumb is not None:
                self.bar.delete(self._thumb)
                self._thumb = None
            return
        try:
            h = self.bar.winfo_height()
            w = self.bar.winfo_width()
        except Exception:
            return
        if h <= 8 or w <= 0:
            return
        span = last - first
        th = max(int(h * span), 28)
        y0 = int(first * (h - th))
        y1 = y0 + th
        r = (w - 2) / 2.0
        cx = r + 1
        pts = (_arc_points(cx, y0 + r, r, 180, 0) +
               _arc_points(cx, y1 - r, r, 360, 180))
        flat = [v for p in pts for v in p]
        if self._thumb is None:
            self._thumb = self.bar.create_polygon(flat, fill="#C7C7CC",
                                                  outline="")
        else:
            self.bar.coords(self._thumb, *flat)


class ThinScrollbar(tk.Canvas):
    """自绘细滚动条，替代系统原生 Scrollbar（与整体风格统一）。

    提供 set(first, last) 以兼容 yscrollcommand=sb.set 的回调约定。
    """

    def __init__(self, master, command=None, width=8, bg=FIELD):
        tk.Canvas.__init__(self, master, width=width, bg=bg,
                           highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self._first, self._last = 0.0, 1.0
        self._thumb = None
        self._drag = False
        self.bind("<Configure>", lambda e: self.after_idle(self._draw))
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)

    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        self.after_idle(self._draw)

    def _press(self, e):
        self._drag = True
        self._motion(e)

    def _motion(self, e):
        if not self._drag or not self.command:
            return
        h = self.winfo_height()
        span = max(self._last - self._first, 0.0001)
        th = max(int(h * span), 24)
        if h <= th:
            return
        y = min(max(e.y - th / 2.0, 0.0), float(h - th))
        self.command("moveto", str(y / float(h - th)))

    def _release(self, e):
        self._drag = False

    def _draw(self):
        h = self.winfo_height()
        w = self.winfo_width()
        if h <= 8 or w <= 0:
            return
        first, last = self._first, self._last
        if first <= 0.0 and last >= 1.0:
            if self._thumb is not None:
                self.delete(self._thumb)
                self._thumb = None
            return
        span = last - first
        th = max(int(h * span), 24)
        y0 = int(first * (h - th))
        y1 = y0 + th
        r = (w - 2) / 2.0
        cx = r + 1
        pts = (_arc_points(cx, y0 + r, r, 180, 0) +
               _arc_points(cx, y1 - r, r, 360, 180))
        flat = [v for p in pts for v in p]
        if self._thumb is None:
            self._thumb = self.create_polygon(flat, fill="#C7C7CC",
                                              outline="")
        else:
            self.coords(self._thumb, *flat)


# ============================ 侧栏图标 ============================

def _icon_wifi(cv, x, y, color, s=15):
    """信号塔：三段弧 + 圆点（连接与账号）"""
    cv.create_arc(x - s, y - s * 0.2, x + s, y + s * 1.4, start=40,
                  extent=100, style=tk.ARC, outline=color, width=1.8)
    cv.create_arc(x - s * 0.6, y + s * 0.15, x + s * 0.6, y + s * 1.5,
                  start=30, extent=120, style=tk.ARC, outline=color,
                  width=1.8)
    cv.create_oval(x - s * 0.16, y + s * 0.72, x + s * 0.16, y + s * 1.2,
                   fill=color, outline="")


def _icon_gear(cv, x, y, color, s=8):
    """齿轮（自动化）"""
    cv.create_oval(x - s, y - s, x + s, y + s, outline=color, width=1.8)
    cv.create_oval(x - s * 0.38, y - s * 0.38, x + s * 0.38, y + s * 0.38,
                   outline=color, width=1.8)
    for i in range(6):
        a = math.radians(60 * i)
        cv.create_line(x + math.cos(a) * s * 0.72, y - math.sin(a) * s * 0.72,
                       x + math.cos(a) * s * 1.28, y - math.sin(a) * s * 1.28,
                       fill=color, width=1.8)


def _icon_pulse(cv, x, y, color, s=9):
    """心跳折线（状态与日志）"""
    pts = [(-1.0, 0), (-0.5, 0), (-0.25, -0.85), (0.05, 0.9),
           (0.32, -0.5), (0.58, 0), (1.0, 0)]
    cv.create_line([(x + px * s, y - py * s * 0.75) for px, py in pts],
                   fill=color, width=1.8, smooth=True)


ICONS = {"conn": _icon_wifi, "auto": _icon_gear, "status": _icon_pulse}


# ============================ 气泡通知 / 确认框 ============================

class Toast(tk.Frame):
    """
    自制气泡通知（不用系统通知 / messagebox）。
    多级 UX：先显示标题预览 -> 点击展开详情 -> 关闭；右侧滑入动效。
    """

    COLORS = {"ok": GREEN, "error": RED, "warn": ORANGE, "info": BLUE}

    def __init__(self, master):
        tk.Frame.__init__(self, master, bg=CARD, bd=0,
                          highlightthickness=1, highlightbackground=LINE)
        self.expanded = False
        self.detail = ""
        self.job = None
        self._slide_job = None
        self._offset = 0

        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x", padx=14, pady=(11, 11))
        self.dot = tk.Canvas(head, width=9, height=9, bg=CARD,
                             highlightthickness=0)
        self.dot.pack(side="left")
        self.title_var = tk.StringVar()
        tk.Label(head, textvariable=self.title_var, bg=CARD, fg=TEXT,
                 font=F(12, "bold")).pack(side="left", padx=(9, 0))
        self.close_lbl = tk.Label(head, text="✕", bg=CARD, fg=SUB2,
                                  font=F(11), cursor="hand2")
        self.close_lbl.pack(side="right")

        self.body = tk.Frame(self, bg=CARD)
        self.detail_var = tk.StringVar()
        self.detail_lbl = tk.Label(self.body, textvariable=self.detail_var,
                                   bg=CARD, fg=SUB, font=F(10),
                                   justify="left", wraplength=380, anchor="w")
        self.detail_lbl.pack(fill="x", padx=(32, 14), pady=(0, 12))

        for wgt in (self, head, self.dot):
            wgt.bind("<Button-1>", self._on_click)
        self.close_lbl.bind("<Button-1>", self._close)

    def _on_click(self, event=None):
        if self.detail:
            self.expanded = not self.expanded
            if self.expanded:
                self.body.pack(fill="x")
            else:
                self.body.pack_forget()

    def _close(self, event=None):
        self.hide()

    def show(self, title, detail="", kind="ok", ms=4200):
        color = self.COLORS.get(kind, BLUE)
        self.dot.delete("all")
        self.dot.create_oval(0, 0, 9, 9, fill=color, outline="")
        self.title_var.set(title)
        self.detail = detail or ""
        self.expanded = False
        self.body.pack_forget()
        self.detail_var.set(self.detail)
        self.lift()
        if self.job:
            self.after_cancel(self.job)
        # 从右侧滑入
        self._offset = 46
        self._slide()
        self.job = self.after(ms, self.hide)

    def _slide(self):
        self._offset = max(0, self._offset - 9)
        self.place(relx=1.0, rely=0.0, x=-24 + self._offset, y=16,
                   anchor="ne")
        if self._offset > 0:
            self._slide_job = self.after(16, self._slide)

    def hide(self):
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        if self._slide_job:
            self.after_cancel(self._slide_job)
            self._slide_job = None
        self.place_forget()


class ConfirmDialog(tk.Toplevel):
    """自制确认对话框（不用原生弹窗）"""

    def __init__(self, master, title, message, ok_text="确定",
                 kind="primary"):
        tk.Toplevel.__init__(self, master)
        self.result = False
        self.overrideredirect(True)
        self.configure(bg=CARD, highlightthickness=1,
                       highlightbackground=LINE)
        self.transient(master)
        self.withdraw()

        box = tk.Frame(self, bg=CARD)
        box.pack(fill="both", expand=True, padx=24, pady=22)
        tk.Label(box, text=title, bg=CARD, fg=TEXT,
                 font=F(15, "bold")).pack(anchor="w")
        tk.Label(box, text=message, bg=CARD, fg=SUB, font=F(11),
                 justify="left", wraplength=390,
                 anchor="w").pack(anchor="w", pady=(10, 0))

        bar = tk.Frame(box, bg=CARD)
        bar.pack(anchor="e", pady=(22, 0))
        PillButton(bar, text="取消", kind="ghost",
                   command=self._no).pack(side="right", padx=(10, 0))
        PillButton(bar, text=ok_text, kind=kind,
                   command=self._yes).pack(side="right")

        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        mx, my = master.winfo_rootx(), master.winfo_rooty()
        mw, mh = master.winfo_width(), master.winfo_height()
        self.geometry("%dx%d+%d+%d" % (w, h, mx + (mw - w) // 2,
                                       my + (mh - h) // 2))
        self.deiconify()
        self.grab_set()
        self.focus_set()
        self.wait_window()

    def _yes(self):
        self.result = True
        self.destroy()

    def _no(self):
        self.result = False
        self.destroy()


class ResultDialog(tk.Toplevel):
    """自制结果查看对话框（自检输出等长文本），非模态"""

    def __init__(self, master, title, text):
        tk.Toplevel.__init__(self, master)
        self.configure(bg=CARD, highlightthickness=1,
                       highlightbackground=LINE)
        self.transient(master)
        self.title(title)
        self.geometry("620x420")
        self.withdraw()

        box = tk.Frame(self, bg=CARD)
        box.pack(fill="both", expand=True, padx=20, pady=18)
        tk.Label(box, text=title, bg=CARD, fg=TEXT,
                 font=F(14, "bold")).pack(anchor="w", pady=(0, 12))

        wrap = tk.Frame(box, bg=FIELD, highlightthickness=1,
                        highlightbackground=LINE_SOFT)
        wrap.pack(fill="both", expand=True)
        txt = tk.Text(wrap, bg=FIELD, fg=TEXT, relief="flat", bd=0,
                      font=FM(10), wrap="word", spacing1=3, spacing3=3,
                      padx=8, pady=6)
        sb = tk.Scrollbar(wrap, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", text or "（无输出）")
        txt.configure(state="disabled")

        bar = tk.Frame(box, bg=CARD)
        bar.pack(anchor="e", pady=(14, 0))
        PillButton(bar, text="关闭", command=self.destroy).pack(side="right")

        self.update_idletasks()
        mx, my = master.winfo_rootx(), master.winfo_rooty()
        mw, mh = master.winfo_width(), master.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry("%dx%d+%d+%d" % (max(w, 520), max(h, 340),
                                       mx + (mw - w) // 2,
                                       my + (mh - h) // 2))
        self.deiconify()


# ============================ 主界面 ============================

class App(tk.Tk):

    SERVICES = [("校园网", "default"), ("电信", "DX"), ("移动", "YD"),
                ("联通", "LT"), ("自动", "auto")]

    def __init__(self):
        tk.Tk.__init__(self)
        # 关键：先隐藏窗口再配置。否则 Tk 会先把默认窗口显示出来，
        # 之后才套无边框 / 设置尺寸 / 构建内容 —— 用户会看到一个空白
        # 矩形一闪而过（就是所谓的"黑框闪动"）。
        self.withdraw()
        self.overrideredirect(True)  # 无边框：标题栏/窗口按钮全部自绘
        self.title("CampusNetAuth · 校园网无感认证")
        self.geometry("%dx%d" % (WIN_W, WIN_H))
        self.minsize(1000, 640)
        self.configure(bg=BG)
        self._center()

        self._maxed = False          # 是否处于最大化
        self._saved_geo = ""         # 最大化前的窗口几何
        self.cfg = cn.load_config()
        self.busy = False
        self.pages = {}
        # 线程安全的结果队列：工作线程只入队，UI 更新一律在主线程完成
        self._q = queue.Queue()

        self._build_titlebar()
        self._build_sidebar()
        self._build_main()

        self.toast = Toast(self.content)
        self._poll_results()
        self.refresh_all()
        self.select_page("conn")

        # 全部绘制完成后再显示（淡入），彻底消除启动闪动
        self._fade_in()
        self.lift()
        self.after(80, self.focus_force)

    def _center(self):
        self.update_idletasks()
        x, y, w, h = self._work_area()
        px = x + max((w - WIN_W) // 2, 0)
        py = y + max((h - WIN_H) // 2, 0)
        self.geometry("%dx%d+%d+%d" % (WIN_W, WIN_H, px, py))

    def _fade_in(self):
        """窗口淡入（避免 deiconify 瞬间的生硬出现）"""
        try:
            self.attributes("-alpha", 0.0)
            self.deiconify()
            self._alpha = 0.0
            self._fade_step()
        except Exception:
            self.deiconify()

    def _fade_step(self):
        self._alpha = min(1.0, self._alpha + 0.12)
        try:
            self.attributes("-alpha", self._alpha)
        except Exception:
            return
        if self._alpha < 1.0:
            self.after(16, self._fade_step)
        else:
            self.attributes("-alpha", 1.0)

    # ---------- 自绘标题栏 ----------
    def _walk(self, root, skip=None):
        """递归遍历控件树（用于给标题栏所有子控件统一绑定拖拽）"""
        yield root
        for w in root.winfo_children():
            if skip and w in skip:
                continue
            yield from self._walk(w)

    def _build_titlebar(self):
        bar = tk.Frame(self, bg=TB_BG, height=TITLE_H)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        tk.Frame(self, bg=LINE, height=1).pack(side="top", fill="x")

        brand = tk.Label(bar, text="CampusNetAuth", bg=TB_BG, fg=TEXT,
                         font=F(11, "bold"))
        brand.pack(side="left", padx=(16, 0))
        tk.Label(bar, text="·", bg=TB_BG, fg=SUB2,
                 font=F(11)).pack(side="left", padx=(6, 0))
        tk.Label(bar, text="校园网无感认证", bg=TB_BG, fg=SUB,
                 font=F(10)).pack(side="left", padx=(6, 0))
        tk.Label(bar, text="    双击标题栏可最大化", bg=TB_BG, fg=SUB2,
                 font=F(8)).pack(side="left", padx=(10, 0))

        btnbox = tk.Frame(bar, bg=TB_BG)
        btnbox.pack(side="right", padx=12)
        self._make_tb_btn(btnbox, "—", self._minimize, (ORANGE, "#B25E00"))
        self._make_tb_btn(btnbox, "□", self._toggle_max, (GREEN, "#1E7B32"))
        self._make_tb_btn(btnbox, "✕", self.destroy, (RED, "#B00E0E"))

        # 拖拽：标题栏整条区域（窗口按钮除外，它们有自己的点击事件）
        for w in self._walk(bar, skip={btnbox}):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
        bar.bind("<Double-Button-1>", lambda e: self._toggle_max())

    def _make_tb_btn(self, parent, glyph, cmd, colors):
        """Apple 交通灯风格窗口按钮：圆点 + 悬停时显示符号"""
        color, hover = colors
        size = 14
        c = tk.Canvas(parent, width=size + 10, height=size + 10, bg=TB_BG,
                      highlightthickness=0, cursor="hand2")
        c.pack(side="left", padx=3)

        def draw(hov=False):
            c.delete("all")
            c.create_oval(2, 2, size + 8, size + 8,
                          fill=hover if hov else color, outline="")
            if hov:
                c.create_text(size / 2 + 5, size / 2 + 5, text=glyph,
                              fill="#FFFFFF", font=F(7, "bold"))

        draw()
        c.bind("<Enter>", lambda e: draw(True))
        c.bind("<Leave>", lambda e: draw(False))
        c.bind("<Button-1>", lambda e: cmd())

    # ---------- 窗口控制 ----------
    def _start_drag(self, e):
        self._drag_x, self._drag_y = e.x_root, e.y_root
        self._win_x, self._win_y = self.winfo_x(), self.winfo_y()

    def _on_drag(self, e):
        if self._maxed:
            return
        dx = e.x_root - self._drag_x
        dy = e.y_root - self._drag_y
        self.geometry("+%d+%d" % (self._win_x + dx, self._win_y + dy))

    def _minimize(self):
        # 无边框窗口不能直接 iconify（任务栏恢复后边框会错乱）。
        # 标准做法：先临时恢复原生边框 -> 最小化 -> 轮询到恢复后再套回无边框。
        self.overrideredirect(False)
        self.update_idletasks()
        self.iconify()
        self._poll_reframe()

    def _poll_reframe(self):
        if self.state() == "iconic":
            self.after(120, self._poll_reframe)
        else:
            self.overrideredirect(True)

    def _toggle_max(self):
        if self._maxed:
            self.geometry(self._saved_geo)
            self._maxed = False
            return
        self._saved_geo = self.geometry()
        x, y, w, h = self._work_area()
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._maxed = True

    @staticmethod
    def _work_area():
        """屏幕工作区（排除任务栏），SPI_GETWORKAREA = 0x0030"""
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(r), 0)
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    # ---------- 侧栏 ----------
    def _build_sidebar(self):
        side = tk.Frame(self, bg=SIDEBAR, width=SIDEBAR_W)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(side, bg=LINE, width=1).pack(side="right", fill="y")

        head = tk.Frame(side, bg=SIDEBAR)
        head.pack(fill="x", padx=20, pady=(26, 24))
        tk.Label(head, text="CampusNet", bg=SIDEBAR, fg=TEXT,
                 font=F(17, "bold")).pack(anchor="w")
        tk.Label(head, text="校园网无感认证", bg=SIDEBAR, fg=SUB,
                 font=F(10)).pack(anchor="w", pady=(4, 0))

        self.tab_btns = {}
        for key, label in (("conn", "连接与账号"),
                           ("auto", "自动化"),
                           ("status", "状态与日志")):
            btn = tk.Frame(side, bg=SIDEBAR, cursor="hand2")
            btn.pack(fill="x", padx=10, pady=2)
            pill = tk.Canvas(btn, height=40, bg=SIDEBAR,
                             highlightthickness=0)
            pill.pack(fill="x")
            for w in (btn, pill):
                w.bind("<Button-1>", lambda e, k=key: self.select_page(k))
                w.bind("<Enter>", lambda e, k=key: self._hover_tab(k, True))
                w.bind("<Leave>", lambda e, k=key: self._hover_tab(k, False))
            self.tab_btns[key] = (btn, pill, label)
            self._draw_tab(key)

        tip = tk.Frame(side, bg=SIDEBAR)
        tip.pack(side="bottom", fill="x", padx=20, pady=18)
        tk.Frame(tip, bg=LINE_SOFT, height=1).pack(fill="x", pady=(0, 12))
        tk.Label(tip,
                 text="密码使用 Windows DPAPI 加密，仅当前 Windows 用户可解密",
                 bg=SIDEBAR, fg=SUB, font=F(9), justify="left",
                 anchor="w", wraplength=SIDEBAR_W - 40).pack(anchor="w")

    def _draw_tab(self, key, hover=False):
        btn, pill, label = self.tab_btns[key]
        on = (getattr(self, "_cur_page", None) == key)
        w = SIDEBAR_W - 20
        pill.delete("all")
        if on:
            round_rect(pill, 1, 2, w - 1, 40, r=11, fill=SHADOW, outline="")
            round_rect(pill, 1, 1, w - 1, 38, r=11, fill="#FFFFFF",
                       outline=LINE_SOFT)
        elif hover:
            round_rect(pill, 1, 1, w - 1, 38, r=11, fill=_mix(SIDEBAR,
                                                             "#FFFFFF",
                                                             0.55),
                       outline="")
        color = BLUE if on else (TEXT if hover else SUB)
        ICONS[key](pill, 26, 20, color)
        pill.create_text(46, 20, text=label, anchor="w", fill=color,
                         font=F(12, "bold" if on else "normal"))

    def _hover_tab(self, key, on):
        if getattr(self, "_cur_page", None) != key:
            self._draw_tab(key, on)

    # ---------- 主区 ----------
    def _build_main(self):
        self.main = tk.Frame(self, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)

        # 顶部：薄状态条（只放状态徽章，不再放大标题——大标题归各页自己负责，
        # 避免在自动化/状态页出现"校园网自动认证"这种冗余标题）
        top = tk.Frame(self.main, bg=BG, height=48)
        top.pack(fill="x", padx=24, pady=(18, 0))
        top.pack_propagate(False)

        self.subtitle = tk.StringVar(value="正在读取状态…")
        # 左侧：副标题（小号、灰色，留给各页扩展显示）
        tk.Label(top, textvariable=self.subtitle, bg=BG, fg=SUB,
                 font=F(10)).pack(side="left", pady=14)

        # 右侧：状态徽章（圆点 + 文字 + spinner）
        right = tk.Frame(top, bg=BG)
        right.pack(side="right", pady=10)
        self.spinner = Spinner(right, size=16)
        self.spinner.pack(side="right", padx=(10, 0))
        self.state_var = tk.StringVar(value="—")
        self.state_lbl = tk.Label(right, textvariable=self.state_var,
                                  bg=BG, fg=TEXT, font=F(12, "bold"))
        self.state_lbl.pack(side="right")
        self.state_dot = BreathingDot(right, size=8, color=SUB)
        self.state_dot.pack(side="right", padx=(0, 8))
        self.state_dot.start()
        self.spinner.pack_forget()

        # 内容区：外层负责留白，内层是可滚动挂载点。
        # self.content 指向滚动区的 inner，这样三个 _build_page_* 无需改动。
        content_wrap = tk.Frame(self.main, bg=BG)
        content_wrap.pack(fill="both", expand=True, padx=24, pady=(16, 20))
        self.scroll = ScrollArea(content_wrap)
        self.scroll.pack(fill="both", expand=True)
        self.content = self.scroll.inner

        self._build_page_conn()
        self._build_page_auto()
        self._build_page_status()
        # 页面构建完成后，把滚轮事件递归挂到所有子控件上
        self.scroll.bind_children()

    # ---------- 页：连接与账号 ----------
    def _build_page_conn(self):
        page = tk.Frame(self.content, bg=BG)
        self.pages["conn"] = page

        # 页面标题（独立于顶部状态条——让 conn 页有自己的"门户人格"）
        tk.Label(page, text="校园网自动认证", bg=BG, fg=TEXT,
                 font=F(20, "bold")).pack(anchor="w", padx=2, pady=(0, 14))

        card, inner = make_card(page)
        card.pack(fill="x")

        tk.Label(inner, text="认证信息", bg=CARD, fg=TEXT,
                 font=F(15, "bold")).pack(anchor="w", pady=(0, 4))
        tk.Label(inner, text="修改后点击「保存并连接」立即生效并尝试认证",
                 bg=CARD, fg=SUB, font=F(10)).pack(anchor="w", pady=(0, 18))

        # 门户地址
        tk.Label(inner, text="门户地址", bg=CARD, fg=SUB,
                 font=F(10)).pack(anchor="w")
        self.var_host = tk.StringVar()
        self._entry(inner, self.var_host).pack(fill="x", pady=(6, 16))

        # 运营商
        tk.Label(inner, text="运营商", bg=CARD, fg=SUB,
                 font=F(10)).pack(anchor="w")
        self.seg_service = Segmented(inner, self.SERVICES,
                                     value=self.cfg.get("service", "default"))
        self.seg_service.pack(anchor="w", pady=(8, 16))

        # 账号
        tk.Label(inner, text="账号（学号）", bg=CARD, fg=SUB,
                 font=F(10)).pack(anchor="w")
        self.var_user = tk.StringVar(value=self.cfg.get("username", ""))
        self._entry(inner, self.var_user).pack(fill="x", pady=(6, 16))

        # 密码
        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text="密码", bg=CARD, fg=SUB,
                 font=F(10)).pack(side="left")
        self.show_pwd = tk.BooleanVar(value=False)
        self.toggle_pwd_lbl = tk.Label(row, text="显示", bg=CARD, fg=BLUE,
                                       font=F(10), cursor="hand2")
        self.toggle_pwd_lbl.pack(side="right")

        self.var_pass = tk.StringVar()
        self.pwd_entry = self._entry(inner, self.var_pass, show="•")
        self.pwd_entry.pack(fill="x", pady=(6, 8))
        self.toggle_pwd_lbl.bind("<Button-1>", self._toggle_pwd)

        cred_exists = os.path.exists(cn.CRED_PATH)
        self.pwd_hint = tk.StringVar(value=(
            "已保存密码，留空表示不修改" if cred_exists
            else "尚未保存密码"))
        tk.Label(inner, textvariable=self.pwd_hint, bg=CARD, fg=SUB2,
                 font=F(9)).pack(anchor="w", pady=(0, 18))

        bar = tk.Frame(inner, bg=CARD)
        bar.pack(fill="x")
        self.btn_connect = PillButton(bar, text="保存并连接",
                                      command=self.on_save_connect)
        self.btn_connect.pack(side="left")
        self.btn_logout = PillButton(bar, text="下线", kind="danger",
                                     command=self.on_logout)
        self.btn_logout.pack(side="left", padx=(10, 0))

    # ---------- 页：自动化 ----------
    def _build_page_auto(self):
        page = tk.Frame(self.content, bg=BG)
        self.pages["auto"] = page

        # 工具箱（提至顶部，无需滚动即可访问；运维操作集中在此）
        card_t, inner_t = make_card(page)
        card_t.pack(fill="x", pady=(0, 16))
        tk.Label(inner_t, text="工具箱", bg=CARD, fg=TEXT,
                 font=F(15, "bold")).pack(anchor="w")
        tk.Label(inner_t,
                 text="一键自检 · 体检 · 自启管理 · 抓取诊断（原 .bat 已全部内置）",
                 bg=CARD, fg=SUB, font=F(10)).pack(anchor="w", pady=(4, 14))
        bar_t1 = tk.Frame(inner_t, bg=CARD)
        bar_t1.pack(fill="x")
        self.btn_self_test = PillButton(bar_t1, text="自检",
                                        command=self.on_self_test)
        self.btn_self_test.pack(side="left")
        self.btn_health = PillButton(bar_t1, text="一键体检",
                                     command=self.on_health_check)
        self.btn_health.pack(side="left", padx=(10, 0))
        self.btn_open_dir = PillButton(bar_t1, text="打开目录", kind="ghost",
                                       command=self.on_open_dir)
        self.btn_open_dir.pack(side="left", padx=(10, 0))

        bar_t2 = tk.Frame(inner_t, bg=CARD)
        bar_t2.pack(fill="x", pady=(12, 0))
        self.btn_install_auto = PillButton(bar_t2, text="安装开机自启",
                                           command=self.on_install_autostart)
        self.btn_install_auto.pack(side="left")
        self.btn_uninstall_auto = PillButton(bar_t2, text="卸载开机自启",
                                             kind="danger",
                                             command=self.on_uninstall_autostart)
        self.btn_uninstall_auto.pack(side="left", padx=(10, 0))
        self.btn_diagnose = PillButton(bar_t2, text="抓取诊断", kind="ghost",
                                       command=self.on_diagnose)
        self.btn_diagnose.pack(side="left", padx=(10, 0))
        tk.Label(bar_t2, text="登录 Windows 即自动守护，全程无窗口",
                 bg=CARD, fg=SUB2, font=F(9)).pack(side="right")

        card, inner = make_card(page)
        card.pack(fill="x")

        tk.Label(inner, text="自动认证", bg=CARD, fg=TEXT,
                 font=F(15, "bold")).pack(anchor="w", pady=(0, 16))

        # 开机自动认证
        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=(0, 14))
        box = tk.Frame(row, bg=CARD)
        box.pack(side="left")
        tk.Label(box, text="开机后自动认证", bg=CARD, fg=TEXT,
                 font=F(12, "bold")).pack(anchor="w")
        tk.Label(box, text="登录后自动在后台启动守护，无需任何操作",
                 bg=CARD, fg=SUB, font=F(10)).pack(anchor="w", pady=(4, 0))
        self.sw_autostart = Switch(row, command=self.on_toggle_autostart)
        self.sw_autostart.pack(side="right")

        tk.Frame(inner, bg=LINE_SOFT, height=1).pack(fill="x", pady=(4, 14))

        # 后台守护（进程管理）
        row2 = tk.Frame(inner, bg=CARD)
        row2.pack(fill="x")
        box2 = tk.Frame(row2, bg=CARD)
        box2.pack(side="left")
        tk.Label(box2, text="后台守护进程", bg=CARD, fg=TEXT,
                 font=F(12, "bold")).pack(anchor="w")
        tk.Label(box2, text="持续检测网络，掉线后静默自动重连（仅校园网内生效）",
                 bg=CARD, fg=SUB, font=F(10)).pack(anchor="w", pady=(4, 0))
        self.daemon_state_var = tk.StringVar(value="检测中…")
        tk.Label(row2, textvariable=self.daemon_state_var, bg=CARD,
                 fg=BLUE, font=F(10, "bold")).pack(side="right")

        dbar = tk.Frame(inner, bg=CARD)
        dbar.pack(fill="x", pady=(14, 0))
        self.btn_daemon_start = PillButton(dbar, text="启动守护",
                                           command=self.on_daemon_start)
        self.btn_daemon_start.pack(side="left")
        self.btn_daemon_stop = PillButton(dbar, text="停止守护",
                                          kind="danger",
                                          command=self.on_daemon_stop)
        self.btn_daemon_stop.pack(side="left", padx=(10, 0))
        self.btn_daemon_restart = PillButton(dbar, text="重启守护",
                                             kind="ghost",
                                             command=self.on_daemon_restart)
        self.btn_daemon_restart.pack(side="left", padx=(10, 0))
        tk.Label(dbar,
                 text="注：仅当处于校园网且检测到未认证时才自动登录",
                 bg=CARD, fg=SUB2, font=F(9)).pack(side="right")

        # 检测参数
        card2, inner2 = make_card(page)
        card2.pack(fill="x", pady=(16, 0))
        tk.Label(inner2, text="检测参数", bg=CARD, fg=TEXT,
                 font=F(15, "bold")).pack(anchor="w", pady=(0, 16))

        self.num_vars = {}
        fields = [
            ("interval", "在线检测周期（秒）", "掉线后最长多久自动重连"),
            ("offline_interval", "离线检测周期（秒）", "不在校园网时的检测间隔"),
            ("retry_interval", "失败重试间隔（秒）", "登录失败后的重试间隔"),
            ("max_interval", "最长退避间隔（秒）", "指数退避的上限"),
            ("timeout", "单次请求超时（秒）", "HTTP 请求超时时间"),
        ]
        grid = tk.Frame(inner2, bg=CARD)
        grid.pack(fill="x")
        for i, (key, label, hint) in enumerate(fields):
            cell = tk.Frame(grid, bg=CARD)
            cell.grid(row=i // 2, column=i % 2, sticky="ew",
                      padx=(0, 26) if i % 2 == 0 else 0, pady=(0, 14))
            tk.Label(cell, text=label, bg=CARD, fg=SUB,
                     font=F(10)).pack(anchor="w")
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            self.num_vars[key] = v
            self._entry(cell, v, width=12).pack(anchor="w", pady=(6, 0))
            tk.Label(cell, text=hint, bg=CARD, fg=SUB2,
                     font=F(9)).pack(anchor="w", pady=(4, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        bar = tk.Frame(inner2, bg=CARD)
        bar.pack(fill="x", pady=(6, 0))
        self.btn_save_auto = PillButton(bar, text="保存设置",
                                        command=self.on_save_auto)
        self.btn_save_auto.pack(side="left")

    # ---------- 页：状态与日志 ----------
    def _build_page_status(self):
        page = tk.Frame(self.content, bg=BG)
        self.pages["status"] = page

        card, inner = make_card(page)
        card.pack(fill="x")
        tk.Label(inner, text="当前状态", bg=CARD, fg=TEXT,
                 font=F(15, "bold")).pack(anchor="w", pady=(0, 16))

        self.stat_vars = {}
        grid = tk.Frame(inner, bg=CARD)
        grid.pack(fill="x")
        for i, (key, label) in enumerate([
                ("network", "网络状态"), ("account", "账号"),
                ("service", "服务"), ("ip", "IP 地址"),
                ("autostart", "开机自启"), ("daemon", "后台守护")]):
            r, c = divmod(i, 3)
            cell = tk.Frame(grid, bg=CARD)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 44), pady=(0, 14))
            tk.Label(cell, text=label, bg=CARD, fg=SUB,
                     font=F(10)).pack(anchor="w")
            v = tk.StringVar(value="—")
            self.stat_vars[key] = v
            tk.Label(cell, textvariable=v, bg=CARD, fg=TEXT,
                     font=F(13, "bold")).pack(anchor="w", pady=(4, 0))

        bar = tk.Frame(inner, bg=CARD)
        bar.pack(fill="x", pady=(6, 0))
        self.btn_refresh = PillButton(bar, text="刷新",
                                      kind="ghost", command=self.refresh_all)
        self.btn_refresh.pack(side="left")
        self.btn_once = PillButton(bar, text="立即检测",
                                   command=self.on_check_once)
        self.btn_once.pack(side="left", padx=(10, 0))
        self.btn_logout2 = PillButton(bar, text="下线", kind="danger",
                                      command=self.on_logout)
        self.btn_logout2.pack(side="left", padx=(10, 0))

        # 日志
        card2, inner2 = make_card(page, expand=True)
        card2.pack(fill="both", expand=True, pady=(16, 0))
        head = tk.Frame(inner2, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="运行日志", bg=CARD, fg=TEXT,
                 font=F(15, "bold")).pack(side="left")
        PillButton(head, text="打开日志文件", kind="ghost",
                   command=self.on_open_log).pack(side="right")

        wrap = tk.Frame(inner2, bg=FIELD, highlightthickness=1,
                        highlightbackground=LINE_SOFT)
        wrap.pack(fill="both", expand=True, pady=(14, 0))
        # wrap="char"：日志行再长也不会横向溢出，免去一条横向滚动条
        self.log_text = tk.Text(wrap, height=10, bg=FIELD, fg=TEXT,
                                relief="flat", bd=0, font=FM(9),
                                wrap="char", spacing1=3, spacing3=3,
                                padx=8, pady=6,
                                insertbackground=TEXT,
                                selectbackground=BLUE_SOFT,
                                selectforeground=TEXT)
        sb = ThinScrollbar(wrap, command=self.log_text.yview, bg=FIELD)
        self.log_text.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self.log_text.pack(side="left", fill="both", expand=True)

    # ---------- 小工具 ----------
    def _entry(self, parent, var, show=None, width=None):
        return RoundedEntry(parent, var=var, show=show, width=width)

    def _toggle_pwd(self, event=None):
        self.show_pwd.set(not self.show_pwd.get())
        self.pwd_entry.configure(show="" if self.show_pwd.get() else "•")
        self.toggle_pwd_lbl.configure(
            text="隐藏" if self.show_pwd.get() else "显示")

    def select_page(self, key):
        self._cur_page = key
        for k in self.tab_btns:
            self._draw_tab(k)
        for k, page in self.pages.items():
            if k == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        # 切页回到顶部，并重新统计滚动区
        try:
            self.scroll.reset()
            self.scroll.bind_children()
        except Exception:
            pass

    # ---------- 异步 ----------
    def run_async(self, fn, on_done=None, busy="处理中…"):
        if self.busy:
            return
        self.busy = True
        self._set_busy_ui(True, busy)
        self.state_var.set(busy)
        self.state_dot.set_color(SUB)

        def worker():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, "%r" % e
            # 注意：tkinter 只能在创建它的主线程中操作，
            # 这里绝不能调 self.after / 直接改控件，只能入队。
            self._q.put((res, err, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self):
        """主线程定时轮询工作线程的结果（唯一安全的跨线程 UI 更新方式）"""
        try:
            while True:
                res, err, on_done = self._q.get_nowait()
                self._async_done(res, err, on_done)
        except queue.Empty:
            pass
        self.after(60, self._poll_results)

    def _async_done(self, res, err, on_done):
        # 注意：这里不能再自动调用 refresh_status()，否则
        # "状态探测完成 -> 再探测" 会形成死循环，UI 会一直处于忙碌态。
        # 需要刷新状态的动作请在各自的 on_done 里显式调用。
        self.busy = False
        self._set_busy_ui(False)
        if err is not None:
            self.toast.show("操作失败", err, kind="error")
            return
        if on_done:
            on_done(res)

    def _set_busy_ui(self, on, text=""):
        if on:
            self.spinner.pack(side="right", padx=(12, 0))
            self.spinner.start()
        else:
            self.spinner.stop()
            self.spinner.pack_forget()
        for btn in (self.btn_connect, self.btn_logout, self.btn_save_auto,
                    self.btn_refresh, self.btn_once, self.btn_logout2,
                    self.btn_daemon_start, self.btn_daemon_stop,
                    self.btn_daemon_restart, self.btn_self_test,
                    self.btn_health, self.btn_install_auto,
                    self.btn_uninstall_auto, self.btn_open_dir):
            try:
                btn.set_enabled(not on)
            except Exception:
                pass
        self.sw_autostart.configure(cursor="" if on else "hand2")

    # ---------- 状态 ----------
    def refresh_status(self):
        """只重新探测网络 / 自启 / 守护状态（异步）"""
        self.run_async(self._probe_status, self._on_status)

    def refresh_all(self):
        """把配置文件重新载入界面，并刷新状态"""
        self.cfg = cn.load_config()
        self.var_host.set("%s:%s" % (self.cfg.get("portal_host", ""),
                                     self.cfg.get("portal_port", 80)))
        self.var_user.set(self.cfg.get("username", ""))
        self.seg_service.set(self.cfg.get("service", "default"))
        for key, var in self.num_vars.items():
            var.set(str(self.cfg.get(key, "")))
        self.refresh_status()

    def _probe_status(self):
        cfg = cn.load_config()
        client = cn.PortalClient(cfg)
        online, _ = client.check_online()
        info = None
        if online:
            info = client.interface("getOnlineUserInfo")
        return {
            "online": online,
            "info": info if isinstance(info, dict) else {},
            "autostart": cn.autostart_status(),
            "daemon": cn.daemon_running(),
            "daemon_pid": cn.daemon_pid(),
            "username": cfg.get("username", ""),
            "service": cfg.get("service", ""),
        }

    def _on_status(self, st):
        online = st["online"]
        info = st["info"]
        if online is True:
            self.state_var.set("已连接")
            self.state_dot.set_color(GREEN)
        elif online is None:
            self.state_var.set("网络不可达")
            self.state_dot.set_color(ORANGE)
        else:
            self.state_var.set("未认证")
            self.state_dot.set_color(RED)

        svc = info.get("service") or st["service"] or "—"
        self.stat_vars["network"].set(
            {True: "已认证", False: "未认证",
             None: "不可达"}[online])
        self.stat_vars["account"].set(
            cn.mask_user(st["username"]) or "—")
        self.stat_vars["service"].set(svc)
        self.stat_vars["ip"].set(info.get("userIp") or "—")
        self.stat_vars["autostart"].set(
            "已启用" if st["autostart"] else "未启用")
        self.stat_vars["daemon"].set(
            "运行中" if st["daemon"] else "已停止")
        self.daemon_state_var.set(
            ("● 运行中 (PID %s)" % st["daemon_pid"])
            if st["daemon"] else "○ 已停止")

        self.subtitle.set("门户 %s · 检测周期 %s 秒" % (
            self.cfg.get("portal_host", ""), self.cfg.get("interval", "")))

        self.sw_autostart.set(st["autostart"])
        self._load_log()
        self._sync_daemon_btns(st["daemon"])

    def _load_log(self):
        try:
            with open(cn.LOG_PATH, "r", encoding="utf-8",
                      errors="replace") as f:
                lines = f.readlines()[-LOG_TAIL:]
        except Exception:
            lines = ["（暂无日志）"]
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "".join(lines))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _sync_daemon_btns(self, running):
        """守护按钮按上下文启用/禁用：未运行时只能「启动」；运行时才能「停止/重启」"""
        try:
            self.btn_daemon_start.set_enabled(not running)
            self.btn_daemon_stop.set_enabled(running)
            self.btn_daemon_restart.set_enabled(running)
        except Exception:
            pass

    # ---------- 事件 ----------
    def on_save_connect(self):
        host = self.var_host.get().strip()
        if host:
            if "://" in host:
                host = host.split("://", 1)[1]
            host = host.split("/")[0]
            if ":" in host:
                h, p = host.rsplit(":", 1)
                try:
                    self.cfg["portal_port"] = int(p)
                except ValueError:
                    self.toast.show("端口无效", "请输入正确的端口号",
                                    kind="error")
                    return
                self.cfg["portal_host"] = h
            else:
                self.cfg["portal_host"] = host
        self.cfg["service"] = self.seg_service.value
        self.cfg["username"] = self.var_user.get().strip()
        if not self.cfg["username"]:
            self.toast.show("请填写账号", "账号（学号）不能为空", kind="error")
            return

        pwd = self.var_pass.get()
        if any(ord(c) > 127 for c in pwd):
            self.toast.show(
                "密码含非 ASCII 字符",
                "门户的加密库对中文等字符处理有缺陷，可能导致登录失败。\n"
                "建议改为纯字母 / 数字 / 常见符号。", kind="warn", ms=9000)
        cn.save_config(self.cfg)
        if pwd:
            cn.CredentialStore().save(pwd)
            self.var_pass.set("")
            self.pwd_hint.set("已保存密码，留空表示不修改")

        self.run_async(self._do_login, self._on_login_result, "正在认证…")

    def _do_login(self):
        cfg = cn.load_config()
        cred = cn.CredentialStore().load()
        if not cred:
            return {"result": "fail", "message": "尚未保存密码"}
        client = cn.PortalClient(cfg)
        return client.login(cfg.get("username", ""), cred)

    def _on_login_result(self, res):
        if isinstance(res, dict) and res.get("result") == "success":
            if res.get("userIndex"):
                # 必须走 update_state 合并写：save_state 是整体替换，会把
                # events（守护记录的掉线/重连/登录事件）和 first_run_at 冲掉，
                # 界面"最近事件"条会随之清空。与 HTML 版保持一致。
                cn.update_state({"userIndex": res["userIndex"],
                                 "loginTime": time.strftime(
                                     "%Y-%m-%d %H:%M:%S")},
                                drop=("last_error",))
            client = cn.PortalClient(cn.load_config())
            info = client.interface(
                "getOnlineUserInfo", {"userIndex": res.get("userIndex", "")})
            detail = ""
            if isinstance(info, dict):
                detail = "\n".join([
                    "姓名：%s" % (info.get("userName") or "—"),
                    "IP：%s" % (info.get("userIp") or "—"),
                    "服务：%s" % (info.get("service") or "—"),
                    "用户组：%s" % (info.get("userGroup") or "—"),
                ])
            self.toast.show("认证成功", detail, kind="ok")
        else:
            msg = cn.safe_message(res)
            hint = res.get("_hint", "") if isinstance(res, dict) else ""
            self.toast.show("认证失败",
                            (msg + ("\n" + hint if hint else "")),
                            kind="error", ms=8000)
        self.refresh_status()

    def on_logout(self):
        dlg = ConfirmDialog(self, "确认下线",
                            "下线后网络会中断。若后台守护正在运行，"
                            "它会自动重新认证。",
                            ok_text="下线", kind="danger")
        if not dlg.result:
            return

        def work():
            return {"rc": cn.cmd_logout(None)}

        def done(r):
            rc = r.get("rc") if r else 2
            if rc == 0:
                self.toast.show("已下线", "", kind="ok")
            else:
                self.toast.show("下线未完成", "详情请查看运行日志",
                                kind="warn", ms=6000)
            self.refresh_status()

        self.run_async(work, done, "正在下线…")

    def on_check_once(self):
        class _Args(object):
            pass

        def work():
            return {"rc": cn.cmd_once(_Args())}

        def done(r):
            rc = r.get("rc") if r else 2
            self.toast.show(
                "检测完成",
                {0: "网络正常，无需操作", 2: "认证失败，详情见日志"}.get(
                    rc, "已处理"),
                kind="ok" if rc == 0 else "warn")
            self.refresh_status()

        self.run_async(work, done, "正在检测…")

    def on_save_auto(self):
        for key, var in self.num_vars.items():
            raw = var.get().strip()
            try:
                val = int(raw)
            except ValueError:
                self.toast.show("参数无效", "%s 必须是整数" % key,
                                kind="error")
                return
            if val <= 0:
                self.toast.show("参数无效", "%s 必须大于 0" % key,
                                kind="error")
                return
            self.cfg[key] = val
        cn.save_config(self.cfg)
        self.toast.show("设置已保存", "新的检测参数将在守护下次循环时生效",
                        kind="ok")

    def on_toggle_autostart(self, value):
        def work():
            ok = cn.install_autostart() if value else cn.uninstall_autostart()
            return {"ok": ok, "value": value}

        def done(r):
            on = r.get("value")
            ok = r.get("ok")
            self.toast.show(
                "开机自动认证已%s" % ("启用" if on else "关闭"),
                "" if ok else "操作未完成，详情见日志",
                kind="ok" if ok else "error")
            self.refresh_status()

        self.run_async(work, done, "正在应用…")

    def on_daemon_start(self):
        def work():
            return {"ok": cn.start_daemon()}

        def done(r):
            ok = r.get("ok") if r else False
            self.toast.show("守护启动",
                            "已启动" if ok else "启动失败，详情见日志",
                            kind="ok" if ok else "error")
            self.refresh_status()

        self.run_async(work, done, "正在启动守护…")

    def on_daemon_stop(self):
        dlg = ConfirmDialog(self, "停止守护",
                            "停止后将不再自动检测与重连。",
                            ok_text="停止", kind="danger")
        if not dlg.result:
            return

        def work():
            return {"ok": cn.stop_daemon()}

        def done(r):
            ok = r.get("ok") if r else False
            self.toast.show("守护停止",
                            "已停止" if ok else "停止失败：未找到守护进程",
                            kind="ok" if ok else "warn")
            self.refresh_status()

        self.run_async(work, done, "正在停止守护…")

    def on_daemon_restart(self):
        def work():
            cn.stop_daemon()
            time.sleep(0.8)
            return {"ok": cn.start_daemon()}

        def done(r):
            ok = r.get("ok") if r else False
            self.toast.show("守护重启",
                            "已重启" if ok else "重启失败，详情见日志",
                            kind="ok" if ok else "error")
            self.refresh_status()

        self.run_async(work, done, "正在重启守护…")

    # ---------- 工具箱 ----------
    def on_self_test(self):
        """运行内置自检（RSA + 门户连通），结果弹出查看窗口"""

        def work():
            # CREATE_NO_WINDOW：防止子进程（尤其开发态的 python.exe，是控制台
            # 程序）弹出控制台/终端窗口。UI 是 GUI 进程，启动 CUI 子进程时系统
            # 会分配控制台，默认终端为 Windows Terminal 时会闪一个终端窗口。
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            if getattr(sys, "frozen", False):
                # 打包态：自检 = 运行自身 exe 的 test 子命令
                r = subprocess.run([sys.executable, "test"],
                                   capture_output=True, timeout=90,
                                   creationflags=flags)
            else:
                script = os.path.join(cn.BASE_DIR, "campusnet.py")
                r = subprocess.run([sys.executable, script, "test"],
                                   capture_output=True, timeout=90,
                                   creationflags=flags)
            out = (r.stdout or b"").decode("utf-8", "replace")
            err = (r.stderr or b"").decode("utf-8", "replace")
            return {"rc": r.returncode, "out": out + err}

        def done(r):
            if not r:
                self.toast.show("自检失败", "无法启动自检", kind="error")
                return
            ResultDialog(self, "自检结果", r.get("out") or "（无输出）")
            self.toast.show("自检完成",
                            "全部通过" if r.get("rc") == 0 else "存在问题，见结果",
                            kind="ok" if r.get("rc") == 0 else "warn")

        self.run_async(work, done, "正在自检…")

    def on_health_check(self):
        """一键体检（替代 一键测试.bat）：启动守护 -> 下线 -> 等自动重连

        这是真正的端到端验证：会短暂断开网络，等待守护自动恢复认证。
        打包态也能用（不依赖外部脚本）。
        """
        dlg = ConfirmDialog(
            self, "一键体检",
            "将执行端到端验证：启动守护 → 主动下线 → 等待守护自动重连。\n"
            "过程中网络会短暂中断（通常十几秒），完成后自动恢复。",
            ok_text="开始体检")
        if not dlg.result:
            return

        def work():
            steps = []
            cfg = cn.load_config()
            if not cfg.get("username") or not cn.CredentialStore().load():
                return {"ok": False,
                        "steps": ["× 尚未配置账号 / 密码"],
                        "detail": "请先在「连接与账号」页保存账号密码"}
            steps.append("✓ 配置与凭据齐全")

            if cn.daemon_running():
                steps.append("✓ 守护已在运行")
            else:
                ok = cn.start_daemon()
                steps.append(("✓ 守护已启动" if ok else "× 守护启动失败"))
                if not ok:
                    return {"ok": False, "steps": steps,
                            "detail": "守护启动失败，详情见运行日志"}

            rc = cn.cmd_logout(None)
            steps.append(("✓ 已触发下线" if rc == 0 else "× 下线失败"))
            if rc != 0:
                return {"ok": False, "steps": steps,
                        "detail": "下线失败（可能当前已离线），详情见日志"}

            deadline = time.time() + 75
            online = False
            waited = 0
            while time.time() < deadline:
                time.sleep(2)
                waited = 75 - (deadline - time.time())
                try:
                    c = cn.PortalClient(cn.load_config())
                    online, _ = c.check_online()
                except Exception:
                    online = False
                if online:
                    break
            steps.append(("✓ 守护自动重连（%.0f 秒）" % waited) if online
                         else "× 等待自动重连超时（%.0f 秒）" % waited)
            return {
                "ok": bool(online),
                "steps": steps,
                "detail": ("守护已在 %.0f 秒内自动恢复认证，"
                           "自动重连链路正常" % waited) if online
                          else ("等待超时：请确认处于校园网环境，"
                                "且守护进程确实在运行（见「自动化」页）"),
            }

        def done(r):
            if not r:
                return
            text = "\n".join(r.get("steps", [])) + \
                   "\n\n" + (r.get("detail") or "")
            ResultDialog(self, "一键体检结果", text)
            self.toast.show(
                "体检通过" if r.get("ok") else "体检未通过",
                r.get("detail") or "", kind="ok" if r.get("ok") else "warn",
                ms=8000)
            self.refresh_status()

        self.run_async(work, done, "正在体检…")

    def on_open_dir(self):
        """打开程序所在目录（配置文件 / 日志都在这里）"""
        try:
            os.startfile(cn.BASE_DIR)  # noqa: S606
        except Exception as e:
            self.toast.show("打开失败", "%r" % e, kind="error")

    def on_diagnose(self):
        """门户抓取诊断（持续监控 + 自动抓取登录/会话证据并出报告）"""
        def work():
            # CREATE_NO_WINDOW+DETACHED+NEW_PG：完全脱离父进程，
            # UI 关闭不影响诊断运行；输出报告写到 probe/capture_result.json
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                     | getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "diagnose"]
            else:
                script = os.path.join(cn.BASE_DIR, "campusnet.py")
                cmd = [sys.executable, script, "diagnose"]
            subprocess.Popen(cmd, cwd=cn.BASE_DIR,
                             creationflags=flags, close_fds=True)
            return {"ok": True}

        def done(r):
            out = os.path.join(cn.BASE_DIR, "probe")
            self.toast.show(
                "诊断已启动",
                "后台持续监控门户，认证完成时自动出报告。"
                "结果写入：%s" % os.path.join(out, "capture_result.json"),
                kind="info", ms=6500)
            self._load_log()

        self.run_async(work, done, "正在启动诊断…")

    def on_install_autostart(self):
        dlg = ConfirmDialog(self, "安装开机自启",
                            "将写入当前用户启动项，登录后自动在后台守护认证。",
                            ok_text="安装")
        if not dlg.result:
            return

        def work():
            return {"ok": cn.install_autostart()}

        def done(r):
            self.toast.show("开机自启安装",
                            "已安装" if r.get("ok") else "安装失败，详情见日志",
                            kind="ok" if r.get("ok") else "error")
            self.refresh_status()

        self.run_async(work, done, "正在安装…")

    def on_uninstall_autostart(self):
        dlg = ConfirmDialog(self, "卸载开机自启",
                            "将移除当前用户启动项，不再自动守护认证。",
                            ok_text="卸载", kind="danger")
        if not dlg.result:
            return

        def work():
            return {"ok": cn.uninstall_autostart()}

        def done(r):
            self.toast.show("开机自启卸载",
                            "已卸载" if r.get("ok") else "卸载失败，详情见日志",
                            kind="ok" if r.get("ok") else "error")
            self.refresh_status()

        self.run_async(work, done, "正在卸载…")

    def on_open_log(self):
        try:
            if os.path.exists(cn.LOG_PATH):
                os.startfile(cn.LOG_PATH)  # noqa: S606
            else:
                self.toast.show("暂无日志", "还没有生成日志文件", kind="warn")
        except Exception as e:
            self.toast.show("打开失败", "%r" % e, kind="error")


def main():
    try:
        with open("_ui_trace.log", "a", encoding="utf-8") as f:
            f.write("ui.main() entered\n")
    except Exception:
        pass
    cn.force_utf8_console()
    try:
        with open("_ui_trace.log", "a", encoding="utf-8") as f:
            f.write("after force_utf8_console\n")
    except Exception:
        pass
    # 高 DPI 屏下让界面清晰（必须在创建窗口之前调用）
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        with open("_ui_trace.log", "a", encoding="utf-8") as f:
            f.write("before App()\n")
    except Exception:
        pass
    app = App()
    try:
        with open("_ui_trace.log", "a", encoding="utf-8") as f:
            f.write("after App() hwnd=%d\n" % app.winfo_id())
    except Exception:
        pass
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

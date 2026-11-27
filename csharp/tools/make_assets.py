#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 icon-render.png 转成应用图标资产（ICO / 1024 / 256 / splash）。

图标本身由 assets/icon.html（HTML/CSS/SVG 代码绘制）经 IconRenderer 渲染；
本脚本只做尺寸导出与安装引导图合成。需要 Pillow。
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
assets = os.path.join(here, "assets")
src_path = os.path.join(assets, "icon-render.png")
if not os.path.exists(src_path):
    sys.exit("先运行 build_icon.ps1 / IconRenderer 生成 " + src_path)

src = Image.open(src_path).convert("RGBA")
src.save(os.path.join(assets, "app-1024.png"))
src.resize((256, 256), Image.LANCZOS).save(os.path.join(assets, "app-256.png"))
src.save(os.path.join(assets, "app.ico"),
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

# 安装引导图：浅色底 + 图标 + 标题 + 品牌蓝底边（与 UI 同风格）
W, H = 920, 240
img = Image.new("RGB", (W, H), (245, 245, 247))
d = ImageDraw.Draw(img)
icon = src.resize((168, 168), Image.LANCZOS)
img.paste(icon, (44, (H - 168) // 2), icon)
bold = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 46)
sub = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 24)
d.text((244, 66), "CampusNetAuth", font=bold, fill=(29, 29, 31))
d.text((246, 136), "校园网无感认证 · 安装", font=sub, fill=(95, 95, 102))
d.rectangle([(0, H - 6), (W, H)], fill=(0, 122, 255))
img.save(os.path.join(assets, "splash.png"))
print("assets regenerated")

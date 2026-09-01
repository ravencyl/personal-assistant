#!/usr/bin/env python3
"""从品牌源图生成站点 logo 全套资源（裁切 + 多尺寸 + 图标底板）

跑法（pillow/numpy 只用于出图，不进 requirements.txt、不进运行时）：
    ./venv/bin/pip install --quiet --target /tmp/pil-lib pillow numpy
    PYTHONPATH=/tmp/pil-lib ./venv/bin/python brand/make_logo_assets.py

换源图后直接重跑：所有裁切框由「暗色内容包围盒 + 行分层」自动推导，不写死坐标。
源图四周的大片留白和网点底纹会被自动裁掉（网点是浅灰，不算暗色内容）。

产出（全部提交进仓库，站点不依赖本脚本运行）：
    static/img/logo-mark.png      透明底，鸟+三磊，导航用（宽 264）
    static/img/logo-lockup.png    透明底，鸟+三磊+RAVEN，登录页这类品牌位（宽 320）
    static/icons/favicon-{16,32,48}.png     深色方块 + 加粗反白（小尺寸下细线会断）
    static/icons/apple-touch-icon.png       180，iOS 不圆角（系统自己加 mask）
    static/icons/icon-{192,512}.png         PWA，圆角
    static/icons/icon-maskable-512.png      PWA maskable，内容收在中央 80% 安全区
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'brand' / 'raven-sanlei-source.png'
OUT_IMG = ROOT / 'static' / 'img'
OUT_ICON = ROOT / 'static' / 'icons'

INK = (24, 24, 27)          # 与 theme-color 一致的近黑
WHITE = (255, 255, 255)
LUM_THRESHOLD = 180         # 低于此亮度算「内容」（网点底纹亮度约 220-245，天然被排除）
EDGE_PAD = 0.04             # 裁切后额外留的内容宽/高比例边距

# 小尺寸加粗量：单位是「目标图标上的像素」，不是源图比例。
# 在源图空间（1008px 宽）固定核膨胀再缩放，缩到 16px 后加粗量只剩零点几像素，等于没加粗。
# 档位是逐尺寸目测定出来的：16px 加 0.7px 仍能看出轮廓咬合，加到 1.0px 「三/磊」就并成色块；
# 48px 加 0.6px 线条实心且结构完整。改这几个值只会更糊或更断，调前先看实际渲染。
TARGET_THICKEN = {16: 0.70, 32: 0.60, 48: 0.60, 180: 0.80}

# 导航标另有一档轻加粗：资源宽 264、页面只显 44px，不加粗的话线条掉进亚像素、
# 整标发灰（「三磊」几乎看不清）。按实测 44px 下加 0.3px 最接近设计稿观感，
# 0.55px 以上「磊」的内部笔画就开始并；高分屏上这一档加粗几乎看不出来。
NAV_MARK_DISPLAY = 44
NAV_MARK_THICKEN = 0.30


def content_bbox(mask):
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def row_bands(mask, min_gap=6):
    """按行投影切出上下分层，用于把 RAVEN 字标从图形里分离出去"""
    rows = mask.sum(axis=1)
    bands, start = [], None
    for i, v in enumerate(rows):
        if v > 3 and start is None:
            start = i
        elif v <= 3 and start is not None:
            bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, len(rows)))
    return [b for b in bands if b[1] - b[0] >= min_gap]


def padded(box, src_size, pad_bottom=True):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    px, py = int(w * EDGE_PAD), int(h * EDGE_PAD)
    # pad_bottom=False：下边界是手工选定的分层切线（RAVEN 上方那条空隙），
    # 再往下吃边就会把字标顶部吃进图形标（实测踩过：logo-mark 下缘出现 RAVEN 截断头）
    return (max(0, x0 - px), max(0, y0 - py), min(src_size[0], x1 + px),
            min(src_size[1], y1 + (py if pad_bottom else 0)))


def to_alpha(crop, color, extra_px=0.0, display_px=None):
    """白底细线图 → 透明底彩色图（按亮度反推 alpha，保留抗锯齿边缘）

    extra_px 不为 0 时先按「最终显示宽度 display_px」把线条加粗，用于导航标这种
    要缩得很小的透明底场景；不加粗时保持设计稿原粗细。
    """
    lum = np.asarray(crop.convert('L')).astype(float)
    if extra_px and display_px:
        k = max(3, int(round(crop.size[0] / display_px * extra_px)) | 1)
        solid = Image.fromarray((lum < LUM_THRESHOLD).astype(np.uint8) * 255)
        grown = np.asarray(solid.filter(ImageFilter.MaxFilter(k)), dtype=float)
        lum = np.minimum(lum, np.where(grown > 0, 0.0, 255.0))
    alpha = np.clip((250.0 - lum) * (255.0 / (250.0 - 40.0)), 0, 255).astype('uint8')
    out = Image.new('RGBA', crop.size, color + (0,))
    out.putalpha(Image.fromarray(alpha))
    return out


def solid_mask(crop):
    """取暗色内容的二值蒙版（丢掉抗锯齿灰边，后面按目标尺寸决定加粗力度）"""
    lum = np.asarray(crop.convert('L')).astype(int)
    return Image.fromarray(((lum < LUM_THRESHOLD).astype(np.uint8)) * 255)


def thicken(mask_img, size, extra_px):
    """把线条加粗：源图空间核大小 = 源宽 / 目标尺寸 * 目标要多粗的像素数"""
    k = max(3, int(round(mask_img.size[0] / size * extra_px)) | 1)
    return mask_img.filter(ImageFilter.MaxFilter(k))


def tile(alpha_img, size, ratio, color, radius=0, bg=INK, thick_mask=None):
    """把图形居中贴到方形底板上；ratio = 图形长边占底板边的比例

    alpha_img 与 thick_mask 二选一：前者是原粗细的透明底图（192/512 用），
    后者是加粗后的蒙版（16-48px 用，细线缩到那个尺寸会直接断）。
    """
    from PIL import ImageDraw

    src_dims = (alpha_img or thick_mask).size
    canvas = Image.new('RGBA', (size, size), bg + (255,))
    if radius:
        mask = Image.new('L', (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
        canvas.putalpha(mask)

    w = int(size * ratio)
    h = int(w * src_dims[1] / src_dims[0])
    if h > size * ratio:
        h = int(size * ratio)
        w = int(h * src_dims[0] / src_dims[1])
    if thick_mask is not None:
        art = Image.new('RGBA', (w, h), color + (0,))
        art.putalpha(thick_mask.resize((w, h), Image.LANCZOS))
    else:
        art = alpha_img.resize((w, h), Image.LANCZOS)
    canvas.paste(art, ((size - w) // 2, (size - h) // 2), art)
    return canvas


def main():
    if not SRC.exists():
        sys.exit(f'找不到源图 {SRC}')
    src = Image.open(SRC).convert('RGB')
    lum = np.asarray(src).astype(int).mean(axis=2)
    mask = lum < LUM_THRESHOLD

    full = content_bbox(mask)
    bands = row_bands(mask)
    # 最下面一层若明显矮于总高（字标 RAVEN），从图形标里剔除
    mark_box = full
    if len(bands) >= 2:
        last0, last1 = bands[-1]
        if (last1 - last0) < 0.45 * (full[3] - full[1]):
            mark_box = (full[0], full[1], full[2], last0)

    full_box = padded(full, src.size)
    mark_box = padded(mark_box, src.size, pad_bottom=False)
    lockup = src.crop(full_box)
    mark = src.crop(mark_box)
    print(f'源图 {src.size}  内容包围盒 {full}  → 占画布 '
          f'{(full[2]-full[0])/src.size[0]*100:.0f}% x {(full[3]-full[1])/src.size[1]*100:.0f}%')
    print(f'图形标 MARK {mark_box} {mark.size}   完整标 LOCKUP {lockup.size}')

    OUT_IMG.mkdir(parents=True, exist_ok=True)
    OUT_ICON.mkdir(parents=True, exist_ok=True)
    # 透明底墨色图：给导航/登录页这类浅色背景上用。
    # 宽度按「页面显示尺寸 × 2（高分屏余量）」给，不要图省事直接倒源图尺寸：
    # 导航标 h-7（宽 44px）、登录页完整标 w-40（160px），再大就是白白多几倍流量
    mark_a = to_alpha(mark, INK, extra_px=NAV_MARK_THICKEN, display_px=NAV_MARK_DISPLAY)
    lockup_a = to_alpha(lockup, INK)
    mark_a.resize((264, int(264 * mark_a.size[1] / mark_a.size[0])), Image.LANCZOS).save(OUT_IMG / 'logo-mark.png')
    lockup_a.resize((320, int(320 * lockup_a.size[1] / lockup_a.size[0])), Image.LANCZOS).save(OUT_IMG / 'logo-lockup.png')

    # 方形图标统一「深色底板 + 反白图形」：细线在深色上比在白底上撑得住小尺寸
    mark_white = to_alpha(mark, WHITE)
    solid = solid_mask(mark)
    for s in (16, 32, 48):
        tile(None, s, 0.88, WHITE, radius=max(3, s // 6),
             thick_mask=thicken(solid, s, TARGET_THICKEN[s])).save(OUT_ICON / f'favicon-{s}.png')
    # iOS 主屏图标会被系统再加一层 mask，自己再做圆角会双重裁切 → 铺满方角
    tile(None, 180, 0.80, WHITE, radius=0,
         thick_mask=thicken(solid, 180, TARGET_THICKEN[180])).save(OUT_ICON / 'apple-touch-icon.png')
    for s in (192, 512):
        tile(mark_white, s, 0.76, WHITE, radius=s // 5).save(OUT_ICON / f'icon-{s}.png')
    # maskable：各系统按自己的形状裁，内容必须收在中央 80% 安全区内
    tile(mark_white, 512, 0.58, WHITE, radius=0).save(OUT_ICON / 'icon-maskable-512.png')

    made = sorted(list(OUT_IMG.glob('logo-*.png')) + list(OUT_ICON.glob('*.png')))
    print('产出：')
    for p in made:
        print(f'  {p.relative_to(ROOT)}  {Image.open(p).size}  {p.stat().st_size}B')


if __name__ == '__main__':
    main()

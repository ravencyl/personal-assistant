#!/usr/bin/env python3
"""从 AI 生成的粉胖乌鸦源图产出全站图标（favicon / PWA）。

源图 brand/pink-crow-source.png 是一次 ImageGen 生成稿（1024×1024，白色圆角
方块底 + 灰色画布底 + 右下角水印）。本脚本做三件事：
  1. 探测白色圆角方块边界，取其内切正方形（把灰底与水印裁掉）；
  2. 产出 favicon-16/32/48（带圆角 alpha）；
  3. 产出 apple-touch-icon / icon-192 / icon-512（全出血方形，iOS/Android 自裁）
     与 icon-maskable-512（白底全出血 + 头部缩进 safe zone）。

文件名与 base.html / manifest.json 的既有引用一一对应，脚本重跑即整体换图。
PIL 依赖在 venv 内（requirements.txt 已含 Pillow）。
"""
from pathlib import Path

from PIL import Image, ImageDraw

BRAND = Path(__file__).resolve().parent
STATIC_ICONS = BRAND.parent / 'static' / 'icons'
SOURCE = BRAND / 'pink-crow-source.png'

# maskable 图标的安全区：Android 会裁掉外圈，内容须落在中心 ~80% 内
MASKABLE_CONTENT_RATIO = 0.76
# favicon 圆角占比（与参考图的 app icon 观感一致）
FAVICON_CORNER_RATIO = 0.22


def detect_white_rect(im: Image.Image) -> tuple[int, int, int, int]:
    """探测白色圆角方块在灰色画布底上的边界。"""
    px = im.convert('RGB').load()
    w, h = im.size

    def is_gray_bg(c):
        r, g, b = c
        return abs(r - g) < 14 and abs(g - b) < 14 and 200 < r < 245

    row, col = h // 2, w // 2
    left = next(x for x in range(w) if not is_gray_bg(px[x, row]))
    right = next(x for x in range(w - 1, -1, -1) if not is_gray_bg(px[x, row]))
    top = next(y for y in range(h) if not is_gray_bg(px[col, y]))
    bottom = next(y for y in range(h - 1, -1, -1) if not is_gray_bg(px[col, y]))
    return left, top, right, bottom


def square_master() -> Image.Image:
    """裁出白色方块内的最大内切正方形（顺带裁掉边界外水印），返回 RGBA。"""
    im = Image.open(SOURCE).convert('RGB')
    left, top, right, bottom = detect_white_rect(im)
    # 生成器署名水印（中灰文字）与卡片底缘阴影都落在白底下部：把下条带内的近灰
    # 像素统一刷白（含 x 全宽，阈值放宽到浅灰阴影），粉色头部（r 通道显著高于
    # g/b）与橙嘴不受影响；深灰蓝眉毛不在该条带内
    px = im.load()
    for y in range(int(top + (bottom - top) * 0.86), im.size[1]):
        for x in range(left, im.size[0]):
            r, g, b = px[x, y]
            if abs(r - g) < 18 and abs(g - b) < 18 and 90 < r < 246:
                px[x, y] = (255, 255, 255)
    # 裁切边缘 25% 环带：源卡片圆角弧与圆角遮罩半径存在细微错位，会在四角
    # 残留浅灰弧线，环带内统一刷白（头部粉色/橙嘴为彩色，不会被误伤）
    side = min(right - left, bottom - top)
    ring = int(side * 0.25)
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            if not (x < left + ring or x > right - ring
                    or y < top + ring or y > bottom - ring):
                continue
            r, g, b = px[x, y]
            if abs(r - g) < 18 and abs(g - b) < 18 and 90 < r < 246:
                px[x, y] = (255, 255, 255)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    side = min(right - left, bottom - top)
    half = side // 2
    # 各边再收 1.5%，把圆角弧残留裁干净
    half = int(half * 0.985)
    box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
    return im.crop(box).resize((1024, 1024), Image.LANCZOS).convert('RGBA')


def flatten(im: Image.Image) -> Image.Image:
    """圆角遮罩贴回白底：去掉源卡片圆角外透出的灰底，输出全出血不透明 RGB。"""
    mask = Image.new('L', im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, im.size[0] - 1, im.size[1] - 1],
        radius=int(im.size[0] * FAVICON_CORNER_RATIO), fill=255)
    out = Image.new('RGB', im.size, (255, 255, 255))
    out.paste(im, (0, 0), mask)
    return out


def rounded(im: Image.Image, ratio: float) -> Image.Image:
    mask = Image.new('L', im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, im.size[0] - 1, im.size[1] - 1],
        radius=int(im.size[0] * ratio), fill=255)
    out = Image.new('RGBA', im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def main() -> None:
    master = square_master()

    # favicon：圆角 alpha，浏览器标签页直接呈现 app icon 观感
    for size in (16, 32, 48):
        rounded(master.resize((size, size), Image.LANCZOS), FAVICON_CORNER_RATIO) \
            .save(STATIC_ICONS / f'favicon-{size}.png')

    # apple-touch-icon：iOS 自行上圆角，必须全出血不透明
    flatten(master).resize((180, 180), Image.LANCZOS).save(STATIC_ICONS / 'apple-touch-icon.png')

    # PWA 主图标：全出血方形
    flat = flatten(master)
    for size in (192, 512):
        flat.resize((size, size), Image.LANCZOS).save(STATIC_ICONS / f'icon-{size}.png')

    # maskable：白底全出血，内容缩进 safe zone
    content_size = int(512 * MASKABLE_CONTENT_RATIO)
    head = flat.resize((content_size, content_size), Image.LANCZOS)
    canvas = Image.new('RGB', (512, 512), (255, 255, 255))
    canvas.paste(head, ((512 - content_size) // 2, (512 - content_size) // 2))
    canvas.save(STATIC_ICONS / 'icon-maskable-512.png')

    print('OK: icons written to', STATIC_ICONS)


if __name__ == '__main__':
    main()

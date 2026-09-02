"""桌面两列布局回归锁的共享断言件（只在测试里用，不是运行时模块）。

为什么住 core：这套断言要被 core / activities / chat / knowledge / notes / memory 六个
测试模块共用，而依赖方向是单向的 —— helper 只能放在横切层 core，不能放在任何业务 app
里（否则就出现 app → app 的反向依赖）。它只读文本（渲染出的 HTML 与 custom.css），
不 import 任何业务 app、不碰 ORM。

为什么必须有这类锁：桌面两列完全靠「custom.css 里的一份 .page-cols 声明 + 模板里两个
列容器」实现，退化时没有任何报错 —— 顺手改回单列、把右列某块挤进左列、给移动端加了
sm: 结构断点、给某页单独抄一份 grid，都只在真实屏幕上看得出来。所以每张改造过的页面
都要在这里留一条锁。
"""
import re
from pathlib import Path

from django.conf import settings

# 全站唯一的列声明位置
CSS_PATH = Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css'

# 列容器分隔锚点：模板里显式写成注释，改结构时会一起被看到（比按 class 猜嵌套深度可靠）
COLS_END = '</div><!-- /.page-cols -->'
MAIN_END = '</div><!-- /左列 -->'
RAIL_END = '</div><!-- /右列 -->'

# AGENTS.md【前端分端约定】：结构性显隐只允许 md:，sm: 仅可作纯尺寸渐进
STRUCTURAL_SM = re.compile(
    r'sm:(hidden|block|inline|flex|grid|order|col-span|row-span|sticky|absolute|fixed)\S*')
# 扫结构断点前先剔掉注释：这里要锁的是「真正生效的类」，不是散文。
# 不剔的话一句「原来的 sm:grid-cols-4 是越界断点」的说明就会把锁打成假失败（真实踩到过）
COMMENT_SPANS = re.compile(r'<!--.*?-->|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', re.S)
# 同一个约定反过来：也不允许拿 lg:/xl: 当结构断点（费用报告页原来就有一堆）。
# 视口断点不跟随列宽：两列化后左列只到 864px，那里的 lg:grid-cols-2 会硬拆成两个 416px 块。
STRUCTURAL_WIDE = re.compile(
    r'\b(?:lg|xl):(hidden|block|inline|flex|grid|order|col-span|row-span|sticky|absolute|fixed)\S*')


def code_only(template_src):
    """剔掉模板里的 HTML 注释与 {% comment %} 块，只留下会真正渲染的部分

    静态扫类名时必须走这里：那些「为什么改」的注释里就会提到旧类名（max-w-4xl、
    sm:grid-cols-4……），不剔掉扫的是散文而不是生效的类，锁会假失败（本轮踩到两次）。
    """
    return COMMENT_SPANS.sub('', template_src)


def css_rules(css, selector):
    """取 custom.css 里所有「选择器串中出现 selector」的规则块。

    返回 [{'selectors': str, 'media': 所在 @media 条件（不在媒体查询里则为空串）,
           'body': 声明文本}]。直接字符串找 '.page-cols {' 会漏掉分组选择器的情况，
    锁就静默空跑了，所以统一走这里。
    """
    blocks = []
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        selectors = ' '.join(s.strip() for s in m.group(1).split(',')).strip()
        if selector not in selectors:
            continue
        before = css[:m.start()]
        media = ''
        at = before.rfind('@media')
        if at != -1 and before.count('{', at) > before.count('}', at):
            media = before[at:].split('{', 1)[0].replace('@media', '').strip()
        blocks.append({'selectors': selectors, 'media': media, 'body': m.group(2)})
    return blocks


def _pairs(anchors):
    """锚点既接受 'a'，也接受 ('a', '说明')；统一成 (anchor, desc)"""
    return [x if isinstance(x, tuple) else (x, x) for x in anchors]


def assert_desktop_two_columns(case, html, *, template_src=None, left=(), right=(),
                               mobile_order=(), rail_first=False, exclusive=True):
    """断言「这一页是通用的桌面两列，且移动端顺序与列归属没跑偏」。

    参数
      html          : 页面渲染结果（必须带登录态渲染，否则条件渲染块整块不出现，锁会空跑）
      template_src  : 模板源文本，用于 sm: 结构断点扫描；不传则跳过该项
      left / right  : 必须在左列 / 右列出现的锚点（允许 ('锚点', '人话说明') 形式）
      mobile_order  : 一串锚点，断言它们在 html 里的先后顺序 = 移动端阅读顺序
      rail_first    : 该页右列整块是否排在主内容流之前（为了移动端顺序与改造前一致）
      exclusive     : 是否同时断言「右列锚点不得出现在左列」（反之亦然）
    """
    # ---- 1. 列容器唯一且成对 ----
    case.assertEqual(html.count('class="page-cols'), 1,
                     '这一页应有一个 .page-cols 两列容器（多一个说明有人抄了第二份）')
    start = html.find('class="page-cols')
    if start < 0:
        case.fail('找不到 .page-cols 两列容器 —— 这一页被改回单列了')
    end = html.find(COLS_END, start)
    if end < 0:
        case.fail(f'找不到两列收尾锚点 {COLS_END}，两列结构已破损')
    cols = html[start:end]
    case.assertEqual(cols.count('class="page-main'), 1,
                     '.page-cols 内应恰好一个主内容流容器（左列）')
    case.assertEqual(cols.count('class="page-rail'), 1,
                     '.page-cols 内应恰好一个辅助右列容器')

    # ---- 2. 列容器不加显隐类：移动端视觉顺序 = DOM 顺序的前提 ----
    for cls in ('page-main', 'page-rail'):
        tag = re.search(r'<div class="%s[^"]*"' % cls, cols)
        case.assertTrue(tag, f'{cls} 容器写法异常（应是以 {cls} 开头的 class）')
        case.assertNotIn('hidden', tag.group(0),
                         f'{cls} 加了显隐类，移动端会少一整列')

    # ---- 3. DOM 顺序与 rail-first 修饰类 ----
    main_at = cols.find('class="page-main')
    rail_at = cols.find('class="page-rail')
    rail_first_in_html = 'page-cols--rail-first' in html
    case.assertEqual(rail_first_in_html, rail_first,
                     'rail-first 修饰类与 DOM 顺序不一致：右列在 DOM 里排前面却不换序，'
                     '桌面端会变成左右颠倒（或反过来：该换序没换，移动端阅读顺序被改动）')
    if rail_first:
        case.assertLess(rail_at, main_at,
                        '挂了 rail-first 却把右列放在后面，桌面端左右会颠倒')
    else:
        case.assertLess(main_at, rail_at, '没挂 rail-first 时主内容流应在 DOM 前')

    main_end = cols.find(MAIN_END)
    rail_end = cols.find(RAIL_END)
    case.assertGreater(main_end, -1, f'左列未闭合（缺 {MAIN_END}）')
    case.assertGreater(rail_end, -1, f'右列未闭合（缺 {RAIL_END}）')
    left_slice = cols[:main_end]
    # 右列切片：rail-first 时右列在左列之前
    if rail_first:
        right_slice = cols[:rail_end]
        left_slice = cols[main_at:main_end]
    else:
        right_slice = cols[rail_at:rail_end]

    # ---- 4. 内容归属：主内容流 vs 辅助右列 ----
    for anchor, desc in _pairs(left):
        case.assertIn(anchor, left_slice, f'{desc} 应在左列主内容流')
    for anchor, desc in _pairs(right):
        case.assertIn(anchor, right_slice, f'{desc} 应在右列（辅助信息/概览/操作入口）')
    if exclusive:
        for anchor, desc in _pairs(right):
            case.assertNotIn(anchor, left_slice, f'{desc} 不该出现在左列（被挤进主内容流了）')
        for anchor, desc in _pairs(left):
            case.assertNotIn(anchor, right_slice, f'{desc} 不该出现在右列')

    # ---- 5. 移动端单列顺序 ----
    positions = []
    for anchor, desc in _pairs(mobile_order):
        at = html.find(anchor)
        if at < 0:
            case.fail(f'移动端顺序锁空跑：页面里没有 {desc or anchor}（条件渲染块没带数据？）')
        positions.append(at)
    case.assertEqual(positions, sorted(positions),
                     '移动端单列阅读顺序变了（列容器不加显隐类，视觉顺序 = DOM 顺序）')

    # ---- 6. 列声明：全站唯一一份、只落在 768px 这个结构断点上 ----
    css = CSS_PATH.read_text(encoding='utf-8')
    cols_blocks = css_rules(css, '.page-cols')
    case.assertTrue(cols_blocks, 'custom.css 里 .page-cols 的声明丢了')
    for block in cols_blocks:
        case.assertEqual(block['media'], '(min-width: 768px)',
                         '.page-cols 必须只落在 768px 这个唯一结构断点内（移动端不该有两列）')
    grid = next((b for b in cols_blocks if b['selectors'] == '.page-cols'), None)
    case.assertTrue(grid, '.page-cols 的 grid 声明必须只有独立的一份，不与其他页面分组共享'
                          '（分组一处改动会连带别的页面）')
    case.assertIn('minmax(0, 1fr)', grid['body'], '左列须用 minmax(0,1fr) 兜住长内容撑破列')
    case.assertIn('320px', grid['body'], '右列固定 320px 是口径的一部分')
    case.assertIn('align-items: start', grid['body'],
                  '网格默认 stretch 会把右列拉高，sticky 就没空间钉住')
    rail_blocks = css_rules(css, '.page-rail')
    case.assertTrue(rail_blocks, 'custom.css 里 .page-rail 的常驻声明丢了')
    case.assertIn('position: sticky', rail_blocks[0]['body'], '右列整列常驻是桌面端设计的一部分')
    case.assertIn('max-height', rail_blocks[0]['body'],
                  '矮视口下右列需列内滚动，否则底部信息看不到')
    if rail_first:
        order_blocks = [b for b in css_rules(css, '.page-rail') if 'order' in b['body']]
        case.assertTrue(order_blocks, '缺把右列换回右侧的 order 声明，桌面端左右会颠倒')

    # ---- 7. 分端约定：只允许 md: 这一个结构断点 ----
    if template_src is not None:
        stripped = code_only(template_src)
        hits = STRUCTURAL_SM.findall(stripped)
        case.assertEqual(hits, [], f'模板出现 sm: 结构性断点：{hits}')
        wide = STRUCTURAL_WIDE.findall(stripped)
        case.assertEqual(wide, [], f'模板出现 lg:/xl: 结构性断点：{wide}')

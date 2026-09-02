"""AI 回复的 Markdown 渲染（服务端，无第三方依赖）

为什么放在服务端而不是前端：
1. 「先转义、再解析」这条安全顺序可以直接用单测覆盖（前端的 XSS 面在 Django 测试里看不见）
2. 离线页 / PWA 缓存里的内容同样是排好版的，不依赖 JS 是否加载成功
3. 不会出现「先纯文本、再闪成 HTML」的重排
4. 对话消息、知识库文章、周报正文三处复用同一个过滤器，不用装三份渲染器

设计口径（与 AGENTS.md「任何环节失败都降级为纯文本」一致）：
- **先整体 escape，再插入我自己生成的标签**。模型输出的任何 HTML（含 `<script>`）
  都只能以文字形态出现；`[文字](javascript:…)` 这类伪链接被 scheme 白名单拦掉
- 只支持 AI 实际会用到的语法：标题、粗斜体、行内代码、围栏代码块、有序/无序列表、
  引用、表格、链接与裸链自动链接、分隔线、段落内换行转 `<br>`
- 不追求 CommonMark 完备：越少的规则越少错判，读不懂的行原样输出成文字即可
"""
import re

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

# 链接 scheme 白名单：相对路径与锚点也允许（以 / # . 开头）
_SAFE_URL = re.compile(r'^(?:https?://|mailto:|[#/.])', re.I)
_UNSAFE_URL = re.compile(r'^\s*(?:javascript|data|vbscript|file)\s*:', re.I)

_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_HR = re.compile(r'^\s*(?:-{3,}|\*{3,}|_{3,})\s*$')
_QUOTE = re.compile(r'^&gt;\s?(.*)$')
_UL = re.compile(r'^(\s*)[-*+]\s+(.*)$')
_OL = re.compile(r'^(\s*)\d+[.)]\s+(.*)$')
_TABLE_SEP = re.compile(r'^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$')
_CODE_SPAN = re.compile(r'`([^`\n]+)`')
_MD_LINK = re.compile(r'\[([^\]\n]+)\]\(([^)\s]+)\)')
_BARE_URL = re.compile(r'(?<!["=>\w])(https?://[^\s<>"\']+)', re.I)


def _keep(store, html):
    """把已生成的片段藏进占位符，避免被后续内联规则二次加工

    占位符用 \\x01 包序号：输入里的 NUL 与 \\x01 在渲染前已被剥掉，不可能撞车。
    """
    store.append(html)
    return '\x01%d\x01' % (len(store) - 1)


def _link(url, text, store):
    if _UNSAFE_URL.match(url) or not _SAFE_URL.match(url):
        # 不安全/不认识的 scheme：连标记语法一起当普通文字输出，不生成 <a>
        return '[%s](%s)' % (text, url)
    return _keep(store, '<a class="md-link" href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                 % (url, text))


def _inline(text, store):
    """行内语法 → HTML。入参必须是**已转义**的文本"""
    # 1. 行内代码：内部一律不再解析（`**不是粗体**` 必须保持字面量）
    text = _CODE_SPAN.sub(lambda m: _keep(store, '<code class="md-code">%s</code>' % m.group(1)), text)
    # 2. [文字](链接)
    text = _MD_LINK.sub(lambda m: _link(m.group(2), m.group(1), store), text)
    # 3. 裸链自动链接（放在 2 之后，避免把已生成的 href 再包一层）
    text = _BARE_URL.sub(lambda m: _keep(store, '<a class="md-link md-break" href="%s" target="_blank" '
                                               'rel="noopener noreferrer">%s</a>' % (m.group(1), m.group(1))), text)
    # 4. 粗体 / 斜体
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'<em>\1</em>', text)
    return text


def _restore(text, store):
    return re.sub(r'\x01(\d+)\x01', lambda m: store[int(m.group(1))], text)


def _is_block_start(line):
    return bool(_HEADING.match(line) or _HR.match(line) or _QUOTE.match(line)
                or _UL.match(line) or _OL.match(line) or line.startswith('```'))


def _render_table(rows, store):
    """把 `| a | b |` 形式的行渲染成表格；分隔行决定表头边界

    单元格也要走行内解析：AI 经常在表格里写 **加粗** 与 `代码`。
    """
    def cells(line):
        return [c.strip() for c in line.strip().strip('|').split('|')]

    def row_html(cells_, tag):
        return '<tr>' + ''.join('<%s>%s</%s>'
                                % (tag, _restore(_inline(c, store), store), tag)
                                for c in cells_) + '</tr>'

    head = cells(rows[0])
    body = [cells(r) for r in rows[2:]] if len(rows) > 2 else []
    return ('<div class="md-table-wrap"><table class="md-table"><thead>%s</thead>'
            '<tbody>%s</tbody></table></div>'
            % (row_html(head, 'th'), ''.join(row_html(r, 'td') for r in body)))


def _render_list(lines, ordered, store):
    """有序/无序列表；按缩进两格一档支持嵌套（AI 回复里最多也就两层）"""
    html = []
    stack = []          # [(depth, tag)]
    item_re = _OL if ordered else _UL      # 写成 and/or 链容易选反（已因此死循环过一次）
    for line in lines:
        m = item_re.match(line)
        if not m:
            continue
        depth = len(m.group(1)) // 2
        item = _restore(_inline(m.group(2), store), store)
        while stack and stack[-1][0] > depth:
            html.append('</li></%s>' % stack[-1][1])
            stack.pop()
        if not stack or stack[-1][0] < depth:
            tag = 'ol' if ordered else 'ul'
            html.append('<%s class="md-list">' % tag)
            stack.append((depth, tag))
        else:
            html.append('</li>')
        html.append('<li>%s' % item)
    while stack:
        html.append('</li></%s>' % stack.pop()[1])
    return ''.join(html)


def render_markdown(text):
    """AI 文本 → 安全的 HTML（SafeString，模板里不需要再写 |safe）

    任何异常都退回转义后的纯文本：排版是锦上添花，绝不能因为它把整条消息弄没。
    """
    if not text:
        return mark_safe('')
    try:
        # NUL / \x01 会被当成占位符定界符，先剥掉；CRLF 归一
        src = str(text).replace('\x00', '').replace('\x01', '').replace('\r\n', '\n').rstrip()
        lines = escape(src).split('\n')       # ← 先转义：这一行就是全部 XSS 防线
        out = []
        store = []                            # 本次渲染的片段暂存（占位符指向它）
        i = 0
        prev_i = -1
        while i < len(lines):
            line = lines[i]
            # 防死循环守卫：放在循环顶部而不是各分支末尾 —— 带 continue 的分支会跳过
            # 末尾检查，而实测卡死的恰好就是列表分支（'- ' 单行上 i 永远不前进，
            # 会直接拖死一个 gunicorn worker）。宁可少排版也不能不返回。
            if i == prev_i:
                raise RuntimeError('markdown 行处理未推进：%r' % line[:40])
            prev_i = i

            if line.startswith('```'):
                buf = []
                i += 1
                while i < len(lines) and not lines[i].startswith('```'):
                    buf.append(lines[i])
                    i += 1
                i += 1                        # 跳过收尾 ```（没有收尾就走完文本，不报错）
                out.append('<pre class="md-pre"><code>%s</code></pre>' % '\n'.join(buf))
                continue

            if ('|' in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1])
                    and not _is_block_start(line)):
                rows = []
                while i < len(lines) and '|' in lines[i] and lines[i].strip():
                    rows.append(lines[i])
                    i += 1
                out.append(_render_table(rows, store))
                continue

            m = _HEADING.match(line)
            if m:
                # 只把 h1 夹到 h2，不做整体降级：模型在正文里习惯用 `##` 起头写小节，
                # 整体降一级会让真正的小节掉到 h3（字号跟正文差不了多少，读不出层级）
                level = min(max(len(m.group(1)), 2), 6)
                out.append('<h%d class="md-h">%s</h%d>'
                           % (level, _restore(_inline(m.group(2), store), store), level))
                i += 1
                continue

            if _HR.match(line):
                out.append('<hr class="md-hr">')
                i += 1
                continue

            if _QUOTE.match(line):
                buf = []
                while i < len(lines):
                    q = _QUOTE.match(lines[i])
                    if not q:
                        break
                    buf.append(q.group(1))
                    i += 1
                out.append('<blockquote class="md-quote">%s</blockquote>'
                           % _restore(_inline('<br>'.join(buf), store), store))
                continue

            if _UL.match(line) or _OL.match(line):
                ordered = bool(_OL.match(line))
                buf = []
                item_re = _OL if ordered else _UL      # 上一版选反了：无序行拿 _OL 匹配
                while i < len(lines) and (item_re.match(lines[i])
                                          or (lines[i].startswith('  ') and buf)):
                    buf.append(lines[i])
                    i += 1
                out.append(_render_list(buf, ordered, store))
                continue

            if not line.strip():
                i += 1
                continue

            buf = [line]
            i += 1
            # 带 | 的续行不当段落的一部分：宁可让下一轮循环自己判断是不是表格，
            # 也不要把表头行吞进上一段（代价：正文里孤立的「3|5 人」会被拆成两段，不丢内容）
            while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]) \
                    and '|' not in lines[i]:
                buf.append(lines[i])
                i += 1
            out.append('<p class="md-p">%s</p>'
                       % _restore(_inline('<br>'.join(buf), store), store))

        return mark_safe('\n'.join(out))
    except Exception:
        return mark_safe('<p class="md-p">%s</p>' % escape(str(text)))

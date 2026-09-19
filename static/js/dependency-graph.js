/* 活动依赖关系图：SVG 力导向布局
 *
 * 从 /activities/dependency-data/ API 获取 {nodes, edges}，
 * 纯 JS 实现力导向布局（初始位置按依赖深度分层，前置在左、后续在右），
 * 节点颜色按状态，点击跳转详情。
 *
 * 安全约定：SVG 一律 createElementNS + textContent 构建——活动名是用户
 * 数据，禁止用 innerHTML 拼字符串（用户名里带 < 或引号会注入）。
 * 颜色走 CSS 变量（var(--...)），SVG 属性支持，主题改动不用动这里。
 */
(function () {
    'use strict';

    var container = document.getElementById('dependency-graph');
    if (!container) return;

    var STATUS_COLORS = {
        planned: '#a1a1aa',
        in_progress: '#3b82f6',
        done: '#22c55e',
        cancelled: '#ef4444',
    };
    var STATUS_LABELS = {
        planned: '计划',
        in_progress: '进行中',
        done: '已完成',
        cancelled: '已取消',
    };
    var NS = 'http://www.w3.org/2000/svg';
    // 边端点在节点圆周上的留白，箭头不要扎进圆心
    var R = 13;
    var NAME_LIMIT = 8;

    fetch('/activities/dependency-data/')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (!data.nodes || data.nodes.length === 0) {
                container.textContent = '';
                var p = document.createElement('p');
                p.className = 'text-sm text-[var(--text-muted)] text-center py-8';
                p.textContent = '还没有配置活动依赖关系。';
                var hint = document.createElement('span');
                hint.className = 'block text-xs';
                hint.textContent = '在 AI 对话里说「创建活动 X，前置依赖 Y」即可建立依赖';
                p.appendChild(hint);
                container.appendChild(p);
                return;
            }
            render(data.nodes, data.edges);
        })
        .catch(function () {
            container.textContent = '';
            var p = document.createElement('p');
            p.className = 'text-sm text-red-500 text-center py-8';
            p.textContent = '加载失败';
            container.appendChild(p);
        });

    // 最长前置链深度（DAG 分层）：用于初始 x 坐标，力导向只做局部微调。
    // 比纯圆形初始分布收敛更快、成图「前置在左、后续在右」更易读。
    function depthMap(nodes, edges) {
        var depth = {};
        var incoming = {};
        nodes.forEach(function (n) { depth[n.id] = 0; incoming[n.id] = []; });
        edges.forEach(function (e) {
            if (incoming[e.to]) incoming[e.to].push(e.from);
        });
        // 拓扑序松弛（有环时靠迭代上限兜住，图不会死循环）
        for (var i = 0; i < nodes.length; i++) {
            var changed = false;
            edges.forEach(function (e) {
                if (depth[e.from] === undefined || depth[e.to] === undefined) return;
                if (depth[e.from] + 1 > depth[e.to]) {
                    depth[e.to] = depth[e.from] + 1;
                    changed = true;
                }
            });
            if (!changed) break;
        }
        return depth;
    }

    function render(nodes, edges) {
        var W = container.clientWidth || 600;
        var H = Math.max(360, Math.min(560, nodes.length * 46));

        var depth = depthMap(nodes, edges);
        var maxDepth = 0;
        nodes.forEach(function (n) { maxDepth = Math.max(maxDepth, depth[n.id] || 0); });

        // 初始位置：x 按深度分列，y 列内均匀散开
        var colCount = {};
        var colIndex = {};
        nodes.forEach(function (n) {
            var d = depth[n.id] || 0;
            colIndex[n.id] = colCount[d] || 0;
            colCount[d] = (colCount[d] || 0) + 1;
        });
        var padX = 60, padY = 46;
        var colGap = maxDepth > 0 ? (W - padX * 2) / maxDepth : 0;
        nodes.forEach(function (n) {
            var d = depth[n.id] || 0;
            var perCol = colCount[d] || 1;
            n.x = padX + colGap * d;
            n.y = padY + ((H - padY * 2) * (colIndex[n.id] + 0.5)) / perCol;
            n.vx = 0;
            n.vy = 0;
        });

        var nodeMap = {};
        nodes.forEach(function (n) { nodeMap[n.id] = n; });

        // 力导向微调（60 轮）：斥力防重叠，引力收边长，步长逐步衰减
        for (var iter = 0; iter < 60; iter++) {
            var decay = 1 - iter / 70;
            for (var i = 0; i < nodes.length; i++) {
                for (var j = i + 1; j < nodes.length; j++) {
                    var dx = nodes[j].x - nodes[i].x;
                    var dy = nodes[j].y - nodes[i].y;
                    var dist = Math.sqrt(dx * dx + dy * dy) || 1;
                    var force = 4500 / (dist * dist);
                    var fx = (dx / dist) * force;
                    var fy = (dy / dist) * force;
                    nodes[i].vx -= fx;
                    nodes[i].vy -= fy;
                    nodes[j].vx += fx;
                    nodes[j].vy += fy;
                }
            }
            edges.forEach(function (e) {
                var a = nodeMap[e.from];
                var b = nodeMap[e.to];
                if (!a || !b) return;
                var dx = b.x - a.x;
                var dy = b.y - a.y;
                var dist = Math.sqrt(dx * dx + dy * dy) || 1;
                var force = (dist - 120) * 0.02;
                a.vx += (dx / dist) * force;
                a.vy += (dy / dist) * force;
                b.vx -= (dx / dist) * force;
                b.vy -= (dy / dist) * force;
            });
            nodes.forEach(function (n) {
                n.x += n.vx * decay;
                n.y += n.vy * decay;
                n.vx *= 0.75;
                n.vy *= 0.75;
                n.x = Math.max(padX - 30, Math.min(W - padX + 30, n.x));
                n.y = Math.max(padY - 16, Math.min(H - padY + 16, n.y));
            });
        }

        var svg = document.createElementNS(NS, 'svg');
        svg.setAttribute('width', W);
        svg.setAttribute('height', H);
        svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
        svg.setAttribute('class', 'block mx-auto');
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', '活动依赖关系图');

        var defs = document.createElementNS(NS, 'defs');
        var marker = document.createElementNS(NS, 'marker');
        marker.setAttribute('id', 'dep-arrow');
        marker.setAttribute('viewBox', '0 0 10 10');
        marker.setAttribute('refX', '9');
        marker.setAttribute('refY', '5');
        marker.setAttribute('markerWidth', '6');
        marker.setAttribute('markerHeight', '6');
        marker.setAttribute('orient', 'auto');
        var arrowPath = document.createElementNS(NS, 'path');
        arrowPath.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
        arrowPath.setAttribute('fill', 'var(--border-strong, #a1a1aa)');
        marker.appendChild(arrowPath);
        defs.appendChild(marker);
        svg.appendChild(defs);

        // 边：前置已完成 → 淡虚线（该依赖已解锁），未完成 → 实线
        edges.forEach(function (e) {
            var a = nodeMap[e.from];
            var b = nodeMap[e.to];
            if (!a || !b) return;
            var dx = b.x - a.x;
            var dy = b.y - a.y;
            var dist = Math.sqrt(dx * dx + dy * dy) || 1;
            // 圆周收口，箭头贴着圆边而不是扎进圆心
            var x1 = a.x + (dx / dist) * R;
            var y1 = a.y + (dy / dist) * R;
            var x2 = b.x - (dx / dist) * (R + 2);
            var y2 = b.y - (dy / dist) * (R + 2);
            var line = document.createElementNS(NS, 'line');
            line.setAttribute('x1', x1);
            line.setAttribute('y1', y1);
            line.setAttribute('x2', x2);
            line.setAttribute('y2', y2);
            line.setAttribute('stroke', a.status === 'done'
                ? 'var(--border-strong, #d4d4d8)'
                : 'var(--text-muted, #52525b)');
            line.setAttribute('stroke-width', '1.5');
            if (a.status === 'done') line.setAttribute('stroke-dasharray', '4 3');
            line.setAttribute('marker-end', 'url(#dep-arrow)');
            svg.appendChild(line);
        });

        // 节点：圆 + 状态色 + 名称；SVG <title> 提供 hover 全名与可访问名
        nodes.forEach(function (n) {
            var color = STATUS_COLORS[n.status] || '#a1a1aa';
            var a = document.createElementNS(NS, 'a');
            a.setAttribute('href', '/activities/' + n.id + '/');
            a.setAttribute('aria-label', n.name + '（' + (STATUS_LABELS[n.status] || n.status) + '）');

            var title = document.createElementNS(NS, 'title');
            title.textContent = n.name + ' · ' + (STATUS_LABELS[n.status] || n.status);
            a.appendChild(title);

            var circle = document.createElementNS(NS, 'circle');
            circle.setAttribute('cx', n.x);
            circle.setAttribute('cy', n.y);
            circle.setAttribute('r', R - 1);
            circle.setAttribute('fill', color);
            circle.setAttribute('class', 'cursor-pointer hover:opacity-80');
            a.appendChild(circle);

            var text = document.createElementNS(NS, 'text');
            text.setAttribute('x', n.x);
            text.setAttribute('y', n.y + R + 11);
            text.setAttribute('text-anchor', 'middle');
            text.setAttribute('font-size', '11');
            text.setAttribute('fill', 'var(--text-secondary, #52525b)');
            text.textContent = n.name.length > NAME_LIMIT
                ? n.name.slice(0, NAME_LIMIT) + '…' : n.name;
            a.appendChild(text);
            svg.appendChild(a);
        });

        container.textContent = '';
        container.appendChild(svg);

        // 状态图例（节点颜色说明）
        var legend = document.createElement('div');
        legend.className = 'flex flex-wrap items-center justify-center gap-3 mt-3 text-xs text-[var(--text-muted)]';
        Object.keys(STATUS_COLORS).forEach(function (key) {
            var item = document.createElement('span');
            item.className = 'inline-flex items-center gap-1';
            var dot = document.createElement('span');
            dot.className = 'inline-block w-2.5 h-2.5 rounded-full';
            dot.style.backgroundColor = STATUS_COLORS[key];
            item.appendChild(dot);
            item.appendChild(document.createTextNode(STATUS_LABELS[key]));
            legend.appendChild(item);
        });
        container.appendChild(legend);
    }
})();

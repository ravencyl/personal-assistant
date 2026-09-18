/* 活动依赖关系图：SVG 力导向布局
 *
 * 从 /activities/dependency-data/ API 获取 {nodes, edges}，
 * 纯 JS 实现简易力导向布局，节点颜色按状态，点击跳转详情。
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

    fetch('/activities/dependency-data/')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (!data.nodes || data.nodes.length === 0) {
                container.innerHTML = '<p class="text-sm text-[var(--text-muted)] text-center py-8">还没有配置活动依赖关系。<br>' +
                    '<span class="text-xs">在 AI 对话里说「创建活动 X，前置依赖 Y」即可建立依赖</span></p>';
                return;
            }
            render(data.nodes, data.edges);
        })
        .catch(function (err) {
            container.innerHTML = '<p class="text-sm text-red-500 text-center py-8">加载失败</p>';
        });

    function render(nodes, edges) {
        var W = container.clientWidth || 600;
        var H = Math.max(400, nodes.length * 30);

        // 简易力导向布局初始化（圆形分布）
        var cx = W / 2, cy = H / 2, r = Math.min(W, H) * 0.35;
        nodes.forEach(function (n, i) {
            var angle = (2 * Math.PI * i) / nodes.length;
            n.x = cx + r * Math.cos(angle);
            n.y = cy + r * Math.sin(angle);
            n.vx = 0;
            n.vy = 0;
        });

        // 建立节点索引
        var nodeMap = {};
        nodes.forEach(function (n) { nodeMap[n.id] = n; });

        // 简易力模拟（50 轮迭代）
        for (var iter = 0; iter < 50; iter++) {
            // 斥力（节点间）
            for (var i = 0; i < nodes.length; i++) {
                for (var j = i + 1; j < nodes.length; j++) {
                    var dx = nodes[j].x - nodes[i].x;
                    var dy = nodes[j].y - nodes[i].y;
                    var dist = Math.sqrt(dx * dx + dy * dy) || 1;
                    var force = 5000 / (dist * dist);
                    var fx = (dx / dist) * force;
                    var fy = (dy / dist) * force;
                    nodes[i].vx -= fx;
                    nodes[i].vy -= fy;
                    nodes[j].vx += fx;
                    nodes[j].vy += fy;
                }
            }
            // 引力（边）
            edges.forEach(function (e) {
                var a = nodeMap[e.from];
                var b = nodeMap[e.to];
                if (!a || !b) return;
                var dx = b.x - a.x;
                var dy = b.y - a.y;
                var dist = Math.sqrt(dx * dx + dy * dy) || 1;
                var force = (dist - 100) * 0.01;
                var fx = (dx / dist) * force;
                var fy = (dy / dist) * force;
                a.vx += fx;
                a.vy += fy;
                b.vx -= fx;
                b.vy -= fy;
            });
            // 更新位置
            nodes.forEach(function (n) {
                n.x += n.vx * 0.1;
                n.y += n.vy * 0.1;
                n.vx *= 0.8;
                n.vy *= 0.8;
                // 边界约束
                n.x = Math.max(30, Math.min(W - 30, n.x));
                n.y = Math.max(30, Math.min(H - 30, n.y));
            });
        }

        // 生成 SVG
        var svg = '<svg width="' + W + '" height="' + H + '" class="block mx-auto">';
        // 箭头标记
        svg += '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#d4d4d8"/></marker></defs>';
        // 边
        edges.forEach(function (e) {
            var a = nodeMap[e.from];
            var b = nodeMap[e.to];
            if (!a || !b) return;
            svg += '<line x1="' + a.x + '" y1="' + a.y + '" x2="' + b.x + '" y2="' + b.y + '" stroke="#d4d4d8" stroke-width="1.5" marker-end="url(#arrow)"/>';
        });
        // 节点
        nodes.forEach(function (n) {
            var color = STATUS_COLORS[n.status] || '#a1a1aa';
            svg += '<a href="/activities/' + n.id + '/">' +
                '<circle cx="' + n.x + '" cy="' + n.y + '" r="12" fill="' + color + '" class="cursor-pointer hover:opacity-80"/>' +
                '<text x="' + n.x + '" y="' + (n.y + 24) + '" text-anchor="middle" font-size="11" fill="#52525b">' +
                (n.name.length > 8 ? n.name.slice(0, 8) + '...' : n.name) + '</text></a>';
        });
        svg += '</svg>';
        container.innerHTML = svg;
    }
})();

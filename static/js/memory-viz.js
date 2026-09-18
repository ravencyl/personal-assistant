/**
 * 记忆可视化面板：雷达图（分类分布）+ 时间线（月度趋势）+ 统计卡片
 * 纯 SVG + 原生 fetch，无外部依赖
 */
(function () {
    'use strict';

    var container = document.getElementById('memory-viz-container');
    if (!container) return;

    var statsUrl = container.dataset.statsUrl;
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrf = csrfMeta ? csrfMeta.content : '';

    var CATEGORY_LABELS = {
        preference: '偏好', fact: '事实', goal: '目标',
        relationship: '关系', habit: '习惯', other: '其他',
    };

    fetch(statsUrl, { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (data) { render(data); })
        .catch(function (err) { console.error('记忆统计加载失败:', err); });

    function render(data) {
        container.innerHTML = '';

        // 统计卡片行
        var summaryRow = document.createElement('div');
        summaryRow.className = 'grid grid-cols-3 gap-3 mb-6';
        var cards = [
            { value: data.summary.total, label: '总记忆数' },
            { value: data.summary.this_month, label: '本月新增' },
            { value: data.summary.high_importance, label: '高重要度 (≥8)' },
        ];
        cards.forEach(function (c) {
            var div = document.createElement('div');
            div.className = 'app-card rounded-xl p-4 text-center';
            div.innerHTML = '<div class="text-2xl font-bold text-[var(--text)]">' + c.value + '</div>' +
                '<div class="text-xs text-[var(--text-muted)] mt-1">' + c.label + '</div>';
            summaryRow.appendChild(div);
        });
        container.appendChild(summaryRow);

        // 图表行：雷达图 + 月度趋势
        var chartRow = document.createElement('div');
        chartRow.className = 'grid grid-cols-1 md:grid-cols-2 gap-4';

        chartRow.appendChild(buildRadarChart(data.categories));
        chartRow.appendChild(buildTimelineChart(data.monthly));

        container.appendChild(chartRow);
    }

    /** 雷达图：6 类记忆数量分布 */
    function buildRadarChart(categories) {
        var card = document.createElement('div');
        card.className = 'app-card rounded-xl p-4';

        var title = document.createElement('p');
        title.className = 'text-sm font-medium text-[var(--text)] mb-3';
        title.textContent = '分类分布';
        card.appendChild(title);

        var catMap = {};
        categories.forEach(function (c) { catMap[c.category] = c.count; });
        var keys = Object.keys(CATEGORY_LABELS);
        var values = keys.map(function (k) { return catMap[k] || 0; });
        var maxVal = Math.max.apply(null, values) || 1;

        var size = 220, cx = size / 2, cy = size / 2, r = 85;
        var svg = '<svg viewBox="0 0 ' + size + ' ' + size + '" class="w-full max-w-[220px] mx-auto">';

        // 背景网格（3 层）
        for (var ring = 1; ring <= 3; ring++) {
            var rr = r * ring / 3;
            var pts = [];
            for (var i = 0; i < keys.length; i++) {
                var angle = (Math.PI * 2 * i / keys.length) - Math.PI / 2;
                pts.push((cx + rr * Math.cos(angle)).toFixed(1) + ',' + (cy + rr * Math.sin(angle)).toFixed(1));
            }
            svg += '<polygon points="' + pts.join(' ') + '" fill="none" stroke="var(--border)" stroke-width="0.5"/>';
        }

        // 轴线
        for (var i = 0; i < keys.length; i++) {
            var angle = (Math.PI * 2 * i / keys.length) - Math.PI / 2;
            var ex = cx + r * Math.cos(angle), ey = cy + r * Math.sin(angle);
            svg += '<line x1="' + cx + '" y1="' + cy + '" x2="' + ex.toFixed(1) + '" y2="' + ey.toFixed(1) + '" stroke="var(--border)" stroke-width="0.5"/>';
        }

        // 数据多边形
        var dataPts = [];
        for (var i = 0; i < keys.length; i++) {
            var angle = (Math.PI * 2 * i / keys.length) - Math.PI / 2;
            var dr = r * (values[i] / maxVal);
            dataPts.push((cx + dr * Math.cos(angle)).toFixed(1) + ',' + (cy + dr * Math.sin(angle)).toFixed(1));
        }
        svg += '<polygon points="' + dataPts.join(' ') + '" fill="var(--accent)" fill-opacity="0.15" stroke="var(--accent)" stroke-width="1.5"/>';

        // 标签
        for (var i = 0; i < keys.length; i++) {
            var angle = (Math.PI * 2 * i / keys.length) - Math.PI / 2;
            var lx = cx + (r + 18) * Math.cos(angle);
            var ly = cy + (r + 18) * Math.sin(angle);
            var anchor = Math.abs(Math.cos(angle)) < 0.1 ? 'middle' : (Math.cos(angle) > 0 ? 'start' : 'end');
            svg += '<text x="' + lx.toFixed(1) + '" y="' + (ly + 3).toFixed(1) + '" text-anchor="' + anchor + '" class="text-[9px]" fill="var(--text-secondary)">' + CATEGORY_LABELS[keys[i]] + '</text>';
            if (values[i] > 0) {
                svg += '<text x="' + lx.toFixed(1) + '" y="' + (ly + 13).toFixed(1) + '" text-anchor="' + anchor + '" class="text-[8px]" fill="var(--text-muted)">' + values[i] + '</text>';
            }
        }

        svg += '</svg>';
        card.innerHTML += svg;
        return card;
    }

    /** 月度趋势柱状图 */
    function buildTimelineChart(monthly) {
        var card = document.createElement('div');
        card.className = 'app-card rounded-xl p-4';

        var title = document.createElement('p');
        title.className = 'text-sm font-medium text-[var(--text)] mb-3';
        title.textContent = '月度趋势（近 6 个月）';
        card.appendChild(title);

        var maxCount = Math.max.apply(null, monthly.map(function (m) { return m.count; })) || 1;
        var barW = 28, gap = 12, chartH = 120;
        var totalW = monthly.length * (barW + gap) - gap;

        var svg = '<svg viewBox="0 0 ' + (totalW + 40) + ' ' + (chartH + 30) + '" class="w-full">';

        monthly.forEach(function (m, i) {
            var x = i * (barW + gap) + 20;
            var barH = Math.max(2, (m.count / maxCount) * chartH);
            var y = chartH - barH;

            svg += '<rect x="' + x + '" y="' + y + '" width="' + barW + '" height="' + barH + '" rx="4" fill="var(--accent)" opacity="0.7"/>';
            if (m.count > 0) {
                svg += '<text x="' + (x + barW / 2) + '" y="' + (y - 4) + '" text-anchor="middle" class="text-[9px]" fill="var(--text-secondary)">' + m.count + '</text>';
            }
            svg += '<text x="' + (x + barW / 2) + '" y="' + (chartH + 14) + '" text-anchor="middle" class="text-[9px]" fill="var(--text-muted)">' + m.month.slice(5) + '月</text>';
        });

        svg += '</svg>';
        card.innerHTML += svg;
        return card;
    }
})();

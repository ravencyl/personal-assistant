/**
 * 日历日视图：时间线样式展示当日活动
 *
 * 布局列位（与 activity_calendar.html 日视图模板保持一致，改一处必须同步另一处）：
 *   时间列 0-48px（右对齐）· 节点圆点中心 70px · 内容 86px 起
 * - 「现在」指示行按时间顺序插到正确位置（仅今天，data-show-now）
 * - 无活动时保留模板自带的空状态，不清空列表
 */
(function () {
    var timeline = document.getElementById('day-timeline');
    if (!timeline) return;

    var dayDate = timeline.dataset.dayDate;
    var listEl = document.getElementById('day-activity-list');
    var apiUrl = timeline.dataset.apiUrl;
    var showNow = timeline.dataset.showNow === '1';

    // 列位常量：时间列 / 圆点（圆点中心 70px = left 65px + 半径 5px，
    // top-4 使圆点中心 21px，与卡片首行文字对齐）
    var TIME_COL = 'absolute left-0 top-[11px] w-12 text-right text-[11px] leading-5 tabular-nums';
    var DOT_CLS = 'absolute left-[65px] top-4 w-2.5 h-2.5 rounded-full ring-4 ring-[var(--bg-card)]';

    function pad(n) { return n < 10 ? '0' + n : '' + n; }

    function formatTime(t) {
        var p = t.split(':');
        return p[0] + ':' + p[1];
    }

    // 单条活动行：时间列 + 圆点 + 内容卡片（名称 + 状态徽章，第二行放标签）
    function buildItem(a, isLast) {
        var li = document.createElement('li');
        li.className = 'relative' + (isLast ? '' : ' pb-4');

        // 时间列：有起止时间的堆叠两行（起始加粗，结束淡色小字），否则「全天」
        var time = document.createElement('span');
        time.className = TIME_COL;
        if (a.start_time) {
            time.classList.add('text-[var(--text)]', 'font-medium');
            time.textContent = formatTime(a.start_time);
            if (a.end_time) {
                var end = document.createElement('span');
                end.className = 'block text-[10px] font-normal text-[var(--text-muted)]';
                end.textContent = formatTime(a.end_time);
                time.appendChild(end);
            }
        } else {
            time.classList.add('text-[var(--text-muted)]');
            time.textContent = '全天';
        }
        li.appendChild(time);

        var dot = document.createElement('span');
        dot.className = DOT_CLS + ' status-bg--' + a.status;
        li.appendChild(dot);

        var card = document.createElement('a');
        card.href = a.url;
        card.className = 'ml-[86px] block rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-3.5 py-2.5 transition-colors hover:border-[var(--accent)]';

        var row1 = document.createElement('div');
        row1.className = 'flex items-center gap-2';
        var name = document.createElement('span');
        name.className = 'flex-1 min-w-0 truncate text-sm font-medium text-[var(--text)]';
        name.textContent = a.name;
        name.title = a.name;
        row1.appendChild(name);
        var status = document.createElement('span');
        status.className = 'shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium status-bg--' + a.status + ' status-fg-on--' + a.status;
        status.textContent = a.status_label;
        row1.appendChild(status);
        card.appendChild(row1);

        if (a.tags && a.tags.length > 0) {
            var row2 = document.createElement('div');
            row2.className = 'flex flex-wrap items-center gap-1.5 mt-1.5';
            a.tags.forEach(function (tag) {
                var tagSpan = document.createElement('span');
                tagSpan.className = 'rounded-full bg-[var(--accent-light)] px-2 py-0.5 text-[10px] font-medium text-[var(--accent)]';
                tagSpan.textContent = tag;
                row2.appendChild(tagSpan);
            });
            card.appendChild(row2);
        }

        li.appendChild(card);
        return li;
    }

    // 「现在」指示行：红色时间 + 红点 + 「现在」，占位结构与活动行一致
    function buildNowRow(timeText) {
        var li = document.createElement('li');
        li.className = 'relative';

        var time = document.createElement('span');
        time.className = TIME_COL + ' font-medium text-red-500';
        time.textContent = timeText;
        li.appendChild(time);

        var dot = document.createElement('span');
        dot.className = DOT_CLS + ' bg-red-500';
        li.appendChild(dot);

        var label = document.createElement('span');
        label.className = 'ml-[86px] block py-2.5 text-[11px] font-medium leading-5 text-red-500';
        label.textContent = '现在';
        li.appendChild(label);

        return li;
    }

    function renderDayActivities(activities) {
        // 过滤出当天范围内的活动（start_date <= dayDate <= end_date，跨天活动也要出现）
        var dayActivities = activities.filter(function (a) {
            return a.start_date <= dayDate && a.end_date >= dayDate;
        });

        // 按开始时间排序（无时间的排最后）
        dayActivities.sort(function (a, b) {
            var aTime = a.start_time || '23:59';
            var bTime = b.start_time || '23:59';
            return aTime.localeCompare(bTime);
        });

        if (dayActivities.length === 0) {
            return;  // 模板自带的空状态保持原样
        }

        // 「现在」行的插入位置：第一条开始时间晚于当前时间的活动之前；
        // 都早于当前时间则放在全部活动之后
        var now = new Date();
        var nowStr = pad(now.getHours()) + ':' + pad(now.getMinutes());
        var nowIdx = dayActivities.length;
        if (showNow) {
            for (var i = 0; i < dayActivities.length; i++) {
                var st = dayActivities[i].start_time;
                if (st && st.slice(0, 5) > nowStr) {
                    nowIdx = i;
                    break;
                }
            }
        }

        listEl.innerHTML = '';
        dayActivities.forEach(function (a, index) {
            if (index === nowIdx) {
                listEl.appendChild(buildNowRow(nowStr));
            }
            listEl.appendChild(buildItem(a, index === dayActivities.length - 1 && nowIdx !== index + 1));
        });
        if (nowIdx === dayActivities.length) {
            listEl.appendChild(buildNowRow(nowStr));
        }
    }

    fetch(apiUrl + '?mode=day&date=' + dayDate)
        .then(function (r) { return r.json(); })
        .then(function (data) { renderDayActivities(data.activities || []); })
        .catch(function (err) { console.error('加载日历数据失败:', err); });
})();

/**
 * 用户时区自动检测与手动切换
 *
 * - 页面加载时自动检测浏览器时区，POST 到后端存入 session
 * - 导航栏显示时区缩写 chip，点击弹出选择器可手动切换
 * - 选择器按大洲分组，支持搜索过滤
 */
(function () {
    var csrf = document.querySelector('meta[name="csrf-token"]');
    if (!csrf) return;
    csrf = csrf.content;

    var SET_URL = '/api/timezone/set/';
    var GET_URL = '/api/timezone/';
    var currentTz = '';
    var allTimezones = [];
    var dropdown = null;

    // 检测浏览器时区（Intl API，所有现代浏览器支持）
    function detectTimezone() {
        try {
            return Intl.DateTimeFormat().resolvedOptions().timeZone;
        } catch (e) {
            return null;
        }
    }

    // 从时区名提取缩写（如 Asia/Shanghai → CST, America/New_York → EDT）
    function tzAbbrev(tzName) {
        if (!tzName) return '';
        try {
            var parts = new Intl.DateTimeFormat('en-US', {
                timeZone: tzName,
                timeZoneName: 'short'
            }).formatToParts(new Date());
            var tzPart = parts.find(function (p) { return p.type === 'timeZoneName'; });
            return tzPart ? tzPart.value : tzName.split('/').pop();
        } catch (e) {
            return tzName.split('/').pop();
        }
    }

    // 发送时区到后端
    function sendTimezone(tz) {
        if (!tz) return;
        fetch(SET_URL, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrf,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ timezone: tz })
        }).then(function (r) {
            if (r.ok) {
                currentTz = tz;
                updateIndicator(tz);
            }
        }).catch(function () { /* 静默失败 */ });
    }

    // 更新导航栏时区指示器
    function updateIndicator(tz) {
        var el = document.getElementById('tz-indicator');
        if (el) {
            el.textContent = tzAbbrev(tz);
            el.title = '点击切换时区：' + tz;
            el.classList.remove('hidden');
            el.style.cursor = 'pointer';
        }
    }

    // 按大洲分组
    function groupByRegion(tzs) {
        var groups = {};
        tzs.forEach(function (tz) {
            var region = tz.split('/')[0] || 'Other';
            var map = { Asia: '亚洲', Europe: '欧洲', America: '美洲', Africa: '非洲',
                        Australia: '大洋洲', Pacific: '太平洋', UTC: '通用' };
            var label = map[region] || region;
            if (!groups[label]) groups[label] = [];
            groups[label].push(tz);
        });
        return groups;
    }

    // 创建/切换下拉选择器
    function toggleDropdown() {
        if (dropdown) {
            dropdown.remove();
            dropdown = null;
            return;
        }

        dropdown = document.createElement('div');
        dropdown.id = 'tz-dropdown';
        dropdown.className = 'fixed z-50 bg-[var(--bg-card)] border border-[var(--border-strong)] rounded-xl shadow-lg overflow-hidden';
        dropdown.style.cssText = 'top: 48px; right: 16px; width: 260px; max-height: 400px;';

        // 搜索框
        var searchWrap = document.createElement('div');
        searchWrap.className = 'p-2 border-b border-[var(--border)]';
        var searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.placeholder = '搜索时区…';
        searchInput.className = 'w-full px-2 py-1.5 text-sm rounded-lg border border-[var(--border)] bg-transparent text-[var(--text)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)]';
        searchWrap.appendChild(searchInput);
        dropdown.appendChild(searchWrap);

        // 时区列表容器
        var listWrap = document.createElement('div');
        listWrap.className = 'overflow-y-auto';
        listWrap.style.maxHeight = '320px';
        dropdown.appendChild(listWrap);

        function renderList(filter) {
            listWrap.innerHTML = '';
            var filtered = filter
                ? allTimezones.filter(function (tz) { return tz.toLowerCase().indexOf(filter.toLowerCase()) >= 0; })
                : allTimezones;
            var groups = groupByRegion(filtered);
            var order = ['亚洲', '欧洲', '美洲', '大洋洲', '太平洋', '非洲', '通用'];
            order.forEach(function (region) {
                var tzs = groups[region];
                if (!tzs || !tzs.length) return;
                var header = document.createElement('div');
                header.className = 'px-3 py-1 text-[10px] font-medium text-[var(--text-muted)] uppercase bg-[var(--accent-light)]/50';
                header.textContent = region;
                listWrap.appendChild(header);
                tzs.forEach(function (tz) {
                    var item = document.createElement('button');
                    item.type = 'button';
                    item.className = 'w-full text-left px-3 py-1.5 text-sm hover:bg-[var(--accent-light)] transition-colors flex items-center justify-between' +
                        (tz === currentTz ? ' text-[var(--accent)] font-medium' : ' text-[var(--text)]');
                    var nameSpan = document.createElement('span');
                    nameSpan.textContent = tz.split('/').pop().replace(/_/g, ' ');
                    var abbrevSpan = document.createElement('span');
                    abbrevSpan.className = 'text-[10px] text-[var(--text-muted)] font-mono';
                    abbrevSpan.textContent = tzAbbrev(tz);
                    item.appendChild(nameSpan);
                    item.appendChild(abbrevSpan);
                    item.addEventListener('click', function () {
                        sendTimezone(tz);
                        toggleDropdown();
                    });
                    listWrap.appendChild(item);
                });
            });
        }

        renderList('');
        searchInput.addEventListener('input', function () { renderList(searchInput.value); });
        searchInput.focus();

        document.body.appendChild(dropdown);

        // 点击外部关闭
        setTimeout(function () {
            document.addEventListener('click', closeOnOutsideClick);
        }, 0);
    }

    function closeOnOutsideClick(e) {
        if (dropdown && !dropdown.contains(e.target) && e.target.id !== 'tz-indicator') {
            dropdown.remove();
            dropdown = null;
            document.removeEventListener('click', closeOnOutsideClick);
        }
    }

    // 从后端获取已保存的时区和可选列表（返回 Promise 以便链式调用）
    function loadTimezone() {
        return fetch(GET_URL, {
            headers: { 'Accept': 'application/json' }
        }).then(function (r) {
            return r.json();
        }).then(function (data) {
            if (data.timezone) {
                currentTz = data.timezone;
                updateIndicator(data.timezone);
            }
            if (data.valid_timezones) {
                allTimezones = data.valid_timezones;
            }
        }).catch(function () { /* 静默失败 */ });
    }

    // 绑定点击事件
    function bindToggle() {
        var el = document.getElementById('tz-indicator');
        if (el) {
            el.addEventListener('click', function (e) {
                e.stopPropagation();
                toggleDropdown();
            });
        }
    }

    // 初始化：先加载已保存的时区，仅在检测值与已保存值不同时才 POST
    loadTimezone().then(function () {
        var detected = detectTimezone();
        if (detected && detected !== currentTz) {
            sendTimezone(detected);
        }
    });

    // DOM 就绪后绑定（因为 indicator 可能在 defer 脚本之后才渲染）
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindToggle);
    } else {
        bindToggle();
    }
    // HTMX 替换后重新绑定
    document.addEventListener('htmx:afterSwap', bindToggle);

    // 暴露给全局
    window.paTimezone = {
        detect: detectTimezone,
        abbrev: tzAbbrev,
        update: updateIndicator,
        toggle: toggleDropdown
    };
})();

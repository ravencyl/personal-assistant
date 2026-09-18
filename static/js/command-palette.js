/**
 * 全局命令面板（Cmd+K / Ctrl+K）
 * 
 * 功能：
 * - 桌面端 Cmd+K / Ctrl+K 唤起，移动端需点击触发（后续可扩展）
 * - 输入即搜索（复用全局搜索 API）
 * - 快捷命令：/新建、/报告、/行动、/日历、/归档、/对话、/备忘、/知识
 * - Esc 关闭，点击遮罩关闭
 * - 结果项键盘导航（↑↓）+ Enter 选中
 */
(function () {
    'use strict';

    // ── 快捷命令定义 ──
    var COMMANDS = [
        { keys: ['/新建', '/创建', '/new'], label: '新建活动', url: '/activities/new/', icon: '+' },
        { keys: ['/工作台', '/today', '/今天'], label: '今日工作台', url: '/today/', icon: '🏠' },
        { keys: ['/报告', '/周报', '/report'], label: '本周周报', url: '/reports/weekly/', icon: '📊' },
        { keys: ['/回顾', '/review'], label: '每周回顾', url: '/weekly-review/', icon: '🔍' },
        { keys: ['/行动', '/下一步', '/actions'], label: '下一步行动', url: '/activities/next-actions/', icon: '→' },
        { keys: ['/时间线', '/timeline'], label: '活动时间线', url: '/activities/timeline/', icon: '🕐' },
        { keys: ['/日历', '/calendar'], label: '活动日历', url: '/activities/calendar/', icon: '📅' },
        { keys: ['/归档', '/archive'], label: '已归档活动', url: '/activities/archive/', icon: '📦' },
        { keys: ['/对话', '/chat'], label: 'AI 对话', url: '/chat/', icon: '💬' },
        { keys: ['/备忘', '/note'], label: '备忘录', url: '/notes/', icon: '📝' },
        { keys: ['/知识', '/knowledge'], label: '知识库', url: '/knowledge/', icon: '📚' },
        { keys: ['/记忆', '/memory'], label: '记忆管理', url: '/memory/', icon: '🧠' },
        { keys: ['/仪表盘', '/dashboard'], label: '仪表盘', url: '/dashboard/', icon: '📈' },
    ];

    var panel, input, results, overlay;
    var selectedIndex = -1;
    var currentResults = [];
    var searchTimer = null;

    function init() {
        // 创建面板 DOM
        panel = document.getElementById('command-palette');
        if (!panel) {
            createPanel();
            panel = document.getElementById('command-palette');
            overlay = document.getElementById('command-palette-overlay');
            input = document.getElementById('command-palette-input');
            results = document.getElementById('command-palette-results');
        }

        // 键盘快捷键
        document.addEventListener('keydown', function (e) {
            // Cmd+K (Mac) / Ctrl+K (Win/Linux)
            if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                e.preventDefault();
                toggle();
            }
            // Esc 关闭
            if (e.key === 'Escape' && panel && !panel.classList.contains('hidden')) {
                close();
            }
        });
    }

    function createPanel() {
        var html = '' +
            '<div id="command-palette-overlay" class="fixed inset-0 z-[60] hidden" style="background: rgba(0,0,0,0.4); backdrop-filter: blur(4px);">' +
            '</div>' +
            '<div id="command-palette" class="fixed inset-x-0 top-[15vh] z-[61] hidden mx-auto max-w-lg px-4">' +
                '<div class="bg-[var(--bg-card)] rounded-2xl shadow-[var(--shadow-lg)] border border-[var(--border)] overflow-hidden">' +
                    '<div class="flex items-center gap-3 px-4 py-3 border-b border-[var(--border)]">' +
                        '<svg class="w-5 h-5 text-[var(--text-muted)] shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">' +
                            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>' +
                        '</svg>' +
                        '<input id="command-palette-input" type="text" placeholder="输入命令或搜索... (/新建 /报告 /行动)" ' +
                            'class="flex-1 text-sm bg-transparent outline-none text-[var(--text)] placeholder:text-[var(--text-muted)]" autocomplete="off">' +
                        '<kbd class="hidden sm:inline text-[10px] text-[var(--text-muted)] border border-[var(--border)] rounded px-1.5 py-0.5">Esc</kbd>' +
                    '</div>' +
                    '<div id="command-palette-results" class="max-h-[50vh] overflow-y-auto p-2"></div>' +
                '</div>' +
            '</div>';
        
        var container = document.createElement('div');
        container.innerHTML = html;
        // 添加到 body
        while (container.firstChild) {
            document.body.appendChild(container.firstChild);
        }

        // 事件绑定
        overlay = document.getElementById('command-palette-overlay');
        input = document.getElementById('command-palette-input');
        results = document.getElementById('command-palette-results');

        overlay.addEventListener('click', close);
        input.addEventListener('input', onInput);
        input.addEventListener('keydown', onKeydown);
    }

    function toggle() {
        if (panel.classList.contains('hidden')) {
            open();
        } else {
            close();
        }
    }

    function open() {
        overlay.classList.remove('hidden');
        panel.classList.remove('hidden');
        input.value = '';
        input.focus();
        selectedIndex = -1;
        renderCommands('');
    }

    function close() {
        overlay.classList.add('hidden');
        panel.classList.add('hidden');
        input.value = '';
        currentResults = [];
    }

    function onInput() {
        var query = input.value.trim();
        
        // 清除之前的定时器
        if (searchTimer) clearTimeout(searchTimer);
        
        // 快捷命令立即过滤
        if (query.startsWith('/')) {
            renderCommands(query);
            return;
        }
        
        // 搜索需要防抖
        searchTimer = setTimeout(function () {
            if (query.length >= 2) {
                doSearch(query);
            } else {
                renderCommands('');
            }
        }, 200);
    }

    function onKeydown(e) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            moveSelection(1);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            moveSelection(-1);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            selectCurrent();
        }
    }

    function moveSelection(delta) {
        if (!currentResults.length) return;
        selectedIndex = (selectedIndex + delta + currentResults.length) % currentResults.length;
        updateSelection();
    }

    function updateSelection() {
        var items = results.querySelectorAll('[data-cmd-index]');
        items.forEach(function (item, i) {
            if (i === selectedIndex) {
                item.classList.add('bg-[var(--accent-light)]');
                item.scrollIntoView({ block: 'nearest' });
            } else {
                item.classList.remove('bg-[var(--accent-light)]');
            }
        });
    }

    function selectCurrent() {
        if (selectedIndex >= 0 && selectedIndex < currentResults.length) {
            var item = currentResults[selectedIndex];
            if (item.url) {
                close();
                location.href = item.url;
            }
        }
    }

    function renderCommands(query) {
        var filtered = COMMANDS;
        if (query) {
            var q = query.toLowerCase();
            filtered = COMMANDS.filter(function (cmd) {
                // 匹配命令键或标签
                return cmd.keys.some(function (k) { return k.indexOf(q) !== -1; }) ||
                       cmd.label.toLowerCase().indexOf(q) !== -1;
            });
        }

        currentResults = filtered.map(function (cmd) {
            return { label: cmd.label, url: cmd.url, icon: cmd.icon, type: 'command' };
        });

        renderResults();
    }

    function doSearch(query) {
        // 先展示命令
        renderCommands(query);
        
        // 再异步搜索
        fetch('/api/search/?q=' + encodeURIComponent(query), {
            headers: { 'Accept': 'application/json' }
        }).then(function (r) { return r.json(); }).then(function (data) {
            var searchResults = [];
            
            // 解析搜索结果
            if (data.results) {
                Object.keys(data.results).forEach(function (category) {
                    var items = data.results[category] || [];
                    items.slice(0, 3).forEach(function (item) {
                        searchResults.push({
                            label: item.name || item.title || item.content?.substring(0, 40),
                            url: item.url || item.detail_url,
                            icon: getCategoryIcon(category),
                            type: 'search',
                            category: category
                        });
                    });
                });
            }
            
            // 合并命令和搜索结果
            currentResults = currentResults.concat(searchResults);
            renderResults();
        }).catch(function () {
            // 搜索失败时只保留命令
        });
    }

    function getCategoryIcon(cat) {
        var icons = {
            'activities': '📋',
            'knowledge': '📚',
            'notes': '📝',
            'chat': '💬'
        };
        return icons[cat] || '🔍';
    }

    function renderResults() {
        if (!currentResults.length) {
            results.innerHTML = '<p class="text-xs text-[var(--text-muted)] px-3 py-4 text-center">没有匹配的命令或搜索结果</p>';
            return;
        }

        var html = '';
        currentResults.forEach(function (item, i) {
            var selected = i === selectedIndex ? 'bg-[var(--accent-light)]' : '';
            var badge = item.type === 'command' ? '<span class="text-[9px] text-[var(--text-muted)] bg-zinc-100 rounded px-1 py-0.5 ml-auto">命令</span>' : '';
            
            html += '<a href="' + escapeHtml(item.url) + '" data-cmd-index="' + i + '" ' +
                'class="flex items-center gap-3 px-3 py-2.5 rounded-lg hover:bg-[var(--accent-light)] transition-colors ' + selected + '">' +
                '<span class="w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center text-sm shrink-0">' + (item.icon || '🔍') + '</span>' +
                '<span class="text-sm text-[var(--text)] truncate">' + escapeHtml(item.label) + '</span>' +
                badge +
            '</a>';
        });
        results.innerHTML = html;
    }

    function escapeHtml(str) {
        if (!str) return '';
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    }

    // 初始化
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // 暴露全局 API 供外部调用
    window.paCommandPalette = {
        open: open,
        close: close,
        toggle: toggle
    };
})();

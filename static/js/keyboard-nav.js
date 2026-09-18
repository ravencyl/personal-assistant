/**
 * 全局键盘导航：j/k 选择、Enter 打开、Esc 返回、g+h/a/k/n/c 快速跳转
 *
 * 只在列表页（活动列表、知识列表、笔记列表、记忆列表）生效。
 * 焦点环通过 .kb-focus CSS 类实现。
 */
(function () {
    'use strict';

    // 可导航的列表页选择器
    var LIST_SELECTORS = [
        '.app-card',           // 通用卡片（活动、记忆、笔记等）
        'article',             // 文章列表
    ];

    var focusClass = 'kb-focus';
    var items = [];
    var currentIndex = -1;
    var pendingG = false;
    var pendingTimer = null;

    function getItems() {
        var result = [];
        LIST_SELECTORS.forEach(function (sel) {
            document.querySelectorAll(sel).forEach(function (el) {
                // 排除隐藏元素和嵌套卡片
                if (el.offsetParent !== null && !el.closest('.kb-focus')) {
                    result.push(el);
                }
            });
        });
        return result;
    }

    function setFocus(index) {
        // 清除旧焦点
        items.forEach(function (el) { el.classList.remove(focusClass); });

        if (index < 0 || index >= items.length) {
            currentIndex = -1;
            return;
        }

        currentIndex = index;
        items[currentIndex].classList.add(focusClass);
        items[currentIndex].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    function openCurrent() {
        if (currentIndex < 0 || currentIndex >= items.length) return;
        var el = items[currentIndex];
        var link = el.querySelector('a[href]') || el.closest('a[href]');
        if (link) {
            window.location.href = link.href;
        }
    }

    // 路由映射：g 然后按的键
    var G_ROUTES = {
        'h': '/',                // home
        'a': '/activities/',     // activities
        'k': '/knowledge/',      // knowledge
        'n': '/notes/',          // notes
        'c': '/chat/',           // chat
    };

    document.addEventListener('keydown', function (e) {
        // 忽略输入框内的按键
        var tag = e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target.isContentEditable) {
            return;
        }

        // g 前缀快捷键
        if (pendingG) {
            clearTimeout(pendingTimer);
            pendingG = false;
            var route = G_ROUTES[e.key.toLowerCase()];
            if (route) {
                e.preventDefault();
                window.location.href = route;
                return;
            }
            return;
        }

        switch (e.key) {
            case 'j': // 下一项
                e.preventDefault();
                if (!items.length) items = getItems();
                setFocus(Math.min(currentIndex + 1, items.length - 1));
                break;

            case 'k': // 上一项
                e.preventDefault();
                if (!items.length) items = getItems();
                setFocus(Math.max(currentIndex - 1, 0));
                break;

            case 'Enter': // 打开选中项
                e.preventDefault();
                openCurrent();
                break;

            case 'Escape': // 返回/清除焦点
                e.preventDefault();
                if (currentIndex >= 0) {
                    setFocus(-1);
                } else if (window.history.length > 1) {
                    window.history.back();
                }
                break;

            case 'g': // g 前缀开始
                pendingG = true;
                pendingTimer = setTimeout(function () { pendingG = false; }, 1000);
                break;
        }
    });

    // 页面内容变化时（HTMX 刷新等）重新获取列表项
    document.addEventListener('htmx:afterSwap', function () {
        items = getItems();
        currentIndex = -1;
    });
})();

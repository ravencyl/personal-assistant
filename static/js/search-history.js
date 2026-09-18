/**
 * 搜索历史 & 自动补全
 *
 * localStorage 存最近 20 条搜索词（key: pa_search_history）；
 * 搜索输入框聚焦且为空时展示历史记录；提交搜索时追加到历史（去重）。
 * 点击历史条目直接触发搜索（填充 + 触发 HTMX）。
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'pa_search_history';
  var MAX_HISTORY = 20;

  function getHistory() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function saveHistory(history) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(history.slice(0, MAX_HISTORY)));
    } catch (e) {
      // localStorage 满或不可用：静默忽略
    }
  }

  function addToHistory(query) {
    var q = (query || '').trim();
    if (!q) return;
    var history = getHistory();
    // 去重：移除已存在的同值
    var idx = history.indexOf(q);
    if (idx !== -1) history.splice(idx, 1);
    // 新条目插到最前
    history.unshift(q);
    saveHistory(history);
  }

  function renderHistory(container, input) {
    var history = getHistory();
    if (!history.length) return;

    var html = '<div class="space-y-1">' +
      '<div class="flex items-center justify-between px-1 mb-2">' +
      '<p class="text-xs text-[var(--text-muted)]">搜索历史</p>' +
      '<button type="button" id="clear-search-history" class="text-xs text-[var(--text-muted)] hover:text-[var(--text)] transition-colors">清除</button>' +
      '</div>';

    history.forEach(function (q) {
      html += '<button type="button" class="search-history-item search-result-item w-full flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-zinc-50 transition-colors text-left" data-query="' +
        q.replace(/"/g, '&quot;') + '">' +
        '<span class="w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center text-sm shrink-0">🕐</span>' +
        '<span class="text-sm text-[var(--text)] truncate">' +
        q.replace(/</g, '&lt;').replace(/>/g, '&gt;') +
        '</span></button>';
    });

    html += '</div>';
    container.innerHTML = html;

    // 点击历史条目：填充输入框并触发搜索
    container.querySelectorAll('.search-history-item').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var query = this.getAttribute('data-query');
        input.value = query;
        // 触发 HTMX 搜索
        if (window.htmx) {
          htmx.trigger(input, 'keyup');
        }
      });
    });

    // 清除历史
    var clearBtn = document.getElementById('clear-search-history');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        saveHistory([]);
        // 恢复快捷操作
        container.innerHTML = '<div class="space-y-1">' +
          '<p class="text-xs text-[var(--text-muted)] px-1 mb-2">快捷操作</p>' +
          '<a href="/activities/new/" class="search-result-item flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-zinc-50 transition-colors">' +
          '<span class="w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center text-sm">+</span>' +
          '<span class="text-sm text-[var(--text)]">新建活动</span></a>' +
          '<a href="/notes/" class="search-result-item flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-zinc-50 transition-colors">' +
          '<span class="w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center text-sm">📝</span>' +
          '<span class="text-sm text-[var(--text)]">新建笔记</span></a></div>';
      });
    }
  }

  // 初始化
  function init() {
    var input = document.getElementById('search-input');
    var results = document.getElementById('search-results');
    if (!input || !results) return;

    // 搜索提交时保存到历史：监听 HTMX afterRequest 事件
    input.addEventListener('htmx:afterRequest', function (evt) {
      if (input.value.trim()) {
        addToHistory(input.value);
      }
    });

    // 输入框聚焦且为空时展示搜索历史
    input.addEventListener('focus', function () {
      if (!input.value.trim()) {
        renderHistory(results, input);
      }
    });

    // 输入内容变化时：清空后恢复历史/快捷操作
    input.addEventListener('input', function () {
      if (!input.value.trim()) {
        renderHistory(results, input);
      }
    });
  }

  // DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

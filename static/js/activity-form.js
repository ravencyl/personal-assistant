/* 活动表单组件：标签/参与者 chips 输入（拼音联想）+ 具体时间开合。
 *
 * 消费方：
 * - templates/activities/activity_form.html   独立创建/编辑页（DOM 就绪后自动初始化）
 * - templates/activities/_activity_create_modal.html 「新建活动」弹窗（列表页与 Daily 页共用；校验失败换入错误片段后手动调 initWidgets 重挂）
 *
 * 约定：initWidgets() 幂等，可在表单字段 DOM 被整体替换后重复调用。
 * （一句话快速填表区已于 2026-10-28 随快记功能整体下线删除。）
 */
(function () {
    'use strict';
    var py = window.pinyinPro && window.pinyinPro.pinyin;
    var PINYIN_CACHE = {};

    // 获取名称的拼音（全拼 + 首字母），带缓存
    function pinyinOf(name) {
        if (!(name in PINYIN_CACHE)) {
            PINYIN_CACHE[name] = py ? {
                full: py(name, { toneType: 'none', type: 'array', nonZh: 'consecutive' }).join('').toLowerCase(),
                initials: py(name, { pattern: 'first', toneType: 'none', type: 'array', nonZh: 'consecutive' }).join('').toLowerCase(),
            } : { full: '', initials: '' };
        }
        return PINYIN_CACHE[name];
    }

    // 匹配规则：原文包含、拼音全拼包含、拼音首字母包含
    function matchName(name, kw) {
        if (name.toLowerCase().indexOf(kw) !== -1) return true;
        var info = pinyinOf(name);
        return (info.full && info.full.indexOf(kw) !== -1) ||
               (info.initials && info.initials.indexOf(kw) !== -1);
    }

    // 通用 chips 输入组件：chips + 建议下拉（拼音匹配），同步逗号分隔值到隐藏字段
    function initChipsInput(opts) {
        var box = document.getElementById(opts.boxId);
        var input = document.getElementById(opts.inputId);
        var suggest = document.getElementById(opts.suggestId);
        var hidden = document.getElementById(opts.hiddenId);
        var OPTIONS = JSON.parse(document.getElementById(opts.optionsId).textContent || '[]');
        var selected = hidden.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);

        function renderTags() {
            box.querySelectorAll('.chip-item').forEach(function (el) { el.remove(); });
            selected.forEach(function (name) {
                var chip = document.createElement('span');
                chip.className = 'chip-item inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-sm ' + opts.chipClass;
                chip.innerHTML = '<span></span><button type="button" title="移除" class="opacity-70 hover:opacity-100">&times;</button>';
                chip.querySelector('span').textContent = name;
                chip.querySelector('button').addEventListener('click', function () {
                    selected = selected.filter(function (n) { return n !== name; });
                    renderTags();
                    input.focus();
                });
                box.insertBefore(chip, input);
            });
            hidden.value = selected.join(', ');
            if (api && api.onChanged) api.onChanged(selected);   // 标签墙等外部同步点（向导步①；初始化时 api 尚未赋值，见下）
        }

        function addName(name) {
            name = name.trim().replace(/[,，]/g, '');
            if (!name || selected.indexOf(name) !== -1) return;
            selected.push(name);
            renderTags();
            input.value = '';
            hideSuggest();
        }

        function hideSuggest() { suggest.classList.add('hidden'); }

        function showSuggest() {
            var kw = input.value.trim().toLowerCase();
            var matches = OPTIONS.filter(function (n) {
                return selected.indexOf(n) === -1 && (!kw || matchName(n, kw));
            });
            suggest.innerHTML = '';
            matches.forEach(function (n) {
                var item = document.createElement('div');
                item.className = 'px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--accent-light)] cursor-pointer';
                item.textContent = n;
                item.addEventListener('mousedown', function (e) { e.preventDefault(); addName(n); });
                suggest.appendChild(item);
            });
            if (!matches.length && kw) {
                var tip = document.createElement('div');
                tip.className = 'px-3 py-2 text-sm text-[var(--text-muted)] cursor-pointer';
                tip.innerHTML = opts.newTip + '：<span class="text-[var(--text-secondary)]"></span>';
                tip.querySelector('span').textContent = input.value.trim();
                tip.addEventListener('mousedown', function (e) { e.preventDefault(); addName(input.value); });
                suggest.appendChild(tip);
            }
            suggest.classList.toggle('hidden', suggest.children.length === 0);
        }

        input.addEventListener('input', showSuggest);
        input.addEventListener('focus', showSuggest);
        input.addEventListener('blur', function () { setTimeout(hideSuggest, 150); });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ',' || e.key === '，') {
                e.preventDefault();
                addName(input.value);
            } else if (e.key === 'Backspace' && !input.value && selected.length) {
                selected.pop();
                renderTags();
            }
        });

        // 点击容器聚焦输入框
        box.addEventListener('click', function (e) { if (e.target === box) input.focus(); });

        renderTags();

        // 外部设值/取值接口（供快速输入填充与向导标签墙使用）
        var api = {
            setNames: function (names) { selected = (names || []).slice(); renderTags(); },
            getSelected: function () { return selected.slice(); },
            // 点选切换：已选→移除，未选→追加（走 addName 的去重/去逗号口径）
            toggle: function (name) {
                if (selected.indexOf(name) !== -1) {
                    selected = selected.filter(function (n) { return n !== name; });
                    renderTags();
                } else {
                    addName(name);
                }
            },
        };
        return api;
    }

    // 挂载 chips 组件（字段 DOM 不存在时跳过；可重复调用，每次整体重建）
    function initWidgets() {
        window.__chipsInputs = {};
        initTimeToggle();   // 弹窗重挂后时间开关也要重新就位（幂等，防重绑见函数内）
        if (document.getElementById('participant-box') && document.getElementById('participant-options')) {
            window.__chipsInputs.participants = initChipsInput({
                boxId: 'participant-box', inputId: 'participant-input', suggestId: 'participant-suggest',
                hiddenId: 'id_participants_input', optionsId: 'participant-options',
                chipClass: 'bg-[var(--accent-light)] text-[var(--text-secondary)]',
                newTip: '回车添加新参与者',
            });
        }
        if (document.getElementById('tag-box') && document.getElementById('tag-options')) {
            window.__chipsInputs.tags = initChipsInput({
                boxId: 'tag-box', inputId: 'tag-input', suggestId: 'tag-suggest',
                hiddenId: 'id_tags', optionsId: 'tag-options',
                chipClass: 'bg-[var(--accent-light)] border border-[var(--border-strong)] text-[var(--text-secondary)]',
                newTip: '回车添加新标签',
            });
        }
        return window.__chipsInputs;
    }

    // ==================== 具体时间可选字段：默认收起，开关展开（表单减负，2026-09-17） ====================
    // 2026-10-16 日期-时间配对改版：时间字段拆进开始/结束日期列（[data-time-slot]），
    // 开关一次切换两个槽 + 提示行（[data-time-hint]），不再有单一 #time-fields 容器
    function initTimeToggle() {
        var toggle = document.getElementById('time-toggle');
        var slots = document.querySelectorAll('[data-time-slot]');
        if (!toggle || !slots.length) return;
        var labelEl = document.getElementById('time-toggle-label');
        var hint = document.querySelector('[data-time-hint]');
        function show() {
            slots.forEach(function (el) { el.classList.remove('hidden'); });
            if (hint) hint.classList.remove('hidden');
            if (labelEl) labelEl.textContent = '收起时间';
        }
        function hide() {
            slots.forEach(function (el) { el.classList.add('hidden'); });
            if (hint) hint.classList.add('hidden');
            if (labelEl) labelEl.textContent = '添加具体时间（可选）';
        }
        // initWidgets 可被弹窗重挂重复调用：已绑定的 toggle 不能二次 addEventListener
        // （双绑定会让一次点击翻转两次，等于没点）
        if (!toggle.dataset.timeToggleBound) {
            toggle.dataset.timeToggleBound = '1';
            toggle.addEventListener('click', function () {
                slots[0].classList.contains('hidden') ? show() : hide();
            });
        }
        // 时间字段拆成 小时/分钟 两个 select（id_*_0 / id_*_1）
        if (readTime('id_start_time') || readTime('id_end_time')) show();
    }

    function timeEl(prefix, part) {
        return document.getElementById(prefix + '_' + part);
    }
    function readTime(prefix) {
        var h = timeEl(prefix, '0'), m = timeEl(prefix, '1');
        if (!h) return null;   // 页面无时间字段
        return (h.value && m.value) ? h.value + ':' + m.value : '';
    }

    // ==================== 独立页自动初始化（chips + 时间开关） ====================
    function autoInit() {
        initWidgets();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', autoInit);
    } else {
        autoInit();
    }

    window.PaActivityForm = { initWidgets: initWidgets };
})();

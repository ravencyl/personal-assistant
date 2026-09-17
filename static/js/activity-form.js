/* 活动表单组件：标签/参与者 chips 输入（拼音联想）+ AI 一句话解析结果回填。
 *
 * 消费方：
 * - templates/activities/activity_form.html   独立创建/编辑页（DOM 就绪后自动初始化，含快速填表区）
 * - templates/activities/_activity_create_modal.html 「新建活动」弹窗（列表页与 Daily 页共用；校验失败换入错误片段后手动调 initWidgets 重挂）
 *
 * 约定：initWidgets() 幂等，可在表单字段 DOM 被整体替换后重复调用；
 * FORM_DEFAULTS 只在首次调用时快照（弹窗内表单以初始空白为基准）。
 * 解析通道走 PaQuickParse.parse（原生 fetch + JSON，禁挂 hx-*，CSRF 读 meta 标签）。
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

        // 外部设值接口（供快速输入填充使用）
        return {
            setNames: function (names) { selected = (names || []).slice(); renderTags(); },
        };
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
    function initTimeToggle() {
        var toggle = document.getElementById('time-toggle');
        var fields = document.getElementById('time-fields');
        if (!toggle || !fields) return;
        var labelEl = document.getElementById('time-toggle-label');
        function show() {
            fields.classList.remove('hidden');
            if (labelEl) labelEl.textContent = '收起时间';
        }
        function hide() {
            fields.classList.add('hidden');
            if (labelEl) labelEl.textContent = '添加具体时间（可选）';
        }
        // initWidgets 可被弹窗重挂重复调用：已绑定的 toggle 不能二次 addEventListener
        // （双绑定会让一次点击翻转两次，等于没点）
        if (!toggle.dataset.timeToggleBound) {
            toggle.dataset.timeToggleBound = '1';
            toggle.addEventListener('click', function () {
                fields.classList.contains('hidden') ? show() : hide();
            });
        }
        // 编辑已有时间的活动 / 解析出时间后自动展开（openTimeFields 供 fillForm 调用）
        initTimeToggle.openTimeFields = show;
        // 时间字段拆成 小时/分钟 两个 select（id_*_0 / id_*_1）
        if (readTime('id_start_time') || readTime('id_end_time')) show();
    }

    // ==================== 解析结果回填（弹窗「填入下方表单」与独立页快速填表共用） ====================
    var PARSE_FIELDS = {
        name: 'id_name', start_date: 'id_start_date', end_date: 'id_end_date',
        cost: 'id_parsed_cost'
    };
    // 时间字段是 小时/分钟 两个下拉（HourMinuteSelect），状态是点选 chips（radio 组）
    var TIME_PREFIX = { start_time: 'id_start_time', end_time: 'id_end_time' };
    var RADIO_FIELDS = { status: 'id_status' };
    var FORM_DEFAULTS = null, lastFilled = [];

    function timeEl(prefix, part) {
        return document.getElementById(prefix + '_' + part);
    }
    // 非整格分钟（如 14:07 的 '07'）不在 4 格选项里，直接赋值会静默失败，先注入临时项
    function ensureOption(sel, v) {
        if (v && !Array.prototype.some.call(sel.options, function (o) { return o.value === v; })) {
            sel.add(new Option(v, v));
        }
    }
    function readTime(prefix) {
        var h = timeEl(prefix, '0'), m = timeEl(prefix, '1');
        if (!h) return null;   // 页面无时间字段
        return (h.value && m.value) ? h.value + ':' + m.value : '';
    }
    function writeTime(prefix, v) {
        var h = timeEl(prefix, '0'), m = timeEl(prefix, '1');
        if (!h || !m) return;
        if (!v) { h.value = ''; m.value = ''; return; }
        var p = String(v).split(':');
        ensureOption(h, p[0]);
        ensureOption(m, p[1]);
        h.value = p[0];
        m.value = p[1];
    }
    function readRadio(name) {
        var el = document.querySelector('input[name="' + name + '"]:checked');
        return el ? el.value : '';
    }
    function writeRadio(name, v) {
        if (!v) return;
        var el = document.querySelector('input[name="' + name + '"][value="' + v + '"]');
        if (el) el.checked = true;
    }

    function captureDefaults() {
        if (FORM_DEFAULTS) return;
        FORM_DEFAULTS = {};
        Object.keys(PARSE_FIELDS).forEach(function (k) {
            var el = document.getElementById(PARSE_FIELDS[k]);
            if (el) FORM_DEFAULTS[k] = el.value;
        });
        Object.keys(TIME_PREFIX).forEach(function (k) {
            FORM_DEFAULTS[k] = readTime(TIME_PREFIX[k]);
        });
        Object.keys(RADIO_FIELDS).forEach(function (k) {
            FORM_DEFAULTS[k] = readRadio(RADIO_FIELDS[k]);
        });
    }

    function fillForm(d) {
        captureDefaults();
        var chips = window.__chipsInputs || initWidgets();
        Object.keys(PARSE_FIELDS).forEach(function (k) {
            var el = document.getElementById(PARSE_FIELDS[k]);
            if (!el) return;   // 编辑页无 id_parsed_cost
            var v = d[k];
            if (v !== undefined && v !== null && v !== '') {
                el.value = v;
                if (lastFilled.indexOf(k) === -1) lastFilled.push(k);
            } else if (lastFilled.indexOf(k) !== -1) {
                // 只回滚解析填过的值，用户手输的不动
                el.value = FORM_DEFAULTS[k];
                lastFilled = lastFilled.filter(function (x) { return x !== k; });
            }
        });
        Object.keys(RADIO_FIELDS).forEach(function (k) {
            if (!document.querySelector('input[name="' + RADIO_FIELDS[k] + '"]')) return;
            var v = d[k];
            if (v) {
                writeRadio(RADIO_FIELDS[k], v);
                if (lastFilled.indexOf(k) === -1) lastFilled.push(k);
            } else if (lastFilled.indexOf(k) !== -1) {
                // 只回滚解析填过的值，用户手选的不动
                writeRadio(RADIO_FIELDS[k], FORM_DEFAULTS[k]);
                lastFilled = lastFilled.filter(function (x) { return x !== k; });
            }
        });
        Object.keys(TIME_PREFIX).forEach(function (k) {
            if (!timeEl(TIME_PREFIX[k], '0')) return;
            var v = d[k];
            if (v) {
                writeTime(TIME_PREFIX[k], v);
                if (lastFilled.indexOf(k) === -1) lastFilled.push(k);
            } else if (lastFilled.indexOf(k) !== -1) {
                // 只回滚解析填过的值，用户手输的不动
                writeTime(TIME_PREFIX[k], FORM_DEFAULTS[k]);
                lastFilled = lastFilled.filter(function (x) { return x !== k; });
            }
        });
        if (d.tags && chips.tags) chips.tags.setNames(d.tags);
        if (d.participants && chips.participants) chips.participants.setNames(d.participants);
        // 解析出时间 → 自动展开时间区（收起状态看不到回填值，等于没填）
        if ((d.start_time || d.end_time) && initTimeToggle.openTimeFields) {
            initTimeToggle.openTimeFields();
        }
    }

    // ==================== 独立页自动初始化（chips + 快速填表 + 草稿） ====================
    function autoInit() {
        initWidgets();   // 含时间开关初始化（幂等）

        var qInput = document.getElementById('form-quick-input');
        if (!qInput) return;
        var qBtn = document.getElementById('form-quick-btn');
        var qMsg = document.getElementById('form-quick-msg');

        function showMsg(text, isError) {
            qMsg.textContent = text;
            qMsg.className = 'mt-2 text-sm ' + (isError ? 'text-zinc-800 font-semibold' : 'text-zinc-900');
        }

        function doParse() {
            var text = qInput.value.trim();
            if (!text || qBtn.disabled) return;
            qMsg.classList.add('hidden');
            qBtn.disabled = true;
            qBtn.textContent = '解析中…';
            function done() {
                qBtn.disabled = false;
                qBtn.textContent = '解析';
            }
            PaQuickParse.parse(text, {
                parseUrl: qInput.dataset.parseUrl,
                onOk: function (d) {
                    done();
                    fillForm(d);
                    showMsg('已填入表单，请确认后保存' + (d.source === 'ai' ? '（AI 识别）' : '（规则识别）'), false);
                },
                onErr: function (m) { done(); showMsg(m, true); },
                onNetErr: function () { done(); showMsg('网络异常，请重试', true); },
            });
        }

        qBtn.addEventListener('click', doParse);
        qInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') doParse(); });

        // 列表页「编辑详情」跳转过来：读取草稿直接填充
        var draft = sessionStorage.getItem('quickInputDraft');
        if (draft) {
            sessionStorage.removeItem('quickInputDraft');
            try {
                var d = JSON.parse(draft);
                fillForm(d);
                showMsg('已从快速输入填入，请确认后保存', false);
                qInput.value = d.name || '';
            } catch (e) { /* 草稿损坏时忽略 */ }
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', autoInit);
    } else {
        autoInit();
    }

    window.PaActivityForm = { initWidgets: initWidgets, fillForm: fillForm };
})();

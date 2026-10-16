/* 建活动分步向导（2026-10-16）：创建页与列表页「新建活动」弹窗共用的唯一实现。
 *
 * 消费方：
 * - templates/activities/_activity_form_wizard.html（创建页与弹窗 body 都 include 它）
 * - 编辑页不渲染 [data-wizard]，本脚本 autoInit 时自然空跑
 *
 * 约定：
 * - 单页切步：五步同在一个 <form> 里，提交/服务端校验口径不变；
 * - initWidgets() 幂等：弹窗 422 换体后由弹窗脚本调 reinit()（重绑 + 跳错误步）；
 * - 解析/模板/草稿填完表单后调 goToLast() 让用户从第⑤步确认（填都填好了，逐帧走过没意义）；
 * - 错误步定位：字段错误段带 js-form-error 类（见向导 partial），reinit 时跳到第一处；
 * - Enter 前进只挂在 form 元素上（禁全局键盘快捷键；chips 输入/textarea/select 除外）。
 */
(function () {
    'use strict';

    var STEP_TITLES = ['标签', '名称与描述', '时间与状态', '参与者', '层级'];
    var current = 1, maxVisited = 1;
    var root, steps, bar, indexEl, titleEl, backBtn, nextBtn, submitBtn;

    function $(sel) { return root.querySelector(sel); }
    function $all(sel) { return Array.prototype.slice.call(root.querySelectorAll(sel)); }

    function showStep(n) {
        current = n;
        if (n > maxVisited) maxVisited = n;
        steps.forEach(function (el) {
            el.classList.toggle('hidden', Number(el.dataset.step) !== n);
        });
        var pct = Math.round(n / steps.length * 100);
        if (bar) bar.style.width = pct + '%';
        if (indexEl) indexEl.textContent = n;
        if (titleEl) titleEl.textContent = STEP_TITLES[n - 1] || '';
        // 三个导航按钮的显隐用内联 style 而不是 .hidden 类：它们带 .tap-target
        // （移动端媒体查询里 display:inline-flex），同特异性下加载序靠后会顶掉
        // Tailwind 的 .hidden——这是 turn_status/ask-drawer 同款坑，不能再用类开关
        if (backBtn) backBtn.style.display = n === 1 ? 'none' : '';
        if (nextBtn) nextBtn.style.display = n === steps.length ? 'none' : '';
        if (submitBtn) submitBtn.style.display = n === steps.length ? '' : 'none';
        // stepper 节点：走过的步可点回（高亮当前，未到过的步禁点）
        $all('[data-wizard-goto]').forEach(function (btn) {
            var target = Number(btn.dataset.wizardGoto);
            var dot = btn.querySelector('[data-wizard-dot]');
            btn.disabled = target > maxVisited;
            if (dot) {
                var isCurrent = target === n;
                var isDone = target < n;
                dot.className = 'inline-flex items-center justify-center w-5 h-5 rounded-full text-[10px] ' +
                    (isCurrent ? 'bg-[var(--accent)] text-[var(--bg)] border border-transparent' :
                     isDone ? 'bg-[var(--accent-light)] text-[var(--accent)] border border-transparent' :
                     'border border-[var(--border-strong)] text-[var(--text-muted)]');
            }
        });
        // 步进后滚到向导顶部（弹窗内滚容器/页面各自成立）
        var scroller = root.closest('.overflow-y-auto');
        if (scroller) scroller.scrollTop = 0;
        else root.scrollIntoView({ behavior: 'smooth', block: 'start' });
        hideStepError();
    }

    function hideStepError() {
        var el = $('[data-wizard-step-error]');
        if (el) el.classList.add('hidden');
    }

    // 步内轻校验：只有第②步的名称必填；其余字段全可跳过，交给服务端终验
    function validateStep(n) {
        if (n === 2) {
            var name = document.getElementById('id_name');
            if (name && !name.value.trim()) {
                var tip = $('[data-wizard-step-error]');
                if (tip) tip.classList.remove('hidden');
                if (name) name.focus();
                return false;
            }
        }
        return true;
    }

    function next() {
        if (!validateStep(current)) return;
        if (current < steps.length) showStep(current + 1);
    }
    function back() {
        if (current > 1) showStep(current - 1);
    }

    // ── 步骤⑤ 层级三选一：radio 切换展开对应输入面板 ──
    function initLayerPanels() {
        $all('[data-layer-option]').forEach(function (radio) {
            if (radio.dataset.layerBound) return;   // 422 换体后 DOM 是新的，旧守卫只在重挂时兜底
            radio.dataset.layerBound = '1';
            radio.addEventListener('change', syncLayerPanels);
        });
        syncLayerPanels();
    }
    function syncLayerPanels() {
        var checked = root.querySelector('[data-layer-option]:checked');
        var v = checked ? checked.value : 'standalone';
        $all('[data-layer-panel]').forEach(function (panel) {
            panel.classList.toggle('hidden', panel.dataset.layerPanel !== v);
        });
    }

    // ── 步骤① 标签墙：已有标签平铺成可点 chips，与 chips 输入框（#tag-box）双向同步 ──
    function renderTagWall() {
        var wall = document.getElementById('tag-wall');
        var chips = window.__chipsInputs && window.__chipsInputs.tags;
        if (!wall || !chips) return;
        var optionsEl = document.getElementById('tag-options');
        if (!optionsEl) return;
        var options;
        try { options = JSON.parse(optionsEl.textContent || '[]'); } catch (e) { options = []; }
        var selected = chips.getSelected();
        // 墙 = 已有标签 ∪ 已选但不在候选里的（手输新建的也要能看到/取消）
        var names = options.slice();
        selected.forEach(function (n) { if (names.indexOf(n) === -1) names.push(n); });
        wall.innerHTML = '';
        names.forEach(function (name) {
            var isOn = selected.indexOf(name) !== -1;
            var b = document.createElement('button');
            b.type = 'button';
            b.setAttribute('aria-pressed', isOn ? 'true' : 'false');
            b.className = 'tap-target rounded-full border px-3 py-1.5 text-sm transition-colors ' +
                (isOn
                    ? 'bg-[var(--accent)] border-transparent text-[var(--bg)]'
                    : 'border-[var(--border-strong)] text-[var(--text-secondary)] hover:bg-[var(--accent-light)]');
            b.textContent = name;
            b.addEventListener('click', function () { chips.toggle(name); });
            wall.appendChild(b);
        });
    }

    function initTagWall() {
        var chips = window.__chipsInputs && window.__chipsInputs.tags;
        if (chips) {
            chips.onChanged = renderTagWall;   // chips 任何增删都刷新墙
        }
        renderTagWall();
    }

    // ── Enter 前进：只挂 form 元素；chips 输入/textarea/select/终步不走这里 ──
    function initEnterAdvance(form) {
        if (form.dataset.wizardEnterBound) return;
        form.dataset.wizardEnterBound = '1';
        form.addEventListener('keydown', function (e) {
            if (e.key !== 'Enter' || current >= steps.length) return;
            var t = e.target;
            if (!t || t.tagName !== 'INPUT') return;
            var type = (t.getAttribute('type') || 'text').toLowerCase();
            if (type === 'hidden' || type === 'radio' || type === 'checkbox') return;
            if (t.id === 'tag-input' || t.id === 'participant-input') return;   // chips 自己吃 Enter
            e.preventDefault();
            next();
        });
    }

    function bind() {
        var nextBtnEl = $('[data-wizard-next]');
        var backBtnEl = $('[data-wizard-back]');
        if (nextBtnEl && !nextBtnEl.dataset.wizardBound) {
            nextBtnEl.dataset.wizardBound = '1';
            nextBtnEl.addEventListener('click', next);
        }
        if (backBtnEl && !backBtnEl.dataset.wizardBound) {
            backBtnEl.dataset.wizardBound = '1';
            backBtnEl.addEventListener('click', back);
        }
        $all('[data-wizard-goto]').forEach(function (btn) {
            if (btn.dataset.wizardBound) return;
            btn.dataset.wizardBound = '1';
            btn.addEventListener('click', function () {
                var target = Number(btn.dataset.wizardGoto);
                if (target <= maxVisited) showStep(target);
            });
        });
        var form = root.closest('form');
        if (form) initEnterAdvance(form);
        initLayerPanels();
        initTagWall();
        // 摘掉模板里的初始 .hidden（no-JS 兜底态）：此后显隐全由 showStep 的内联 display 接管，
        //留着类会在 style.display='' 时把按钮闷死（.hidden 与 .tap-target 的 display 之争见 showStep）
        [backBtn, nextBtn, submitBtn].forEach(function (b) { if (b) b.classList.remove('hidden'); });
    }

    function initWidgets(opts) {
        root = document.querySelector('[data-wizard]');
        if (!root) return null;
        opts = opts || {};
        steps = $all('[data-step]');
        bar = $('[data-wizard-bar]');
        indexEl = $('[data-wizard-index]');
        titleEl = $('[data-wizard-title]');
        backBtn = $('[data-wizard-back]');
        nextBtn = $('[data-wizard-next]');
        submitBtn = $('[data-wizard-submit]');
        bind();
        if (opts.startStep) {
            showStep(Math.min(Math.max(1, opts.startStep), steps.length));
        } else {
            // 默认落位：有字段错误（js-form-error）→ 跳错误步；否则第①步
            var err = $('.js-form-error');
            var errStep = err && err.closest('[data-step]');
            maxVisited = errStep ? Number(errStep.dataset.step) : 1;
            showStep(errStep ? Number(errStep.dataset.step) : 1);
        }
        return api;
    }

    var api = {
        next: next,
        back: back,
        showStep: showStep,
        // 解析/模板/草稿回填后调用：直接落到最后一步确认
        goToLast: function () { if (root && steps) showStep(steps.length); },
        // 弹窗 422 换体后调用：字段 DOM 全新，重挂向导并自动跳错误步
        reinit: function () { initWidgets(); },
    };

    // 自初始化：创建页 DOM 就绪即挂（编辑页无 [data-wizard] 自然空跑）；
    // 弹窗初始体随宿主页一起渲染，同样吃这次初始化，422 换体才需要显式 reinit
    function autoInit() { initWidgets(); }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', autoInit);
    } else {
        autoInit();
    }

    window.PaActivityWizard = api;
})();

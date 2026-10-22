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
        // 进入步骤③时同步子步骤面板与主导航按钮显隐（子步骤有自己的指示器，
        // 主"下一步"在非末子步时隐藏，由子步骤面板内的按钮接管前进）
        if (n === 3 && typeof showTimeSubStepEx === 'function') showTimeSubStepEx(timeSubStep);
        // 离开步骤③向前导航（回到步①/②）时重置子步骤，下次进入从 3.1 开始
        if (n < 3) timeSubStep = 1;
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
        // 步骤③有 3 个子步骤（日期→时段→状态），未到末子步时主"下一步"改走子步骤前进
        if (current === 3 && timeSubStep < 3) {
            showTimeSubStepEx(timeSubStep + 1);
            return;
        }
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

    // ── 步骤③ 时间选择三步子流程：日期 → 时段 → 状态 ──
    var timeSubStep = 1;
    function initTimeSubSteps() {
        var indicator = document.querySelector('[data-time-substep-indicator]');
        if (!indicator) return;

        // 初始化：显示子步 1，隐藏其他
        showTimeSubStepEx(1);

        // 绑定「下一步」按钮
        $all('[data-time-next]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var current = Number(btn.dataset.timeNext);
                if (current < 3) showTimeSubStepEx(current + 1);
            });
        });

        // 绑定「上一步」按钮
        $all('[data-time-back]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var current = Number(btn.dataset.timeBack);
                if (current > 1) showTimeSubStepEx(current - 1);
            });
        });

        // 跨天活动开关
        var toggleEndDate = document.getElementById('toggle-end-date');
        var endDateRow = document.getElementById('end-date-row');
        if (toggleEndDate && endDateRow) {
            toggleEndDate.addEventListener('click', function () {
                var isHidden = endDateRow.classList.contains('hidden');
                endDateRow.classList.toggle('hidden');
                toggleEndDate.textContent = isHidden ? '− 去掉结束日期' : '+ 添加结束日期（跨天活动）';
            });
        }

        // 开始日期变更 → 结束日期早于它时自动拉平为开始日期。
        // 结束日期行默认隐藏，但隐藏行里的 initial「今天」会随表单提交——
        // 开始日期选到未来时不同步，提交必撞「结束日期不能早于开始日期」422。
        var startDateInput = document.getElementById('id_start_date');
        var endDateInput = document.getElementById('id_end_date');
        if (startDateInput && endDateInput) {
            startDateInput.addEventListener('change', function () {
                var start = startDateInput.value;
                if (!start) return;
                if (!endDateInput.value || endDateInput.value < start) {
                    endDateInput.value = start;
                }
            });
        }

        // 时段预设 chips
        $all('[data-time-preset]').forEach(function (chip) {
            chip.addEventListener('click', function () {
                var preset = chip.dataset.timePreset;
                fillTimePreset(preset);
                // 高亮选中
                $all('[data-time-preset]').forEach(function (c) {
                    c.classList.remove('bg-[var(--accent)]', 'text-[var(--bg)]', 'border-transparent');
                    c.classList.add('border-[var(--border-strong)]', 'text-[var(--text-secondary)]');
                });
                chip.classList.remove('border-[var(--border-strong)]', 'text-[var(--text-secondary)]');
                chip.classList.add('bg-[var(--accent)]', 'text-[var(--bg)]', 'border-transparent');
            });
        });

        // 结束时间开关
        var toggleEndTime = document.getElementById('toggle-end-time');
        var endTimeRow = document.getElementById('end-time-row');
        if (toggleEndTime && endTimeRow) {
            toggleEndTime.addEventListener('click', function () {
                var isHidden = endTimeRow.classList.contains('hidden');
                endTimeRow.classList.toggle('hidden');
                toggleEndTime.textContent = isHidden ? '− 去掉结束时间' : '+ 添加结束时间';
            });
        }

        // 自定义面板里手动改开始时间 → 结束时间自动 +1h
        var startH = document.getElementById('id_start_time_0');
        var startM = document.getElementById('id_start_time_1');
        if (startH) startH.addEventListener('change', syncEndTimeFromStart);
        if (startM) startM.addEventListener('change', syncEndTimeFromStart);
    }

    function showTimeSubStepEx(n) {
        timeSubStep = n;
        // 更新指示器
        $all('[data-time-substep]').forEach(function (dot) {
            var step = Number(dot.dataset.timeSubstep);
            var isActive = step === n;
            var isDone = step < n;
            dot.className = 'w-8 h-8 rounded-full text-xs font-medium flex items-center justify-center ' +
                (isActive ? 'bg-[var(--accent)] text-[var(--bg)]' :
                 isDone ? 'bg-[var(--accent-light)] text-[var(--accent)] border border-transparent' :
                 'border border-[var(--border-strong)] text-[var(--text-muted)]');
            dot.disabled = !isDone && !isActive;
        });
        // 显示对应面板
        $all('[data-time-substep-panel]').forEach(function (panel) {
            panel.classList.toggle('hidden', Number(panel.dataset.timeSubstepPanel) !== n);
        });
        // 主"下一步"按钮：子步 3.1 时隐藏（面板自带"下一步：选时段"按钮，避免两个前进按钮混淆），
        // 子步 3.2/3.3 时显示（3.2 面板无前进按钮，靠主导航走到 3.3；3.3 靠主导航走到步④）
        if (nextBtn) nextBtn.style.display = n >= 2 ? '' : 'none';
    }

    // 开始时间变更后，自动把结束时间设为开始 + 1 小时
    function syncEndTimeFromStart() {
        var hEl = document.getElementById('id_start_time_0');
        var mEl = document.getElementById('id_start_time_1');
        if (!hEl || !mEl) return;
        var h = parseInt(hEl.value, 10);
        var m = parseInt(mEl.value, 10);
        if (isNaN(h) && isNaN(m)) return;   // 两个都是空/未选就不动
        var endH = (h + 1) % 24;
        var endHStr = String(endH).padStart(2, '0');
        var mStr = String(isNaN(m) ? 0 : m).padStart(2, '0');

        var ehEl = document.getElementById('id_end_time_0');
        var emEl = document.getElementById('id_end_time_1');
        if (ehEl) { ehEl.value = endHStr; ehEl.dispatchEvent(new Event('change', { bubbles: true })); }
        if (emEl) { emEl.value = mStr; emEl.dispatchEvent(new Event('change', { bubbles: true })); }

        // 自动展开结束时间行（用户选了开始时间就意味着需要结束时间）
        var endTimeRow = document.getElementById('end-time-row');
        if (endTimeRow && endTimeRow.classList.contains('hidden')) {
            endTimeRow.classList.remove('hidden');
            var toggle = document.getElementById('toggle-end-time');
            if (toggle) toggle.textContent = '− 去掉结束时间';
        }
    }

    function fillTimePreset(preset) {
        if (preset === 'custom') {
            // 展开自定义时间面板
            var customPanel = document.getElementById('custom-time-panel');
            if (customPanel) customPanel.classList.remove('hidden');
            // 显示提示
            var hint = document.querySelector('[data-time-hint]');
            if (hint) hint.classList.remove('hidden');
            return;
        }

        // HourMinuteSelect 是 MultiWidget：id_start_time_0=时(00-23), id_start_time_1=分(00/15/30/45)
        var times = {
            morning:   ['09', '00'],
            afternoon: ['14', '00'],
            evening:   ['19', '00'],
        };
        var t = times[preset];
        if (!t) return;

        var hSelect = document.getElementById('id_start_time_0');
        var mSelect = document.getElementById('id_start_time_1');
        if (hSelect) {
            hSelect.value = t[0];
            hSelect.dispatchEvent(new Event('change', { bubbles: true }));
        }
        if (mSelect) {
            mSelect.value = t[1];
            mSelect.dispatchEvent(new Event('change', { bubbles: true }));
        }

        // 结束时间 = 开始 + 1 小时
        syncEndTimeFromStart();

        // 隐藏自定义面板（如果开着）
        var customPanel = document.getElementById('custom-time-panel');
        if (customPanel && !customPanel.classList.contains('hidden')) {
            customPanel.classList.add('hidden');
        }
        // 显示提示
        var hint = document.querySelector('[data-time-hint]');
        if (hint) hint.classList.remove('hidden');
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

    var WALL_RETRY = 20;
    function initTagWall() {
        var chips = window.__chipsInputs && window.__chipsInputs.tags;
        if (chips) {
            chips.onChanged = renderTagWall;   // chips 任何增删都刷新墙
            renderTagWall();
        } else if (WALL_RETRY-- > 0) {
            // chips 未就绪（本脚本先于 activity-form.js 执行）时稍后重试；
            // 正常宿主页都会把本脚本排在 activity-form.js 之后，这里只是兜底
            setTimeout(initTagWall, 50);
        }
    }

    // ── 步骤③ 快捷时间选择：今天/明天/本周末一键填入，自定义日期展开完整面板 ──
    function initQuickTimeChips() {
        var customToggle = document.getElementById('custom-date-toggle');
        var customPanel = document.getElementById('custom-date-panel');
        if (!customToggle || !customPanel) return;

        // 点「自定义日期」展开/收起完整面板
        customToggle.addEventListener('click', function () {
            var isOpen = !customPanel.classList.contains('hidden');
            customPanel.classList.toggle('hidden');
            customToggle.classList.toggle('bg-[var(--accent-light)]', !isOpen);
            customToggle.classList.toggle('text-[var(--accent)]', !isOpen);
        });

        // 快捷 chips：今天/明天/本周末
        $all('[data-time-chip]').forEach(function (chip) {
            chip.addEventListener('click', function () {
                var preset = chip.dataset.timeChip;
                fillDatePreset(preset);
                // 视觉反馈：高亮选中的 chip
                $all('[data-time-chip]').forEach(function (c) {
                    c.classList.remove('bg-[var(--accent)]', 'text-[var(--bg)]', 'border-transparent');
                    c.classList.add('border-[var(--border-strong)]', 'text-[var(--text-secondary)]');
                });
                chip.classList.remove('border-[var(--border-strong)]', 'text-[var(--text-secondary)]');
                chip.classList.add('bg-[var(--accent)]', 'text-[var(--bg)]', 'border-transparent');
            });
        });
    }

    function fillDatePreset(preset) {
        var today = new Date();
        var startInput = document.getElementById('id_start_date');
        var endInput = document.getElementById('id_end_date');
        if (!startInput || !endInput) return;

        var formatDate = function (d) {
            var y = d.getFullYear();
            var m = String(d.getMonth() + 1).padStart(2, '0');
            var day = String(d.getDate()).padStart(2, '0');
            return y + '-' + m + '-' + day;
        };

        var start = new Date(today);
        var end = new Date(today);

        if (preset === 'today') {
            // 今天 → 开始=结束=今天
        } else if (preset === 'tomorrow') {
            start.setDate(start.getDate() + 1);
            end.setDate(end.getDate() + 1);
        } else if (preset === 'this-weekend') {
            // 本周末：周六和周日
            var dayOfWeek = today.getDay(); // 0=周日, 6=周六
            var daysToSaturday = 6 - dayOfWeek;
            if (daysToSaturday < 0) daysToSaturday += 7; // 如果今天是周日，推到下周六
            start.setDate(start.getDate() + daysToSaturday);
            end.setDate(start.getDate() + 1); // 周日
        }

        startInput.value = formatDate(start);
        endInput.value = formatDate(end);

        // 触发 input 事件以更新联动逻辑（如日历预览等）
        startInput.dispatchEvent(new Event('input', { bubbles: true }));
        endInput.dispatchEvent(new Event('input', { bubbles: true }));

        // 如果自定义面板是关着的，打开它让用户看到填好的值
        var customPanel = document.getElementById('custom-date-panel');
        if (customPanel && customPanel.classList.contains('hidden')) {
            customPanel.classList.remove('hidden');
            var customToggle = document.getElementById('custom-date-toggle');
            if (customToggle) {
                customToggle.classList.add('bg-[var(--accent-light)]', 'text-[var(--accent)]');
            }
        }
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
        initTimeSubSteps();  // 初始化时间选择三步子流程
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
            if (err && errStep) {
                // 错误段落若藏在默认收起的容器里（结束日期行/自定义时间面板/结束时间行），
                // 先摊开再落位——否则跳到了步③用户照样看不见（2026-10-21 线上 422 循环实锤）
                var buried = err.closest('#end-date-row, #end-time-row, #custom-time-panel');
                if (buried) buried.classList.remove('hidden');
                // 步③的错误要落到对应子步（date/time/status 面板各有一个），停在 3.1 时
                // 3.2 里的错误照样不可见
                var sub = err.closest('[data-time-substep-panel]');
                if (sub) timeSubStep = Number(sub.dataset.timeSubstepPanel);
            }
            maxVisited = errStep ? Number(errStep.dataset.step) : 1;
            showStep(errStep ? Number(errStep.dataset.step) : 1);
            // 把错误段落滚到弹窗可视区中央，保证第一眼就能看到
            if (err && err.scrollIntoView) err.scrollIntoView({ block: 'center' });
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

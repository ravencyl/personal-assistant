/* 备忘页（note_list.html）专属脚本：语音速记 + 长备忘折叠 + ?new=1 聚焦。
   原先是模板内联 <script>，外置到此文件统一走 staticv 内容版本号。 */
(function () {
    var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    var btn = document.getElementById('voiceBtn');
    var textarea = document.getElementById('noteContentInput');
    var errEl = document.getElementById('voiceError');
    var iconEl = document.getElementById('voiceBtnIcon');
    var dotEl = btn && btn.querySelector('.voice-rec-dot');
    if (!btn || !textarea) return;

    // 能力检测：不支持则按钮保持隐藏，不提示任何内容（避免视觉噪音）
    if (!SpeechRecognition) { btn.hidden = true; return; }
    btn.hidden = false;

    var recognition = null;
    var recording = false;
    var baseText = '';      // 开始录音时 textarea 的既有内容（不清空）
    var sessionText = '';   // 本次录音累计识别文本（追加在既有内容之后）
    var userStopped = false;

    function showError(msg) {
        if (!errEl) return;
        errEl.textContent = msg;
        errEl.classList.remove('hidden');
    }
    function clearError() {
        if (!errEl) return;
        errEl.textContent = '';
        errEl.classList.add('hidden');
    }

    function setRecordingUI(on) {
        recording = on;
        btn.classList.toggle('voice-recording', on);
        btn.title = on ? '停止录音' : '语音速记';
        if (iconEl) iconEl.style.display = on ? 'none' : '';
        if (dotEl) dotEl.style.display = on ? '' : 'none';
    }

    function appendResult() {
        textarea.value = baseText + sessionText;
    }

    btn.addEventListener('click', function () {
        if (recording) {
            // 再次点击：停止识别（onend 中收尾）
            userStopped = true;
            if (recognition) recognition.stop();
            return;
        }

        clearError();
        recognition = new SpeechRecognition();
        recognition.lang = 'zh-CN';
        recognition.interimResults = true;
        recognition.continuous = true;

        baseText = textarea.value;
        sessionText = '';
        userStopped = false;

        recognition.onstart = function () { setRecordingUI(true); };

        recognition.onresult = function (event) {
            var finalText = '';
            var interimText = '';
            for (var i = 0; i < event.results.length; i++) {
                var result = event.results[i];
                if (result.isFinal) {
                    finalText += result[0].transcript;
                } else {
                    interimText += result[0].transcript;
                }
            }
            sessionText = finalText + interimText;
            appendResult();
        };

        recognition.onerror = function (event) {
            var msg = '语音识别出错，请重试';
            if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
                msg = '麦克风权限被拒绝，请在浏览器设置中允许访问麦克风';
            } else if (event.error === 'no-speech') {
                msg = '没有检测到语音，请靠近麦克风重试';
            } else if (event.error === 'audio-capture') {
                msg = '未检测到麦克风设备';
            } else if (event.error === 'network') {
                msg = '网络异常，语音识别不可用';
            }
            showError(msg);
        };

        recognition.onend = function () {
            setRecordingUI(false);
            // 收尾：把最终文本追加进既有内容后保持原位（不清空用户已输入内容）
            appendResult();
            if (userStopped) {
                userStopped = false;
            }
        };

        try {
            recognition.start();
        } catch (e) {
            setRecordingUI(false);
            showError('无法启动语音识别，请重试');
        }
    });
})();

// 长备忘折叠：内容超过约 6 行才收起并显示切换按钮（无 JS 时全文展示）
(function () {
    var FOLD_HEIGHT = 144; // px，约 6 行正文
    document.querySelectorAll('[data-note-fold]').forEach(function (fold) {
        var body = fold.querySelector('.note-fold-body');
        var toggle = fold.querySelector('[data-note-toggle]');
        if (!body || !toggle) return;
        if (body.scrollHeight <= FOLD_HEIGHT) return;
        fold.classList.add('is-collapsed');
        toggle.classList.remove('hidden');
        toggle.textContent = '展开全文（' + toggle.dataset.length + ' 字）';
        toggle.addEventListener('click', function () {
            var collapsed = fold.classList.toggle('is-collapsed');
            toggle.textContent = collapsed
                ? '展开全文（' + toggle.dataset.length + ' 字）'
                : '收起';
        });
    });
})();

// 搜索面板「写备忘」快捷入口带 ?new=1：进来直接聚焦速记输入框
(function () {
    try {
        if (!window.URLSearchParams) return;
        if (new URLSearchParams(window.location.search).get('new') !== '1') return;
        var input = document.getElementById('noteContentInput');
        if (input) input.focus();
    } catch (e) { /* URLSearchParams 不可用就跳过聚焦，不影响本页其他逻辑 */ }
})();

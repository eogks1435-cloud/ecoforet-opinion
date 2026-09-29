/* Online consent form: an answer per question, privacy choices, signature, confirmation and submission. */
(function () {
  'use strict';

  var form = document.getElementById('consent-form');
  if (!form) return;

  function byId(id) { return document.getElementById(id); }

  var preview = form.getAttribute('data-preview') === '1';
  var accepting = form.getAttribute('data-accepting') === '1';
  var building = byId('building');
  var unit = byId('unit');
  var nameInput = byId('resident_name');
  var finalBox = byId('final_confirmed');
  var submitButton = byId('submit-button');
  var formAlert = byId('form-alert');
  var signatureBox = byId('signature-box');
  var signatureInput = byId('signature-data');
  var tokenInput = byId('client-token');
  var confirmModal = byId('confirm-modal');
  var confirmSubmit = byId('confirm-submit');
  var confirmCancel = byId('confirm-cancel');
  var confirmAlert = byId('confirm-alert');

  var TEMP_FAILURE = '일시적으로 제출하지 못했습니다. 잠시 후 다시 시도해 주세요.';
  var PRIVACY_REQUIRED = '개인정보 수집·이용에 동의하지 않으면 온라인 동의서를 제출할 수 없습니다.';
  var OVERSEAS_REQUIRED = '개인정보 국외 이전에 동의하지 않으면 온라인으로 제출할 수 없습니다. 안내된 다른 참여 방법을 이용해 주세요.';
  var SUBMIT_TIMEOUT = 90000; // a sleeping free instance can take about a minute to wake up
  var KEEP_KEY = 'consent-kept-fields';

  // One choice group per question, the privacy consent and each recipient, in page order.
  var groups = Array.prototype.map.call(form.querySelectorAll('fieldset.question'), function (box) {
    return { name: box.getAttribute('data-field'), label: box.getAttribute('data-summary') };
  });

  // A random token per page load: if a response is lost and the resident taps again, the server recognises
  // the retry and returns the first receipt instead of storing a second copy.
  function randomToken() {
    var bytes = new Uint8Array(18);
    var out = '';
    if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(bytes);
    else for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    for (var j = 0; j < bytes.length; j++) out += ('0' + bytes[j].toString(16)).slice(-2);
    return out;
  }
  tokenInput.value = randomToken();

  // ---------------------------------------------------------------- errors
  function fieldBox(key) {
    return form.querySelector('[data-field="' + key + '"]');
  }

  function setError(key, message) {
    var node = byId('error-' + key);
    if (node) {
      node.textContent = message || '';
      node.hidden = !message;
    }
    var box = fieldBox(key);
    if (!box) return;
    if (message) box.classList.add('has-error');
    else box.classList.remove('has-error');
    var inputs = box.querySelectorAll('input:not([type="hidden"]), textarea');
    for (var i = 0; i < inputs.length; i++) {
      if (message) inputs[i].setAttribute('aria-invalid', 'true');
      else inputs[i].removeAttribute('aria-invalid');
    }
  }

  function showAlert(node, message) {
    node.textContent = message;
    node.hidden = false;
  }

  function hideAlert(node) {
    node.textContent = '';
    node.hidden = true;
  }

  function scrollIntoView(node) {
    try { node.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (err) { node.scrollIntoView(); }
  }

  // Shows every error; focuses and scrolls to the first one in page order.
  function showErrors(errors) {
    var boxes = form.querySelectorAll('[data-field]');
    var first = null;
    for (var i = 0; i < boxes.length; i++) {
      var key = boxes[i].getAttribute('data-field');
      setError(key, errors[key]);
      if (errors[key] && !first) first = boxes[i];
    }
    if (first) {
      var target = first.querySelector('input:not([type="hidden"])');
      if (target) {
        try { target.focus({ preventScroll: true }); } catch (err) { target.focus(); }
      }
      scrollIntoView(first);
    }
    return first;
  }

  // ---------------------------------------------------------------- signature
  var pad = new window.SignaturePad(byId('signature-pad'), function () {
    if (pad.isEmpty()) signatureBox.classList.remove('has-ink');
    else signatureBox.classList.add('has-ink');
    if (pad.isSigned()) setError('signature', '');
  });
  if (window.ResizeObserver) {
    new window.ResizeObserver(function () { pad.resize(); }).observe(pad.canvas);
  } else {
    window.addEventListener('resize', function () { pad.resize(); });
    window.addEventListener('orientationchange', function () { setTimeout(function () { pad.resize(); }, 250); });
  }
  byId('signature-clear').addEventListener('click', function () { pad.clear(); });

  // ---------------------------------------------------------------- inputs
  function checkedValue(name) {
    var input = form.querySelector('input[name="' + name + '"]:checked');
    return input ? input.value : '';
  }

  function checkedLabel(name) {
    var input = form.querySelector('input[name="' + name + '"]:checked');
    var text = input && input.parentNode.querySelector('.choice-text');
    return text ? text.textContent : '';
  }

  function normNumber(value) {
    var number = parseInt(value, 10);
    return isNaN(number) ? '' : String(number);
  }

  function normName(value) {
    return value.replace(/\s+/g, ' ').trim();
  }

  function digitsOnly(input) {
    input.addEventListener('input', function () {
      var match = input.value
        .replace(/[０-９]/g, function (ch) { return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0); })
        .match(/[0-9]+/); // first number only: a pasted '101동 1203호' must not become '1011'
      var cleaned = match ? match[0].slice(0, 4) : '';
      if (cleaned !== input.value) input.value = cleaned;
      setError('residence', '');
    });
  }
  digitsOnly(building);
  digitsOnly(unit);

  function onEnter(input, next) {
    input.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      if (next) next.focus();
      else input.blur();
    });
  }
  onEnter(building, unit);
  onEnter(unit, nameInput);
  onEnter(nameInput, null);

  nameInput.addEventListener('input', function () { setError('resident_name', ''); });
  form.addEventListener('change', function (event) {
    var name = event.target.name;
    if (name === 'privacy_consent') {
      setError(name, event.target.value === 'DISAGREE' ? PRIVACY_REQUIRED : '');
    } else if (name === 'overseas_consent') {
      setError(name, event.target.value === 'DISAGREE' ? OVERSEAS_REQUIRED : '');
    } else if (name && (name.indexOf('answer_') === 0 || name.indexOf('provide_') === 0)) {
      setError(name, '');
    } else if (event.target === finalBox) {
      setError('final_confirmed', '');
    }
  });

  function validate() {
    var errors = {};
    for (var i = 0; i < groups.length; i++) {
      if (!checkedValue(groups[i].name)) {
        errors[groups[i].name] = groups[i].name.indexOf('answer_') === 0
          ? groups[i].label + '에 대한 답변을 선택해 주세요.'
          : groups[i].label + ' 동의 여부를 선택해 주세요.';
      }
    }
    if (checkedValue('privacy_consent') === 'DISAGREE') errors.privacy_consent = PRIVACY_REQUIRED;
    if (checkedValue('overseas_consent') === 'DISAGREE') errors.overseas_consent = OVERSEAS_REQUIRED;
    var b = building.value;
    var u = unit.value;
    var numberOk = function (value) { return /^[0-9]{1,4}$/.test(value) && parseInt(value, 10) > 0; };
    if (!b && !u) errors.residence = '동과 호수를 입력해 주세요.';
    else if (!b) errors.residence = '동을 입력해 주세요.';
    else if (!numberOk(b)) errors.residence = '동을 숫자로 정확히 입력해 주세요.';
    else if (!u) errors.residence = '호수를 입력해 주세요.';
    else if (!numberOk(u)) errors.residence = '호수를 숫자로 정확히 입력해 주세요.';
    var name = normName(nameInput.value);
    if (!name) errors.resident_name = '성명을 입력해 주세요.';
    else if (name.replace(/ /g, '').length < 2) errors.resident_name = '성명을 2자 이상 입력해 주세요.';
    if (pad.isEmpty()) errors.signature = '서명해 주세요.';
    else if (!pad.isSigned()) errors.signature = '서명이 너무 짧습니다. 서명란에 다시 서명해 주세요.';
    if (!finalBox.checked) errors.final_confirmed = '최종 확인에 체크해 주세요.';
    return errors;
  }

  // ---------------------------------------------------------------- confirmation and submission
  var submitting = false;
  var mustReload = false;
  var reloadMessage = '';
  var signatureDataUrl = '';

  function setBusy(busy) {
    confirmSubmit.disabled = busy;
    confirmCancel.disabled = busy;
    confirmSubmit.textContent = busy ? '제출 중…' : (mustReload ? '새로고침' : '최종 제출');
    if (busy) confirmModal.setAttribute('data-locked', 'true');
    else confirmModal.removeAttribute('data-locked');
  }

  function fillSummary() {
    byId('summary-unit').textContent = building.value + '동 ' + unit.value + '호';
    byId('summary-name').textContent = '성명: ' + nameInput.value;
    var list = byId('summary-answers');
    while (list.firstChild) list.removeChild(list.firstChild);
    for (var i = 0; i < groups.length; i++) {
      var item = document.createElement('li');
      var label = document.createElement('span');
      var value = document.createElement('strong');
      label.textContent = groups[i].label;
      value.textContent = checkedLabel(groups[i].name);
      item.appendChild(label);
      item.appendChild(value);
      list.appendChild(item);
    }
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (preview || !accepting || submitting) return;
    hideAlert(formAlert);
    if (showErrors(validate())) {
      showAlert(formAlert, '확인이 필요한 항목이 있습니다. 표시된 내용을 확인해 주세요.');
      return;
    }
    building.value = normNumber(building.value);
    unit.value = normNumber(unit.value);
    nameInput.value = normName(nameInput.value);
    fillSummary();
    signatureDataUrl = pad.toDataURL();
    byId('summary-signature').src = signatureDataUrl;
    if (mustReload) showAlert(confirmAlert, reloadMessage);
    else hideAlert(confirmAlert);
    setBusy(false);
    window.Modal.open(confirmModal);
  });

  function keepIdentity() {
    try {
      window.sessionStorage.setItem(KEEP_KEY, JSON.stringify({
        building: building.value, unit: unit.value, name: nameInput.value
      }));
    } catch (err) { /* storage unavailable: the resident types them again */ }
  }

  function fail(status, data) {
    submitting = false;
    var message = data.message || TEMP_FAILURE;
    if (data.code === 'document_changed') {
      // The wording changed: the resident must read it again. 동·호수·성명 are kept, answers are not.
      mustReload = true;
      reloadMessage = message;
      keepIdentity();
      setBusy(false);
      showAlert(confirmAlert, message);
      return;
    }
    setBusy(false);
    if ((status === 409 || status === 422) && data.errors) {
      window.Modal.close(confirmModal, true);
      showErrors(data.errors);
      showAlert(formAlert, message);
      return;
    }
    // No answer or a temporary error: trying again is safe, the token prevents a second record.
    showAlert(confirmAlert, message);
  }

  confirmSubmit.addEventListener('click', function () {
    if (mustReload) {
      window.location.reload();
      return;
    }
    if (submitting || preview || !accepting) return;
    submitting = true;
    setBusy(true);
    hideAlert(confirmAlert);
    signatureInput.value = signatureDataUrl;

    var controller = typeof window.AbortController === 'function' ? new window.AbortController() : null;
    var timer = setTimeout(function () { if (controller) controller.abort(); }, SUBMIT_TIMEOUT);
    var options = {
      method: 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
    };
    if (controller) options.signal = controller.signal;

    fetch(form.getAttribute('action'), options)
      .then(function (response) {
        return response.json().then(
          function (data) { return { status: response.status, data: data || {} }; },
          function () { return { status: response.status, data: {} }; }
        );
      })
      .then(function (result) {
        clearTimeout(timer);
        if (result.status === 201 && result.data.ok && result.data.redirect) {
          window.location.replace(result.data.redirect); // committed; the form leaves the history
          return;
        }
        fail(result.status, result.data);
      })
      .catch(function () {
        clearTimeout(timer);
        fail(0, {});
      });
  });

  // After a wording change the page reloads with the kept 동·호수·성명 and an explanation.
  (function restoreIdentity() {
    var kept = null;
    try {
      kept = JSON.parse(window.sessionStorage.getItem(KEEP_KEY) || 'null');
      window.sessionStorage.removeItem(KEEP_KEY);
    } catch (err) { kept = null; }
    if (!kept) return;
    building.value = kept.building || '';
    unit.value = kept.unit || '';
    nameInput.value = kept.name || '';
    showAlert(byId('top-alert'),
      '동의서 문구가 변경되었습니다. 변경된 내용을 확인한 뒤 각 질문의 답변과 서명을 다시 입력해 주세요. ' +
      '입력하신 동·호수와 성명은 유지했습니다.');
  })();

  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    submitting = false;
    setBusy(false);
    window.Modal.close(confirmModal, true);
    pad.resize();
  });

  if (preview || !accepting) submitButton.disabled = true;
})();

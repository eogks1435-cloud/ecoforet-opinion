/* Resident opinion form: signature pad, validation, confirmation dialog and submission. */
(function () {
  'use strict';

  // ---------------------------------------------------------------- signature pad
  var ASPECT = 2;     // pad width / height; CSS keeps .sig-box at 2:1
  var OUT_W = 600;    // stored PNG size, identical whatever the screen size
  var OUT_H = 300;
  var LINE = 0.016;   // pen width as a share of the pad height
  var MIN_INK = 0.2;  // minimum total pen travel, in pad heights (a dot or a tap is not a signature)
  var INK = '#111111';

  function clamp01(value) {
    return value < 0 ? 0 : value > 1 ? 1 : value;
  }

  // Points are stored normalised (0..1), so the screen pad and the exported image draw the same shape.
  function drawStrokes(ctx, strokes, width, height) {
    var pen = Math.max(1.5, LINE * height);
    ctx.lineWidth = pen;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = INK;
    ctx.fillStyle = INK;
    for (var s = 0; s < strokes.length; s++) {
      var pts = strokes[s];
      if (pts.length === 1) {
        ctx.beginPath();
        ctx.arc(pts[0].x * width, pts[0].y * height, pen / 2, 0, Math.PI * 2);
        ctx.fill();
        continue;
      }
      ctx.beginPath();
      ctx.moveTo(pts[0].x * width, pts[0].y * height);
      for (var i = 1; i < pts.length - 1; i++) {
        ctx.quadraticCurveTo(
          pts[i].x * width, pts[i].y * height,
          (pts[i].x + pts[i + 1].x) / 2 * width, (pts[i].y + pts[i + 1].y) / 2 * height
        );
      }
      ctx.lineTo(pts[pts.length - 1].x * width, pts[pts.length - 1].y * height);
      ctx.stroke();
    }
  }

  function SignaturePad(canvas, onChange) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.strokes = [];
    this.active = null;
    this.pointerId = null;
    this.width = 0;
    this.height = 0;
    this.frame = 0;
    this.onChange = onChange;
    this.resize();
    this.bind();
  }

  SignaturePad.prototype.resize = function () {
    var rect = this.canvas.getBoundingClientRect();
    var width = Math.round(rect.width);
    var height = Math.round(rect.height);
    if (!width || !height || (width === this.width && height === this.height)) return;
    var ratio = Math.min(window.devicePixelRatio || 1, 3);
    this.width = width;
    this.height = height;
    this.canvas.width = Math.round(width * ratio);
    this.canvas.height = Math.round(height * ratio);
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.redraw();
  };

  SignaturePad.prototype.redraw = function () {
    this.frame = 0;
    this.ctx.clearRect(0, 0, this.width, this.height);
    drawStrokes(this.ctx, this.strokes, this.width, this.height);
  };

  SignaturePad.prototype.requestRedraw = function () {
    var self = this;
    if (!this.frame) this.frame = window.requestAnimationFrame(function () { self.redraw(); });
  };

  SignaturePad.prototype.pointFrom = function (event) {
    var rect = this.canvas.getBoundingClientRect();
    return {
      x: clamp01((event.clientX - rect.left) / rect.width),
      y: clamp01((event.clientY - rect.top) / rect.height)
    };
  };

  SignaturePad.prototype.begin = function (event) {
    this.active = [this.pointFrom(event)];
    this.strokes.push(this.active);
    this.requestRedraw();
    this.onChange();
  };

  SignaturePad.prototype.extend = function (event) {
    if (!this.active) return;
    var point = this.pointFrom(event);
    var last = this.active[this.active.length - 1];
    var dx = (point.x - last.x) * this.width;
    var dy = (point.y - last.y) * this.height;
    if (dx * dx + dy * dy < 1) return; // sub-pixel jitter
    this.active.push(point);
    this.requestRedraw();
  };

  SignaturePad.prototype.finish = function () {
    if (!this.active) return;
    this.active = null;
    this.pointerId = null;
    this.requestRedraw();
    this.onChange();
  };

  SignaturePad.prototype.clear = function () {
    this.strokes = [];
    this.active = null;
    this.pointerId = null;
    this.redraw();
    this.onChange();
  };

  SignaturePad.prototype.isEmpty = function () {
    return this.strokes.length === 0;
  };

  SignaturePad.prototype.isSigned = function () {
    var total = 0;
    for (var s = 0; s < this.strokes.length; s++) {
      var pts = this.strokes[s];
      for (var i = 1; i < pts.length; i++) {
        var dx = (pts[i].x - pts[i - 1].x) * ASPECT;
        var dy = pts[i].y - pts[i - 1].y;
        total += Math.sqrt(dx * dx + dy * dy);
      }
    }
    return total >= MIN_INK;
  };

  SignaturePad.prototype.toDataURL = function () {
    var out = document.createElement('canvas');
    out.width = OUT_W;
    out.height = OUT_H;
    var ctx = out.getContext('2d');
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, OUT_W, OUT_H);
    drawStrokes(ctx, this.strokes, OUT_W, OUT_H);
    return out.toDataURL('image/png');
  };

  SignaturePad.prototype.bind = function () {
    var self = this;
    var canvas = this.canvas;

    // A finger on the pad must never scroll or zoom the page (touch-action: none covers current browsers).
    var holdPage = function (event) { if (event.cancelable) event.preventDefault(); };
    canvas.addEventListener('touchstart', holdPage, { passive: false });
    canvas.addEventListener('touchmove', holdPage, { passive: false });

    if (window.PointerEvent) {
      canvas.addEventListener('pointerdown', function (event) {
        if (self.pointerId !== null || (event.pointerType === 'mouse' && event.button !== 0)) return;
        event.preventDefault();
        self.pointerId = event.pointerId;
        try { canvas.setPointerCapture(event.pointerId); } catch (err) { /* drawing still works */ }
        self.begin(event);
      });
      canvas.addEventListener('pointermove', function (event) {
        if (event.pointerId !== self.pointerId) return;
        event.preventDefault();
        var batch = typeof event.getCoalescedEvents === 'function' ? event.getCoalescedEvents() : null;
        if (batch && batch.length) {
          for (var i = 0; i < batch.length; i++) self.extend(batch[i]);
        } else {
          self.extend(event);
        }
      });
      var end = function (event) { if (event.pointerId === self.pointerId) self.finish(); };
      canvas.addEventListener('pointerup', end);
      canvas.addEventListener('pointercancel', end);
      canvas.addEventListener('lostpointercapture', end);
    } else {
      // iOS 12 and older have no pointer events.
      canvas.addEventListener('touchstart', function (event) {
        if (event.touches.length === 1) self.begin(event.touches[0]);
      }, { passive: false });
      canvas.addEventListener('touchmove', function (event) {
        if (event.touches.length === 1) self.extend(event.touches[0]);
      }, { passive: false });
      canvas.addEventListener('touchend', function () { self.finish(); });
      canvas.addEventListener('touchcancel', function () { self.finish(); });
      canvas.addEventListener('mousedown', function (event) {
        if (event.button !== 0) return;
        event.preventDefault();
        self.begin(event);
      });
      window.addEventListener('mousemove', function (event) { if (self.active) self.extend(event); });
      window.addEventListener('mouseup', function () { self.finish(); });
    }
  };

  // ---------------------------------------------------------------- form
  var form = document.getElementById('opinion-form');
  if (!form) return;

  function byId(id) { return document.getElementById(id); }

  var building = byId('building');
  var unit = byId('unit');
  var nameInput = byId('resident_name');
  var comment = byId('additional_comment');
  var commentLength = byId('comment-length');
  var statement = byId('statement_confirmed');
  var usage = byId('usage_consent');
  var submitButton = byId('submit-button');
  var submitHint = byId('submit-hint');
  var formAlert = byId('form-alert');
  var signatureBox = byId('signature-box');
  var signatureInput = byId('signature-data');
  var confirmModal = byId('confirm-modal');
  var confirmSubmit = byId('confirm-submit');
  var confirmCancel = byId('confirm-cancel');
  var confirmAlert = byId('confirm-alert');

  var TEMP_FAILURE = '일시적으로 제출하지 못했습니다. 잠시 후 다시 시도해 주세요.';
  var ERROR_KEYS = ['opinion_choice', 'residence', 'resident_name', 'signature', 'additional_comment',
    'statement_confirmed', 'usage_consent'];

  var pad = new SignaturePad(byId('signature-pad'), function () {
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

  function normNumber(value) {
    var number = parseInt(value, 10);
    return isNaN(number) ? '' : String(number);
  }

  function normName(value) {
    return value.replace(/\s+/g, ' ').trim();
  }

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

  function focusTarget(key) {
    switch (key) {
      case 'opinion_choice': return form.querySelector('input[name="opinion_choice"]');
      case 'residence': return /^[1-9][0-9]{0,3}$/.test(normNumber(building.value)) ? unit : building;
      case 'resident_name': return nameInput;
      case 'additional_comment': return comment;
      case 'statement_confirmed': return statement;
      case 'usage_consent': return usage;
      default: return null;
    }
  }

  function scrollIntoView(node) {
    try { node.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (err) { node.scrollIntoView(); }
  }

  function showErrors(errors) {
    var first = null;
    for (var i = 0; i < ERROR_KEYS.length; i++) {
      setError(ERROR_KEYS[i], errors[ERROR_KEYS[i]]);
      if (errors[ERROR_KEYS[i]] && !first) first = ERROR_KEYS[i];
    }
    if (first) {
      var target = focusTarget(first);
      if (target) {
        try { target.focus({ preventScroll: true }); } catch (err) { target.focus(); }
      }
      scrollIntoView(fieldBox(first) || form);
    }
    return first;
  }

  function showAlert(node, message) {
    node.textContent = message;
    node.hidden = false;
  }

  function hideAlert(node) {
    node.textContent = '';
    node.hidden = true;
  }

  function validate() {
    var errors = {};
    if (!form.querySelector('input[name="opinion_choice"]:checked')) {
      errors.opinion_choice = '본인의 의견을 선택해 주세요.';
    }
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
    if (comment.value.length > 1000) errors.additional_comment = '기타 전하고 싶은 말은 1,000자 이내로 작성해 주세요.';
    if (!statement.checked) errors.statement_confirmed = '본인 의사 확인에 체크해 주세요.';
    if (!usage.checked) errors.usage_consent = '제출·활용 동의에 체크해 주세요.';
    return errors;
  }

  function updateSubmitState() {
    var ready = statement.checked && usage.checked;
    submitButton.disabled = !ready;
    submitHint.hidden = ready;
  }

  // 동·호수: digits only (full-width digits are converted), at most 4.
  function digitsOnly(input) {
    input.addEventListener('input', function () {
      var cleaned = input.value
        .replace(/[０-９]/g, function (ch) { return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0); })
        .replace(/[^0-9]/g, '')
        .slice(0, 4);
      if (cleaned !== input.value) input.value = cleaned;
      setError('residence', '');
    });
  }
  digitsOnly(building);
  digitsOnly(unit);

  // Enter moves to the next field instead of submitting.
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
  comment.addEventListener('input', function () {
    commentLength.textContent = String(comment.value.length);
    setError('additional_comment', '');
  });
  form.addEventListener('change', function (event) {
    var target = event.target;
    if (target.name === 'opinion_choice') setError('opinion_choice', '');
    if (target === statement) setError('statement_confirmed', '');
    if (target === usage) setError('usage_consent', '');
    updateSubmitState();
  });

  var submitting = false;
  var signatureDataUrl = '';
  var mustReload = false; // the document changed while the resident was writing

  function setBusy(busy) {
    confirmSubmit.disabled = busy;
    confirmCancel.disabled = busy;
    confirmSubmit.textContent = busy ? '제출 중…' : '최종 제출';
    if (busy) confirmModal.setAttribute('data-locked', 'true');
    else confirmModal.removeAttribute('data-locked');
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (submitting) return;
    hideAlert(formAlert);
    if (showErrors(validate())) {
      showAlert(formAlert, '확인이 필요한 항목이 있습니다. 표시된 내용을 확인해 주세요.');
      return;
    }
    building.value = normNumber(building.value);
    unit.value = normNumber(unit.value);
    nameInput.value = normName(nameInput.value);
    var choice = form.querySelector('input[name="opinion_choice"]:checked');
    byId('summary-unit').textContent = building.value + '동 ' + unit.value + '호';
    byId('summary-name').textContent = '성명: ' + nameInput.value;
    byId('summary-opinion').textContent = '의견: ' + choice.getAttribute('data-summary');
    signatureDataUrl = pad.toDataURL();
    byId('summary-signature').src = signatureDataUrl;
    hideAlert(confirmAlert);
    window.Modal.open(confirmModal);
  });

  function fail(status, data) {
    submitting = false;
    setBusy(false);
    var message = data.message || TEMP_FAILURE;
    if (data.code === 'document_changed') {
      // The resident has to read the new wording first: the dialog button now reloads the page.
      showAlert(confirmAlert, message);
      mustReload = true;
      confirmSubmit.textContent = '새로고침';
      return;
    }
    if ((status === 409 || status === 422) && data.errors) {
      // Something in the form has to change (duplicate 동·호수, invalid field): show it on the form.
      window.Modal.close(confirmModal, true);
      showErrors(data.errors);
      showAlert(formAlert, message);
      return;
    }
    // Network or server trouble: stay in the dialog so trying again is one tap.
    showAlert(confirmAlert, message);
  }

  confirmSubmit.addEventListener('click', function () {
    if (mustReload) {
      window.location.reload();
      return;
    }
    if (submitting) return;
    submitting = true;
    setBusy(true);
    hideAlert(confirmAlert);
    signatureInput.value = signatureDataUrl;

    var controller = typeof window.AbortController === 'function' ? new window.AbortController() : null;
    var timer = setTimeout(function () { if (controller) controller.abort(); }, 30000);
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
          // The server has committed. replace() drops this form from the history (POST-redirect-GET).
          window.location.replace(result.data.redirect);
          return;
        }
        fail(result.status, result.data);
      })
      .catch(function () {
        clearTimeout(timer);
        fail(0, {});
      });
  });

  // Coming back through the back/forward cache: never leave the dialog stuck in its busy state.
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    submitting = false;
    setBusy(false);
    window.Modal.close(confirmModal, true);
    updateSubmitState();
    pad.resize();
  });

  updateSubmitState();
})();

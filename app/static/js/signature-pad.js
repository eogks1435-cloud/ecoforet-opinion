/* Signature pad shared by the resident forms: normalised strokes, white 600x300 PNG export. */
(function () {
  'use strict';

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
    // Paint the paper white: with a transparent canvas, forced dark modes (Samsung Internet) turn the pad black.
    this.ctx.fillStyle = '#ffffff';
    this.ctx.fillRect(0, 0, this.width, this.height);
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


  window.SignaturePad = SignaturePad;
})();

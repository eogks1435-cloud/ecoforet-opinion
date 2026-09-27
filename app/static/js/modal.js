/* Minimal accessible modal: open/close, Escape, backdrop click, focus return and a simple focus trap. */
(function () {
  'use strict';

  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
    'select:not([disabled]), textarea:not([disabled]), summary';
  var current = null;
  var lastFocus = null;

  function focusables(modal) {
    return Array.prototype.filter.call(modal.querySelectorAll(FOCUSABLE), function (node) {
      return node.offsetWidth > 0 || node.offsetHeight > 0;
    });
  }

  function open(modal, initialFocus) {
    if (current && current !== modal) close(current, true);
    lastFocus = document.activeElement;
    current = modal;
    modal.hidden = false;
    document.documentElement.classList.add('modal-open');
    var target = initialFocus || modal.querySelector('.modal-panel');
    if (target && target.focus) target.focus();
  }

  // A modal marked data-locked (e.g. while submitting) ignores close requests unless forced.
  function close(modal, force) {
    modal = modal || current;
    if (!modal || modal.hidden) return;
    if (!force && modal.getAttribute('data-locked') === 'true') return;
    modal.hidden = true;
    if (current === modal) current = null;
    document.documentElement.classList.remove('modal-open');
    if (lastFocus && lastFocus.focus) {
      try { lastFocus.focus(); } catch (err) { /* element gone */ }
    }
    lastFocus = null;
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest ? event.target.closest('[data-modal-close]') : null;
    if (trigger && current && current.contains(trigger)) close(current, false);
  });

  document.addEventListener('keydown', function (event) {
    if (!current) return;
    if (event.key === 'Escape' || event.key === 'Esc') {
      close(current, false);
      return;
    }
    if (event.key !== 'Tab') return;
    var items = focusables(current);
    if (!items.length) {
      event.preventDefault();
      return;
    }
    var first = items[0];
    var last = items[items.length - 1];
    var panel = current.querySelector('.modal-panel');
    if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  window.Modal = { open: open, close: close };
})();

/* Admin: signature/detail dialog, invalidation dialog and confirmations. */
(function () {
  'use strict';

  function byId(id) { return document.getElementById(id); }

  function setText(id, value) {
    byId(id).textContent = value || '-';
  }

  // Forms that change what residents see ask once more (e.g. publishing a document version).
  document.addEventListener('submit', function (event) {
    var form = event.target;
    var question = form.getAttribute && form.getAttribute('data-confirm');
    if (question && !window.confirm(question)) {
      event.preventDefault();
      return;
    }
    var button = form.querySelector && form.querySelector('button[type="submit"]');
    if (question && button) {
      button.disabled = true;
      button.textContent = '처리 중…';
    }
  });

  var detailModal = byId('detail-modal');
  var invalidateModal = byId('invalidate-modal');
  var invalidateForm = byId('invalidate-form');
  if (!detailModal || !invalidateModal || !window.Modal) return;

  function openDetail(button) {
    var data;
    try {
      data = JSON.parse(button.getAttribute('data-detail'));
    } catch (err) {
      return;
    }
    setText('detail-title', data.title);
    byId('detail-signature').src = '/admin/signatures/' + encodeURIComponent(data.id) + '.png';
    setText('detail-opinion', data.opinion);
    setText('detail-submitted', data.submitted);
    setText('detail-number', data.number);
    setText('detail-status', data.status);
    setText('detail-reason', data.reason);
    byId('detail-reason-row').hidden = !data.reason;
    var version = byId('detail-version');
    version.textContent = data.version;
    version.href = '/admin/document/versions/' + encodeURIComponent(data.version);
    setText('detail-comment', data.comment);
    setText('detail-ip', data.ip);
    setText('detail-ua', data.ua);
    byId('detail-access').open = false;
    window.Modal.open(detailModal);
  }

  function openInvalidate(button) {
    invalidateForm.setAttribute(
      'action', '/admin/submissions/' + encodeURIComponent(button.getAttribute('data-invalidate')) + '/invalidate'
    );
    setText('invalidate-target', button.getAttribute('data-target'));
    byId('invalidate-reason').value = '';
    window.Modal.open(invalidateModal);
  }

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;
    var thumb = event.target.closest('.sig-thumb');
    if (thumb) {
      openDetail(thumb);
      return;
    }
    var invalidate = event.target.closest('[data-invalidate]');
    if (invalidate) openInvalidate(invalidate);
  });

  invalidateForm.addEventListener('submit', function () {
    var button = invalidateForm.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = '처리 중…';
    invalidateModal.setAttribute('data-locked', 'true');
  });
})();

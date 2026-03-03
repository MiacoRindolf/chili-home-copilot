/**
 * Reusable drag-and-drop file upload component for CHILI.
 * Usage:
 *   var uploader = ChiliFileUpload.create(containerEl, {
 *     uploadUrl: '/api/projects/1/files',
 *     accept: '.txt,.pdf,.py',
 *     multiple: true,
 *     onUploadComplete: function(results) { ... },
 *     onError: function(err) { ... },
 *   });
 *   uploader.destroy();
 */
var ChiliFileUpload = (function() {
  'use strict';

  var FILE_ICONS = {
    '.pdf': '\ud83d\udcc4', '.txt': '\ud83d\udcc4', '.md': '\ud83d\udcc4', '.csv': '\ud83d\udcca',
    '.json': '\ud83d\udcdd', '.xml': '\ud83d\udcdd', '.yaml': '\ud83d\udcdd', '.yml': '\ud83d\udcdd',
    '.py': '\ud83d\udc0d', '.js': '\ud83d\udce6', '.ts': '\ud83d\udce6', '.html': '\ud83c\udf10',
    '.css': '\ud83c\udfa8', '.java': '\u2615', '.go': '\ud83d\udc39', '.rs': '\u2699\ufe0f',
    '.c': '\ud83d\udcbb', '.cpp': '\ud83d\udcbb', '.h': '\ud83d\udcbb',
    '.png': '\ud83d\uddbc\ufe0f', '.jpg': '\ud83d\uddbc\ufe0f', '.jpeg': '\ud83d\uddbc\ufe0f',
    '.gif': '\ud83d\uddbc\ufe0f', '.webp': '\ud83d\uddbc\ufe0f',
  };

  function getIcon(filename) {
    var ext = '.' + filename.split('.').pop().toLowerCase();
    return FILE_ICONS[ext] || '\ud83d\udcc1';
  }

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  function create(container, opts) {
    opts = opts || {};

    var zone = document.createElement('div');
    zone.className = 'chili-upload-zone';
    zone.innerHTML =
      '<div class="chili-upload-icon">\ud83d\udcc1</div>' +
      '<div class="chili-upload-text">Drag & drop files here</div>' +
      '<div class="chili-upload-subtext">or <a href="#" class="chili-upload-browse">browse</a></div>' +
      '<div class="chili-upload-progress" style="display:none;">' +
        '<div class="chili-upload-progress-bar"><div class="chili-upload-progress-fill"></div></div>' +
        '<div class="chili-upload-progress-text">Uploading...</div>' +
      '</div>';

    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.style.display = 'none';
    if (opts.accept) fileInput.accept = opts.accept;
    if (opts.multiple !== false) fileInput.multiple = true;

    container.appendChild(zone);
    container.appendChild(fileInput);

    var browseLink = zone.querySelector('.chili-upload-browse');
    var progressWrap = zone.querySelector('.chili-upload-progress');
    var progressFill = zone.querySelector('.chili-upload-progress-fill');
    var progressText = zone.querySelector('.chili-upload-progress-text');

    browseLink.onclick = function(e) { e.preventDefault(); fileInput.click(); };

    fileInput.onchange = function() {
      if (this.files.length > 0) uploadFiles(this.files);
      this.value = '';
    };

    var _dragCount = 0;
    zone.addEventListener('dragenter', function(e) {
      e.preventDefault(); _dragCount++;
      zone.classList.add('chili-upload-hover');
    });
    zone.addEventListener('dragleave', function(e) {
      e.preventDefault(); _dragCount--;
      if (_dragCount <= 0) { _dragCount = 0; zone.classList.remove('chili-upload-hover'); }
    });
    zone.addEventListener('dragover', function(e) { e.preventDefault(); });
    zone.addEventListener('drop', function(e) {
      e.preventDefault(); _dragCount = 0;
      zone.classList.remove('chili-upload-hover');
      if (e.dataTransfer && e.dataTransfer.files.length > 0) {
        uploadFiles(e.dataTransfer.files);
      }
    });

    function uploadFiles(fileList) {
      if (!opts.uploadUrl) return;

      var formData = new FormData();
      for (var i = 0; i < fileList.length; i++) {
        formData.append('files', fileList[i]);
      }

      progressWrap.style.display = '';
      progressFill.style.width = '0%';
      progressText.textContent = 'Uploading ' + fileList.length + ' file(s)...';

      var xhr = new XMLHttpRequest();
      xhr.open('POST', opts.uploadUrl, true);

      xhr.upload.onprogress = function(e) {
        if (e.lengthComputable) {
          var pct = Math.round((e.loaded / e.total) * 100);
          progressFill.style.width = pct + '%';
          progressText.textContent = 'Uploading... ' + pct + '%';
        }
      };

      xhr.onload = function() {
        progressWrap.style.display = 'none';
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            var data = JSON.parse(xhr.responseText);
            if (opts.onUploadComplete) opts.onUploadComplete(data.results || []);
          } catch(e) {
            if (opts.onError) opts.onError('Invalid response');
          }
        } else {
          if (opts.onError) opts.onError('Upload failed: ' + xhr.status);
        }
      };

      xhr.onerror = function() {
        progressWrap.style.display = 'none';
        if (opts.onError) opts.onError('Network error');
      };

      xhr.send(formData);
    }

    function setUrl(url) { opts.uploadUrl = url; }
    function destroy() { zone.remove(); fileInput.remove(); }

    return { el: zone, setUrl: setUrl, destroy: destroy, getIcon: getIcon, formatSize: formatSize };
  }

  return { create: create, getIcon: getIcon, formatSize: formatSize };
})();

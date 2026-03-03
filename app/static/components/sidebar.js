/**
 * Reusable sidebar manager for CHILI chat with project support.
 * Manages project listing, conversation rendering (grouped and ungrouped),
 * drag-to-assign, and project CRUD UI.
 */
var ChiliSidebar = (function() {
  'use strict';

  var _projects = [];
  var _convos = [];
  var _expandedProjects = {};
  var _callbacks = {};

  function init(opts) {
    _callbacks = opts || {};
    _expandedProjects = JSON.parse(localStorage.getItem('chili-expanded-projects') || '{}');
  }

  function setData(projects, convos) {
    _projects = projects || [];
    _convos = convos || [];
  }

  function render(containerEl, currentConvoId, currentProjectId) {
    containerEl.innerHTML = '';

    var ungrouped = _convos.filter(function(c) { return !c.project_id; });
    var grouped = {};
    _convos.forEach(function(c) {
      if (c.project_id) {
        if (!grouped[c.project_id]) grouped[c.project_id] = [];
        grouped[c.project_id].push(c);
      }
    });

    if (ungrouped.length > 0) {
      var chatLabel = document.createElement('div');
      chatLabel.className = 'sidebar-section-label';
      chatLabel.textContent = 'Chats';
      containerEl.appendChild(chatLabel);
      ungrouped.forEach(function(c) {
        containerEl.appendChild(_createConvoItem(c, currentConvoId));
      });
    }

    if (_projects.length > 0) {
      var projLabel = document.createElement('div');
      projLabel.className = 'sidebar-section-label';
      projLabel.style.marginTop = ungrouped.length > 0 ? '12px' : '0';
      projLabel.textContent = 'Projects';
      containerEl.appendChild(projLabel);

      _projects.forEach(function(p) {
        var projGroup = _createProjectGroup(p, grouped[p.id] || [], currentConvoId);
        containerEl.appendChild(projGroup);
      });
    }

    if (ungrouped.length === 0 && _projects.length === 0) {
      var empty = document.createElement('div');
      empty.style.cssText = 'padding:24px 12px;text-align:center;color:var(--text-muted);font-size:13px;';
      empty.textContent = 'No conversations yet. Start chatting!';
      containerEl.appendChild(empty);
    }
  }

  function _createConvoItem(c, currentConvoId) {
    var item = document.createElement('div');
    item.className = 'convo-item' + (c.id === currentConvoId ? ' active' : '');
    item.draggable = true;
    item.dataset.convoId = c.id;

    item.innerHTML =
      '<span class="convo-title">' + _esc(c.title) + '</span>' +
      '<button class="convo-delete" title="Delete">&times;</button>';

    item.querySelector('.convo-title').onclick = function() {
      if (_callbacks.onSwitchConvo) _callbacks.onSwitchConvo(c.id);
    };
    item.querySelector('.convo-delete').onclick = function(e) {
      e.stopPropagation();
      if (_callbacks.onDeleteConvo) _callbacks.onDeleteConvo(c.id);
    };

    item.addEventListener('dragstart', function(e) {
      e.dataTransfer.setData('text/plain', String(c.id));
      e.dataTransfer.effectAllowed = 'move';
      item.style.opacity = '0.5';
    });
    item.addEventListener('dragend', function() {
      item.style.opacity = '1';
    });

    return item;
  }

  function _createProjectGroup(project, convos, currentConvoId) {
    var group = document.createElement('div');
    group.className = 'project-group';

    var isExpanded = _expandedProjects[project.id] !== false;

    var header = document.createElement('div');
    header.className = 'project-header';

    header.innerHTML =
      '<span class="project-color-dot" style="background:' + _esc(project.color || '#6366f1') + ';"></span>' +
      '<span class="project-expand">' + (isExpanded ? '&#9662;' : '&#9656;') + '</span>' +
      '<span class="project-name">' + _esc(project.name) + '</span>' +
      '<span class="project-badge" title="' + (project.file_count || 0) + ' files">' + (project.file_count || 0) + ' \ud83d\udcc1</span>' +
      '<button class="project-menu-btn" title="Project settings">\u22ef</button>';

    header.querySelector('.project-name').onclick = function() { toggleExpand(); };
    header.querySelector('.project-expand').onclick = function() { toggleExpand(); };

    var menuBtn = header.querySelector('.project-menu-btn');
    menuBtn.onclick = function(e) {
      e.stopPropagation();
      if (_callbacks.onProjectMenu) _callbacks.onProjectMenu(project, menuBtn);
    };

    // Drop target
    header.addEventListener('dragover', function(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      header.classList.add('drop-target');
    });
    header.addEventListener('dragleave', function() {
      header.classList.remove('drop-target');
    });
    header.addEventListener('drop', function(e) {
      e.preventDefault();
      header.classList.remove('drop-target');
      var convoId = parseInt(e.dataTransfer.getData('text/plain'));
      if (convoId && _callbacks.onAssignConvo) {
        _callbacks.onAssignConvo(project.id, convoId);
      }
    });

    group.appendChild(header);

    var list = document.createElement('div');
    list.className = 'project-convo-list';
    list.style.display = isExpanded ? '' : 'none';

    if (convos.length === 0) {
      var placeholder = document.createElement('div');
      placeholder.className = 'project-empty';
      placeholder.textContent = 'Drop chats here';
      list.appendChild(placeholder);
    } else {
      convos.forEach(function(c) {
        var item = _createConvoItem(c, currentConvoId);
        item.classList.add('project-convo-item');
        list.appendChild(item);
      });
    }

    // The list is also a drop target
    list.addEventListener('dragover', function(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    });
    list.addEventListener('drop', function(e) {
      e.preventDefault();
      var convoId = parseInt(e.dataTransfer.getData('text/plain'));
      if (convoId && _callbacks.onAssignConvo) {
        _callbacks.onAssignConvo(project.id, convoId);
      }
    });

    group.appendChild(list);

    function toggleExpand() {
      isExpanded = !isExpanded;
      _expandedProjects[project.id] = isExpanded;
      localStorage.setItem('chili-expanded-projects', JSON.stringify(_expandedProjects));
      header.querySelector('.project-expand').innerHTML = isExpanded ? '&#9662;' : '&#9656;';
      list.style.display = isExpanded ? '' : 'none';
    }

    return group;
  }

  function _esc(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  return { init: init, setData: setData, render: render };
})();

const test = require('node:test');
const assert = require('node:assert/strict');
const presentation = require('../../app/static/js/planner-presentation.js');
const fs = require('node:fs');
const vm = require('node:vm');

const task = (id, fields = {}) => ({ id, status: 'todo', priority: 'medium', title: `Task ${id}`, subtasks: [], ...fields });

test('summary counts each task once and blocked/overdue as a union', () => {
  const child = task(2, { status: 'blocked', end_date: '2026-09-09' });
  const tasks = [task(1, { subtasks: [child] }), child, task(3, { status: 'done', end_date: '2026-09-01' }), task(4, { status: 'in_progress', end_date: '2026-09-10' })];
  assert.deepEqual(presentation.summarize(tasks, '2026-09-10'), {
    total: 4, done: 1, open: 3, inProgress: 1, blocked: 1, overdue: 1, attention: 1, todo: 1, percent: 25,
  });
  assert.deepEqual(presentation.summarize([], '2026-09-10'), {
    total: 0, done: 0, open: 0, inProgress: 0, blocked: 0, overdue: 0, attention: 0, todo: 0, percent: 0,
  });
});

test('due today is not overdue and the calendar day is local', () => {
  assert.equal(presentation.localDate(new Date(2026, 8, 10, 23, 59)), '2026-09-10');
  assert.equal(presentation.overdue(task(1, { end_date: '2026-09-10' }), '2026-09-10'), false);
  assert.equal(presentation.overdue(task(1, { status: 'done', end_date: '2026-09-09' }), '2026-09-10'), false);
  assert.equal(presentation.overdue(task(1, { end_date: null }), '2026-09-10'), false);
});

test('priority ordering uses recorded priority and date without mutating the source', () => {
  const tasks = [task(1, { priority: 'low' }), task(2, { priority: 'high' }), task(3, { priority: 'critical' }), task(4, { priority: 'high', end_date: '2026-09-10' })];
  assert.deepEqual(presentation.sortByPriority(tasks).map(t => t.id), [3, 4, 2, 1]);
  assert.deepEqual(tasks.map(t => t.id), [1, 2, 3, 4]);
});

test('filters find matching subtasks independently of parent status', () => {
  const tasks = [task(1, { status: 'done', subtasks: [task(2, { assigned_to: 9, description: 'Unique receipt', labels: [{ id: 7 }] })] })];
  const filtered = presentation.flatten(tasks).filter(t => presentation.matches(t, { assignee: '9', search: 'receipt', label: '7' }, 'open'));
  assert.deepEqual(filtered.map(t => t.id), [2]);
  assert.equal(presentation.matches(tasks[0], {}, 'done'), true);
  assert.equal(presentation.matches(tasks[0], {}, 'attention', '2026-09-10'), false);
});

test('description formatting is safe, structured, and leaves its source unchanged', () => {
  const source = '# Evidence\r\n\r\n**Passed** and `a < b`\r\n- first\r\n- second\r\n\r\n```html\r\n<img src=x onerror=alert(1)>\r\n```\r\n<script>alert(1)</script>\r\n[unsafe](javascript:alert(1))';
  const before = source;
  const output = presentation.description(source);
  assert.match(output, /<h3>Evidence<\/h3>/);
  assert.match(output, /<strong>Passed<\/strong>/);
  assert.match(output, /<code>a &lt; b<\/code>/);
  assert.match(output, /<ul><li>first<\/li><li>second<\/li><\/ul>/);
  assert.match(output, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(output, /<script|<img|<a[ >]/);
  assert.equal(source, before);
});

test('long descriptions and unclosed code fences retain their final content', () => {
  const long = 'Earlier evidence\n\n'.repeat(1000) + '```\nfinal <receipt>';
  const output = presentation.description(long);
  assert.ok(output.endsWith('<pre><code>final &lt;receipt&gt;</code></pre>'));
  assert.equal((output.match(/Earlier evidence/g) || []).length, 1000);
});

test('numbered evidence steps keep their recorded numbers', () => {
  assert.equal(presentation.description('3. Verify\n7. Reconcile'), '<ol><li value="3">Verify</li><li value="7">Reconcile</li></ol>');
});

function detailSaveHarness({ source, title = 'Original title', draft, editing = false, fails = false }) {
  const html = fs.readFileSync(require.resolve('../../app/templates/planner.html'), 'utf8');
  const code = html.slice(html.indexOf('function saveDetailFields() {'), html.indexOf('function deleteTaskFromDetail() {'));
  const calls = [];
  const elements = {
    'detail-title': { textContent: title },
    'detail-desc': { value: draft == null ? source.replace(/\r\n/g, '\n') : draft },
    'detail-save-status': { textContent: '' },
    'description-save-btn': { disabled: false },
  };
  const context = {
    detailTask: { id: 1, title: 'Original title', description: source }, detailTaskId: 1,
    titleBaselineValue: 'Original title', detailPanelGeneration: 1, detailSavePending: null,
    detailLoadSequence: 0, detailFieldPending: {}, getMyRole: () => 'owner',
    descriptionEditing: editing, descriptionBaselineValue: source.replace(/\r\n/g, '\n'),
    document: { getElementById: id => elements[id] },
    fetch: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return { ok: !fails, json: async () => ({ ok: true }) };
    },
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  return { context, calls, elements };
}

test('reading and closing a CRLF description does not write normalized source', async () => {
  const harness = detailSaveHarness({ source: '  Evidence\r\n\r\nFinal receipt  \r\n' });
  assert.equal(await harness.context.saveDetailFields(), true);
  assert.equal(harness.calls.length, 0);
});

test('a title-only edit preserves the untouched original description', async () => {
  const harness = detailSaveHarness({ source: '  Evidence\r\n', title: 'Updated title' });
  await harness.context.saveDetailFields();
  assert.deepEqual(harness.calls[0].body, { title: 'Updated title' });
});

test('edited-description payload preserves whitespace and failed saves retain the draft', async () => {
  const draft = '  ## Evidence\n\nNew receipt  \n';
  const success = detailSaveHarness({ source: 'Earlier receipt', draft, editing: true });
  assert.equal(await success.context.saveDetailFields(), true);
  assert.equal(success.calls[0].body.description, draft);
  const failure = detailSaveHarness({ source: 'Earlier receipt', draft, editing: true, fails: true });
  assert.equal(await failure.context.saveDetailFields(), false);
  assert.equal(failure.elements['detail-desc'].value, draft);
  assert.match(failure.elements['detail-save-status'].textContent, /Could not save/);
  assert.equal(failure.elements['description-save-btn'].disabled, false);
});

function loadTemplateFunction(context, name) {
  const html = fs.readFileSync(require.resolve('../../app/templates/planner.html'), 'utf8');
  const start = html.indexOf('function ' + name + '(');
  assert.notEqual(start, -1);
  const end = html.indexOf('\nfunction ', start + 1);
  vm.runInContext(html.slice(start, end < 0 ? undefined : end), context);
}

test('typing during save retains the newer draft and shares one pending PUT', async () => {
  const {context, elements} = detailSaveHarness({source:'Before',draft:'Draft sent',editing:true});
  let resolve, requests = 0;
  context.fetch = () => { requests++; return new Promise(done => { resolve = done; }); };
  const first = context.saveDetailFields();
  assert.equal(context.saveDetailFields(), first);
  elements['detail-desc'].value = 'Draft sent plus newer typing';
  resolve({ok:true,json:async () => ({ok:true,task:{id:1,title:'Original title',description:'Draft sent'}})});
  assert.equal(await first, false);
  assert.equal(requests, 1);
  assert.equal(context.descriptionEditing, true);
  assert.equal(elements['detail-desc'].value, 'Draft sent plus newer typing');
  assert.equal(context.descriptionBaselineValue, 'Draft sent');
  assert.match(elements['detail-save-status'].textContent, /newer edits/);
});

test('same-task remote rename refreshes an untouched title without passive PUT', async () => {
  const {context, calls, elements} = detailSaveHarness({source:'Before'});
  loadTemplateFunction(context, 'syncDetailTextFields');
  context.detailTask = {id:1,title:'Changed by collaborator',description:'Remote note'};
  context.syncDetailTextFields(context.detailTask, false);
  assert.equal(elements['detail-title'].textContent, 'Changed by collaborator');
  assert.equal(context.titleBaselineValue, 'Changed by collaborator');
  assert.equal(await context.saveDetailFields(), true);
  assert.equal(calls.length, 0);
});

test('description-only edit does not resend an unchanged title', async () => {
  const {context, calls} = detailSaveHarness({source:'Before',draft:'After',editing:true});
  await context.saveDetailFields();
  assert.deepEqual(calls[0].body, {description:'After'});
});

test('a GET started during a PUT cannot restore the old description after save', async () => {
  const {context, elements} = detailSaveHarness({source:'Before',draft:'After',editing:true});
  let putDone, getDone;
  context.window = {};
  context.loadComments = context.loadActivity = () => {};
  context.renderDetailPanel = () => { throw new Error('stale GET rendered'); };
  context.renderDescriptionReader = () => {};
  elements['description-edit-btn'] = {focus: () => {}};
  context.fetch = (_url, options) => new Promise(done => { if(options) putDone=done; else getDone=done; });
  loadTemplateFunction(context, 'loadDetailTask');
  loadTemplateFunction(context, 'saveDescriptionEdit');
  const put = context.saveDescriptionEdit();
  const get = context.loadDetailTask(1);
  putDone({ok:true,json:async () => ({ok:true,task:{id:1,title:'Original title',description:'After'}})});
  await put;
  getDone({ok:true,json:async () => ({task:{id:1,title:'Original title',description:'Before'}})});
  await get;
  assert.equal(context.detailTask.description, 'After');
  assert.equal(context.descriptionBaselineValue, 'After');
});

test('failed field updates restore the authoritative value without resending title', async () => {
  const {context, elements, calls} = detailSaveHarness({source:'Before',fails:true});
  context.detailTask.status='todo';
  context.detailFieldIds={status:'detail-status'};
  elements['detail-status']={value:'done',disabled:false};
  loadTemplateFunction(context, 'updateDetailField');
  assert.equal(await context.updateDetailField('status','done'),false);
  assert.deepEqual(calls[0].body,{status:'done'});
  assert.equal(elements['detail-status'].value,'todo');
  assert.equal(elements['detail-status'].disabled,false);
  assert.match(elements['detail-save-status'].textContent,/saved value has been restored/);
});

test('viewer text saves and field updates do not send mutation requests', async () => {
  const {context,calls} = detailSaveHarness({source:'Before',draft:'After',editing:true});
  context.getMyRole=()=>'viewer';
  loadTemplateFunction(context,'updateDetailField');
  await context.saveDetailFields();
  await context.updateDetailField('status','done');
  assert.equal(calls.length,0);
});

test('project colors reject CSS declarations and HTML attribute injection', () => {
  assert.equal(presentation.color('#fA3'),'#fA3');
  assert.equal(presentation.color('#123abc'),'#123abc');
  assert.equal(presentation.color('red;" onpointerenter="probe()'),'#6366f1');
  assert.equal(presentation.color('url(https://example.invalid)'),'#6366f1');
});

test('closing during a field update retains text typed after the close click', async () => {
  const {context,elements,calls} = detailSaveHarness({source:'Before'});
  context.detailTask.status='todo';
  context.detailFieldIds={status:'detail-status'};
  context.activeProjectId=9;
  context.loadActivity=()=>{};
  elements['detail-status']={value:'done',disabled:false};
  let closed=false;
  elements['detail-panel']={classList:{remove:()=>{closed=true;}}};
  elements['detail-overlay']={classList:{remove:()=>{closed=true;}}};
  let resolve;
  context.fetch=(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return new Promise(done=>{resolve=done;});};
  loadTemplateFunction(context,'updateDetailField');
  loadTemplateFunction(context,'closeDetailPanel');
  const field=context.updateDetailField('status','done');
  const close=context.closeDetailPanel();
  elements['detail-title'].textContent='A title typed while closing';
  resolve({ok:true,json:async()=>({ok:true,task:{id:1,title:'Original title',description:'Before',status:'done'}})});
  await field;
  await close;
  assert.equal(closed,false);
  assert.equal(context.detailTaskId,1);
  assert.equal(elements['detail-title'].textContent,'A title typed while closing');
  assert.deepEqual(calls[0].body,{status:'done'});
  assert.match(elements['detail-save-status'].textContent,/newer changes are still here/);
});

test('timeline aligns calendar dates west of UTC and escapes stored status/color', () => {
  const previousTimezone = process.env.TZ;
  process.env.TZ = 'America/Los_Angeles';
  try {
    for (const instant of ['2026-09-10T08:00:00Z','2026-09-11T02:00:00Z','2026-11-01T08:30:00Z','2026-11-01T09:30:00Z']) {
      class TestDate extends Date { constructor(...args) { super(...(args.length ? args : [instant])); } }
      const localDay = presentation.localDate(new TestDate());
      const el = {innerHTML:''};
      const context = {
        Date:TestDate, PlannerPresentation:presentation, esc:presentation.escapeHtml,
        document:{getElementById:()=>el}, taskKey:t=>'P-'+t.id,
        activeProject:{color:'red;" onpointerenter="probe()'},
        getFilteredTasks:()=>[task(1,{title:'A date test',status:'todo" onpointerenter="probe()',start_date:localDay,end_date:localDay})],
      };
      vm.createContext(context);
      loadTemplateFunction(context,'renderGanttView');
      context.renderGanttView();
      const day = Number(localDay.slice(-2));
      const weekday = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][new Date(localDay+'T00:00:00Z').getUTCDay()];
      assert.match(el.innerHTML,new RegExp('gantt-day-header today[^\"]*\"><div class="day-num">'+day+'</div><div class="day-name">'+weekday));
      assert.doesNotMatch(el.innerHTML, /" onpointerenter="/);
      assert.match(el.innerHTML,/background:#6366f1;/);
      const bar = el.innerHTML.match(/class="gantt-bar" style="left:([\d.]+)%;width:([\d.]+)%/);
      const headers = [...el.innerHTML.matchAll(/class="gantt-day-header/g)];
      assert.ok(bar);
      assert.ok(Math.abs(Number(bar[2])-100/headers.length)<1e-8);
    }
  } finally {
    if (previousTimezone === undefined) delete process.env.TZ;
    else process.env.TZ=previousTimezone;
  }
});

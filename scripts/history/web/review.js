'use strict';

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const fmt = value => value ? new Date(value).toLocaleString() : '';
const imageURL = artifact => '/api/artifacts/' + encodeURIComponent(artifact.id);
const pretty = value => esc(JSON.stringify(value, null, 2));
const state = {
  rows: [], active: null, run: null, drafts: new Map(), filters: {},
  swapped: false, compare: false, loading: false, selection: 0, queueRequest: 0, debugTab: 'prompts',
};
let saveChain = Promise.resolve();
let announcementTimer;
let statsRequest = 0;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).error || message; } catch {}
    throw new Error(message);
  }
  return response.json();
}

function announce(message) {
  $('#announcement').textContent = message;
  clearTimeout(announcementTimer);
  announcementTimer = setTimeout(() => { $('#announcement').textContent = ''; }, 5000);
}

function badge(rating) {
  return rating ? `<span class="score-badge">★ ${rating.rating}/5</span>` : '<span class="score-badge unrated">Unrated</span>';
}

function renderQueue() {
  const scrollTop = $('#runs').scrollTop;
  const scrollLeft = $('#runs').scrollLeft;
  $('#queue-count').textContent = `${state.rows.length} runs · newest first`;
  $('#mobile-queue-count').textContent = state.rows.length ? `(${state.rows.length})` : '';
  $('#filter-button').innerHTML = `${Object.values(state.filters).some(Boolean) ? 'Filtered' : 'Filters'} <kbd>/</kbd>`;
  $('#unrated-button').setAttribute('aria-pressed', String(state.filters.rating === 'unrated'));
  $('#runs').innerHTML = state.rows.map(row => `
    <button class="run ${row.id === state.active ? 'active' : ''}" data-run="${esc(row.id)}"
      aria-current="${row.id === state.active}" aria-label="${esc(row.summary.headline || row.target_date || 'Generation run')}, ${row.latest_rating ? row.latest_rating.rating + ' out of 5' : 'unrated'}, ${fmt(row.started_at)}">
      <span class="thumbnail">${row.final_display ? `<img loading="lazy" src="${imageURL(row.final_display)}" alt="Final display">` : '<span>No final display</span>'}</span>
      <span class="run-copy"><span class="run-top"><span class="run-date">${esc(row.target_date || fmt(row.started_at).split(',')[0])}</span>${badge(row.latest_rating)}</span>
      <span class="run-title">${esc(row.summary.headline || row.selected_style || 'Generation run')}</span>
      <span class="meta">${esc(row.selected_style || 'Style unknown')} · <span class="badge ${esc(row.status)}">${esc(row.status)}</span><br>${esc(fmt(row.started_at))}</span></span>
    </button>`).join('') || '<p class="empty">No matching runs.<br>Press / to change filters.</p>';
  $('#runs').scrollTop = scrollTop;
  $('#runs').scrollLeft = scrollLeft;
  wireImageErrors($('#runs'));
}

function markActive() {
  document.querySelectorAll('[data-run]').forEach(element => {
    const active = element.dataset.run === state.active;
    element.classList.toggle('active', active);
    element.setAttribute('aria-current', String(active));
    if (active) element.scrollIntoView({block: 'nearest', inline: 'nearest'});
  });
}

function setQueueOpen(open, restoreFocus = true) {
  const panel = $('#queue-panel');
  const isMobile = matchMedia('(max-width: 800px)').matches;
  document.body.classList.toggle('queue-open', open && isMobile);
  panel.classList.toggle('open', open && isMobile);
  panel.inert = isMobile && !open;
  $('#menu-button').setAttribute('aria-expanded', String(open && isMobile));
  $('#queue-backdrop').hidden = !open || !isMobile;
  if (open && isMobile) {
    const active = panel.querySelector('[data-run].active') || $('#runs');
    requestAnimationFrame(() => active.focus({preventScroll: true}));
  } else if (restoreFocus && isMobile) {
    $('#menu-button').focus({preventScroll: true});
  }
}

async function refreshStats() {
  const request = ++statsRequest;
  try {
    const stats = await api('/api/stats');
    if (request !== statsRequest) return;
    $('#progress').textContent = `${stats.rated} rated · ${stats.unrated_boards} boards to review`;
  } catch { if (request === statsRequest) $('#progress').textContent = 'Progress unavailable'; }
}

// Read every page so keyboard review also reaches runs beyond the API page size.
async function loadQueue(preferred = state.active) {
  const request = ++state.queueRequest;
  const filters = {...state.filters};
  state.loading = true;
  $('#refresh').disabled = true;
  try {
    const rows = [];
    let offset = 0;
    while (true) {
      const query = new URLSearchParams({limit: '200', offset: String(offset)});
      Object.entries(filters).forEach(([key, value]) => { if (value) query.set(key, value); });
      const page = await api('/api/runs?' + query);
      if (request !== state.queueRequest) return;
      rows.push(...page.items);
      offset += page.items.length;
      if (!page.items.length || offset >= page.total) break;
    }
    state.rows = rows;
    renderQueue();
    const next = rows.find(row => row.id === preferred) || rows[0];
    if (next) await selectRun(next.id);
    else {
      state.selection++;
      state.active = null;
      state.run = null;
      $('#detail').innerHTML = '<p class="empty">No matching runs. Press / to change filters.</p>';
    }
    refreshStats();
  } catch (error) {
    announce('Could not load runs: ' + error.message);
    if (!state.rows.length) $('#detail').innerHTML = '<p class="empty error">Could not load runs. Press R to retry.</p>';
  } finally {
    if (request === state.queueRequest) {
      state.loading = false;
      $('#refresh').disabled = false;
    }
  }
}

function getDraft(run) {
  if (!state.drafts.has(run.id)) {
    state.drafts.set(run.id, {
      score: run.latest_rating?.rating || 0,
      comment: run.latest_rating?.comment || '', saved: run.latest_rating,
      pending: 0, error: '',
    });
  } else {
    // A refresh can pick up another tab's changes, while preserving local edits.
    const draft = state.drafts.get(run.id);
    if (!draft.pending && !isDirty(draft)) {
      draft.score = run.latest_rating?.rating || 0;
      draft.comment = run.latest_rating?.comment || '';
      draft.saved = run.latest_rating;
    }
  }
  return state.drafts.get(run.id);
}

function isDirty(draft) {
  return draft.score !== (draft.saved?.rating || 0) || draft.comment.trim() !== (draft.saved?.comment || '');
}

function syncNotes() {
  if (state.run && $('#notes')) state.drafts.get(state.run.id).comment = $('#notes').value;
}

function updateRating() {
  if (!state.run || !$('#notes')) return;
  const draft = state.drafts.get(state.run.id);
  $('#current-score').innerHTML = badge(draft.saved);
  document.querySelectorAll('[data-score]').forEach(button => {
    const score = Number(button.dataset.score);
    button.classList.toggle('selected', score <= draft.score);
    button.classList.toggle('exact', score === draft.score);
    button.setAttribute('aria-pressed', String(score === draft.score));
  });
  const status = $('#save-status');
  status.classList.toggle('error', Boolean(draft.error));
  status.textContent = draft.error ? `Save failed: ${draft.error}. Press Enter to retry.`
    : draft.pending ? 'Saving…'
    : isDirty(draft) ? (draft.score ? 'Unsaved notes · Enter saves and advances' : 'Choose 1–5 to save your notes')
    : draft.saved ? `Saved ${draft.saved.rating}/5 · ${fmt(draft.saved.rated_at)}` : 'Not rated yet · choose 1–5';
}

// Serialize writes: a slow score save must never overwrite a later note save.
// Each operation captures its run ID and text before the user can navigate away.
function saveDraft(id) {
  const draft = state.drafts.get(id);
  if (!draft || !draft.score) {
    if (draft?.comment.trim()) {
      announce('Choose a score from 1 to 5 before saving notes.');
      return Promise.resolve(false);
    }
    return Promise.resolve(true);
  }
  const score = draft.score;
  const comment = draft.comment.trim();
  draft.pending++;
  draft.error = '';
  updateRating();
  const operation = saveChain.then(async () => {
    if (draft.saved?.rating === score && draft.saved?.comment === comment) return true;
    try {
      const result = await api('/api/ratings', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target_type: 'run', target_id: id, rating: score, comment}),
      });
      draft.saved = result;
      draft.error = '';
      const row = state.rows.find(item => item.id === id);
      if (row) row.latest_rating = result;
      if (state.run?.id === id) {
        state.run.latest_rating = result;
        if (state.run.detailsLoaded && !state.run.ratings.some(rating => rating.id === result.id)) state.run.ratings.push(result);
        const history = $('#debug-panel-ratings');
        if (history?.dataset.loaded) history.innerHTML = debugPanel('ratings', state.run);
      }
      // Update this row in place, preserving list focus, scroll and thumbnails.
      const element = Array.from(document.querySelectorAll('[data-run]')).find(item => item.dataset.run === id);
      if (element) {
        element.querySelector('.score-badge').outerHTML = badge(result);
        element.setAttribute('aria-label', `${row.summary.headline || row.target_date || 'Generation run'}, ${result.rating} out of 5, ${fmt(row.started_at)}`);
      }
      refreshStats();
      return true;
    } catch (error) {
      draft.error = error.message;
      announce('Rating was not saved. Your notes are still here; press Enter to retry.');
      return false;
    }
  }).finally(() => {
    draft.pending--;
    updateRating();
  });
  saveChain = operation.catch(() => false);
  return operation;
}

async function commitCurrent() {
  syncNotes();
  return state.run ? saveDraft(state.run.id) : true;
}

async function selectRun(id) {
  if (!state.rows.some(row => row.id === id)) return;
  const request = ++state.selection;
  if (!await commitCurrent() || request !== state.selection) return;
  if (document.body.classList.contains('queue-open')) setQueueOpen(false, false);
  // A rated run can stay visible while its notes are being edited in an
  // unrated/score-filtered queue. Remove it from that queue once we leave it.
  const rating = state.filters.rating;
  const filtered = state.rows.filter(row => row.id === id || !rating
    || (rating === 'unrated' ? !row.latest_rating
      : rating === 'rated' ? Boolean(row.latest_rating)
      : row.latest_rating?.rating === Number(rating)));
  if (filtered.length !== state.rows.length) {
    state.rows = filtered;
    renderQueue();
  }
  // Each run opens on the final board; preview is an explicit temporary swap.
  state.swapped = false;
  state.active = id;
  const row = state.rows.find(item => item.id === id);
  // List responses contain everything needed to rate a board. Render at once;
  // downloading logs and snapshots must not swallow a fast next score hotkey.
  state.run = {...row, stages: [], logs: [], snapshots: [], artifacts: [], ratings: [], detailsLoaded: false};
  getDraft(state.run);
  markActive();
  renderDetail();
  $('#workspace').scrollTop = 0;
  $('#workspace').focus({preventScroll: true});
  const next = state.rows[state.rows.findIndex(item => item.id === id) + 1];
  if (next?.final_display) { const image = new Image(); image.src = imageURL(next.final_display); }
  api('/api/runs/' + encodeURIComponent(id)).then(run => {
    if (request !== state.selection) return;
    state.run = {...run, latest_rating: state.drafts.get(id).saved, detailsLoaded: true};
    renderPrompt(state.run);
    if ($('#run-details').open) renderDebug(state.run);
  }).catch(error => {
    if (request !== state.selection) return;
    $('#debug-content').textContent = 'Could not load run details. Press R to retry.';
    announce('Board details could not load: ' + error.message);
  });
}

function navigate(delta) {
  const index = state.rows.findIndex(row => row.id === state.active);
  const next = state.rows[index + delta];
  if (next) return selectRun(next.id);
  announce(delta > 0 ? 'End of the queue.' : 'Start of the queue.');
}

let advancing = false;
async function saveAndAdvance() {
  if (advancing || !state.run) return;
  const id = state.run.id;
  const selection = state.selection;
  if (!state.drafts.get(id).score) {
    announce('Choose a score from 1 to 5 first.');
    return;
  }
  advancing = true;
  try {
    if (!await commitCurrent() || state.active !== id || state.selection !== selection) return;
    const index = state.rows.findIndex(row => row.id === id);
    const ordered = [...state.rows.slice(index + 1), ...state.rows.slice(0, index)];
    const next = ordered.find(row => row.final_display && !row.latest_rating);
    if (next) await selectRun(next.id);
    else {
      $('#notes').blur();
      $('#workspace').focus({preventScroll: true});
      if (!$('#queue-done')) $('#run-heading').insertAdjacentHTML('beforebegin', '<p id="queue-done" class="queue-done" role="status">All boards in this queue are rated. Use J / K to revisit runs, or / to change filters.</p>');
      announce('All boards in this queue are rated.');
    }
  } finally { advancing = false; }
}

function rate(score) {
  if (!state.run) return;
  syncNotes();
  const id = state.run.id;
  state.drafts.get(id).score = score;
  updateRating();
  $('#notes').focus({preventScroll: true});
  saveDraft(id);
}

function imageFigure(artifact, label, view) {
  const reference = view === 'preview';
  const ratio = artifact ? `${artifact.width || 960}/${artifact.height || 640}` : '3/2';
  const caption = reference
    ? label === 'Before palette'
      ? 'Full-color preview before palette reduction. Press P to show it large.'
      : 'Archived preview; palette stage unknown. Press P to show it large.'
    : '';
  return `<figure class="${reference ? 'reference' : 'primary'}" data-view="${view}"><figcaption class="image-label"><span>${esc(label)}</span>${artifact ? `<a href="${imageURL(artifact)}" target="_blank" rel="noopener">Full size ↗</a>` : ''}</figcaption>
    ${artifact ? `<div class="image-frame" style="aspect-ratio:${ratio}"><span class="image-loading">Loading board…</span><img class="board-image" src="${imageURL(artifact)}" alt="${esc(label)}"></div>` : `<div class="missing-image">No ${reference ? 'reference image' : 'final display'} saved for this run.</div>`}
    ${artifact ? `<p class="image-meta">${esc(artifact.kind)} · ${artifact.width || '?'} × ${artifact.height || '?'}</p>` : ''}
    ${caption ? `<p class="reference-caption">${esc(caption)}</p>` : ''}</figure>`;
}

function updateImageView() {
  const main = $('#images .image-main');
  const side = $('#images .reference-column');
  const final = $('#images [data-view="final"]');
  const preview = $('#images [data-view="preview"]');
  if (!main || !side || !final || !preview) return;

  const selected = state.swapped ? preview : final;
  const secondary = state.swapped ? final : preview;
  const prompt = $('#prompt-context');
  main.replaceChildren(selected);
  side.insertBefore(secondary, prompt);
  selected.className = 'primary';
  secondary.className = 'reference';

  const swapButton = $('#swap-view');
  swapButton.disabled = !state.run.reference;
  swapButton.setAttribute('aria-pressed', String(state.swapped));
  swapButton.setAttribute('aria-label', 'Toggle preview image');
  $('#swap-view .mobile-view-label').textContent = state.swapped ? 'Preview ✓' : 'Preview';
  $('#compare-view').setAttribute('aria-pressed', String(state.compare));
  $('#images').classList.toggle('compare', state.compare);
}

function renderImages() {
  const run = state.run;
  const referenceLabel = run.reference?.kind === 'final_display_pre_quantize' ? 'Before palette' : 'Preview (palette stage unknown)';
  $('#images').className = 'image-layout';
  $('#images').innerHTML = '<div class="image-main">' + imageFigure(run.final_display, 'Final display · palette reduced', 'final')
    + '</div><div class="reference-column">' + imageFigure(run.reference, referenceLabel, 'preview')
    + '<section id=prompt-context class=prompt-context aria-label="Prompt context"></section></div>';
  renderPrompt(run);
  updateImageView();
  $('#swap-view .desktop-view-label').textContent = 'Swap view';
  wireImageErrors($('#images'));
}

function renderDetail() {
  const run = state.run;
  const draft = state.drafts.get(run.id);
  const index = state.rows.findIndex(row => row.id === run.id);
  $('#detail').innerHTML = `
    <div class="review-bar">
      <div class="rating-top"><div><div class="rating-heading"><span class="rating-title">Overall board quality</span><span id="current-score">${badge(run.latest_rating)}</span></div>
        <div class="stars" role="group" aria-label="Overall board score, 1 to 5">${[1,2,3,4,5].map(score => `<button type="button" class="star" data-score="${score}" aria-label="Rate ${score} out of 5" aria-pressed="false">${score} ★</button>`).join('')}</div></div>
        <div class="nav-actions"><button class="quiet" id="previous-run" ${index === 0 ? 'disabled' : ''} aria-label="Previous run">↑ <kbd>K</kbd></button><button class="quiet" id="next-run" ${index === state.rows.length - 1 ? 'disabled' : ''} aria-label="Next run">↓ <kbd>J</kbd></button></div>
      </div>
      <div class="notes-row"><label class="notes-field" for="notes"><span>Notes (optional) <kbd>N</kbd></span><textarea id="notes" maxlength="4000" placeholder="What worked? What should change? Enter saves and advances.">${esc(draft.comment)}</textarea></label><button id="save-next">Save & next <kbd>Enter</kbd></button></div>
      <div class="save-line"><span id="save-status" role="status"></span><span>1–5 saves score → notes · Shift + Enter for a new line</span></div>
    </div>
    <div class="review-body"><div class="run-heading" id="run-heading"><div><h2>${esc(run.summary.headline || run.target_date || 'Generation run')}</h2><p class="meta">${esc(run.target_date || '')} · ${esc(run.selected_style || 'Style unknown')} · ${esc(fmt(run.started_at))} <span class="badge ${esc(run.status)}">${esc(run.status)}</span></p></div><span class="position">${index + 1} / ${state.rows.length}</span></div>
      <div class="view-controls"><span class="eyebrow">THE BOARD YOU’RE RATING</span><div><button id="swap-view" class="quiet" aria-pressed="false"><span class="desktop-view-label">Swap view</span><span class="mobile-view-label"></span> <kbd>P</kbd></button><button id="compare-view" class="quiet" aria-pressed="false">Compare <kbd>C</kbd></button></div></div>
      <div id="images" class="image-layout"></div>
      <div class="below-images"><details id="run-details" class="section-details"><summary>Debug · prompts, pipeline & run data <kbd>D</kbd></summary><div id="debug-content" class="detail-content"></div></details></div>
    </div>`;
  renderImages();
  updateRating();
  document.querySelectorAll('[data-score]').forEach(button => { button.onclick = () => rate(Number(button.dataset.score)); });
  $('#previous-run').onclick = () => navigate(-1);
  $('#next-run').onclick = () => navigate(1);
  $('#save-next').onclick = saveAndAdvance;
  $('#swap-view').onclick = () => {
    if (!state.run.reference) return;
    state.swapped = !state.swapped;
    updateImageView();
  };
  $('#compare-view').onclick = () => { state.compare = !state.compare; updateImageView(); };
  $('#notes').oninput = event => {
    state.drafts.get(run.id).comment = event.currentTarget.value;
    updateRating();
  };
  $('#notes').onblur = event => {
    state.drafts.get(run.id).comment = event.currentTarget.value;
    saveDraft(run.id);
  };
  $('#notes').onkeydown = event => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (!event.repeat) saveAndAdvance(); }
    if (event.key === 'Escape') { event.preventDefault(); $('#notes').blur(); $('#workspace').focus({preventScroll: true}); }
  };
  $('#run-details').ontoggle = () => {
    if ($('#run-details')?.open && !$('#debug-content').dataset.loaded) {
      if (state.run.detailsLoaded) renderDebug(state.run);
      else $('#debug-content').textContent = 'Loading run details…';
    }
  };
}

function snapshotMarkup(snapshot, title = snapshot.kind) {
  return `<details class="snapshot" data-snapshot="${esc(snapshot.id)}"><summary>${esc(title)} <span class="meta">${(snapshot.byte_size / 1024).toFixed(1)} KB · ${esc(snapshot.sha256.slice(0, 12))}</span></summary>
    <pre data-payload>Loading…</pre><details class="snapshot-metadata"><summary>Snapshot metadata</summary><pre>${pretty(snapshot)}</pre></details></details>`;
}

function wireSnapshots(container) {
  container.querySelectorAll('[data-snapshot]').forEach(element => {
    element.addEventListener('toggle', async () => {
      if (!element.open || element.dataset.loaded || element.dataset.loading) return;
      element.dataset.loading = 'true';
      try {
        const snapshot = await api('/api/snapshots/' + encodeURIComponent(element.dataset.snapshot));
        element.querySelector('[data-payload]').innerHTML = pretty(snapshot.payload);
        element.dataset.loaded = 'true';
      } catch (error) {
        element.querySelector('[data-payload]').textContent = 'Could not load snapshot: ' + error.message + '. Close and reopen to retry.';
      } finally { delete element.dataset.loading; }
    });
  });
}

function renderPrompt(run) {
  const container = $('#prompt-context');
  if (!container) return;
  const prompts = run.snapshots.filter(snapshot => snapshot.kind === 'image_prompt');
  container.innerHTML = `<h3>Prompt <kbd>G</kbd></h3>
    ${run.summary.illustration_prompt ? `<p class="prompt-direction">${esc(run.summary.illustration_prompt)}</p>` : '<p class="meta">No illustration direction recorded.</p>'}
    ${prompts.map((snapshot, index) => snapshotMarkup(snapshot, prompts.length > 1 ? `Full image prompt ${index + 1}` : 'Full image prompt')).join('')}
    ${!run.detailsLoaded ? '<p class="meta">Loading full prompts…</p>' : ''}
    <button type="button" id="all-prompts" class="quiet">All prompts & model requests ↗</button>`;
  wireSnapshots(container);
  $('#all-prompts').onclick = () => openDebug('prompts');
}

const DEBUG_TABS = [
  ['prompts', 'Prompts & requests'], ['pipeline', 'Pipeline'], ['events', 'Events'],
  ['snapshots', 'Snapshots & outputs'], ['artifacts', 'Artifacts'], ['run', 'Run & settings'], ['ratings', 'Ratings'],
];

function debugPanel(key, run) {
  switch (key) {
    case 'prompts': {
      const snapshots = run.snapshots.filter(snapshot => /prompt|request|generation_input|brief_model_response/.test(snapshot.kind));
      return `<h3>Prompts, model inputs & responses</h3>${run.summary.illustration_prompt ? '<div class="prompt"><b>Illustration direction</b><br>' + esc(run.summary.illustration_prompt) + '</div>' : ''}
        ${snapshots.map(snapshot => snapshotMarkup(snapshot)).join('') || '<p class="meta">No model prompts or requests saved.</p>'}`;
    }
    case 'pipeline':
      return `<h3>Pipeline</h3>${run.stages.map(stage => `<div class="stage"><b>${esc(stage.stage_name)}</b> <span class="badge ${esc(stage.status)}">${esc(stage.status)}</span> <span class="meta">${stage.duration_ms ?? '?'} ms ${stage.exit_code != null ? '· exit ' + stage.exit_code : ''}</span>${stage.message ? '<br>' + esc(stage.message) : ''}<details><summary>Stage details & timestamps</summary><pre>${pretty(stage)}</pre></details></div>`).join('') || '<p class="meta">No stage data.</p>'}`;
    case 'events':
      return `<h3>Generation events</h3>${run.logs.map(log => `<div class="log"><b class="${log.level === 'error' ? 'error' : ''}">${esc(log.level.toUpperCase())}</b> <span class="meta">${esc(fmt(log.occurred_at))}</span> ${esc(log.component)} / ${esc(log.event_type)} — ${esc(log.message || '')}${Object.keys(log.data || {}).length ? '<pre>' + pretty(log.data) + '</pre>' : ''}<details><summary>Complete event</summary><pre>${pretty(log)}</pre></details></div>`).join('') || '<p class="meta">No events.</p>'}`;
    case 'snapshots':
      return `<h3>Input snapshots & model outputs</h3>${run.snapshots.map(snapshot => snapshotMarkup(snapshot)).join('') || '<p class="meta">No snapshots.</p>'}`;
    case 'artifacts':
      return `<h3>All saved artifacts</h3><div class="other-images">${run.artifacts.map(artifact => `<div>${artifact.mime_type.startsWith('image/') ? imageFigure(artifact, artifact.kind, false) : `<a href="${imageURL(artifact)}" target="_blank" rel="noopener">${esc(artifact.kind)} ↗</a>`}<p class="meta">${(artifact.byte_size / 1024).toFixed(1)} KB · ${esc(artifact.sha256)}</p><details><summary>Artifact metadata</summary><pre>${pretty(artifact)}</pre></details></div>`).join('') || '<p class="meta">No artifacts.</p>'}</div>`;
    case 'run': {
      const metadata = Object.fromEntries(Object.entries(run).filter(([key]) => !['stages', 'logs', 'snapshots', 'artifacts', 'ratings', 'detailsLoaded'].includes(key)));
      return `<h3>Run metadata & settings</h3><a href="/api/runs/${encodeURIComponent(run.id)}" target="_blank" rel="noopener">Complete run JSON ↗</a>${run.error_summary ? '<p class="error">' + esc(run.error_summary) + '</p>' : ''}<pre>${pretty(metadata)}</pre><details data-history-stats><summary>History statistics · status counts, styles & failed stages</summary><pre>Loading…</pre></details>`;
    }
    case 'ratings':
      return `<h3>Rating history</h3>${run.ratings.length ? '<pre>' + pretty(run.ratings) + '</pre>' : '<p class="meta">No saved feedback.</p>'}`;
  }
}

function selectDebugTab(key, focus = false) {
  state.debugTab = key;
  const container = $('#debug-content');
  container.querySelectorAll('[role="tab"]').forEach(tab => {
    const selected = tab.dataset.debugTab === key;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focus) tab.focus();
  });
  container.querySelectorAll('[role="tabpanel"]').forEach(panel => {
    panel.hidden = panel.dataset.debugPanel !== key;
    if (!panel.hidden && !panel.dataset.loaded) {
      panel.innerHTML = debugPanel(key, state.run);
      panel.dataset.loaded = 'true';
      wireSnapshots(panel);
      wireImageErrors(panel);
      const stats = panel.querySelector('[data-history-stats]');
      if (stats) stats.addEventListener('toggle', async () => {
        if (!stats.open || stats.dataset.loaded) return;
        try {
          stats.querySelector('pre').innerHTML = pretty(await api('/api/stats'));
          stats.dataset.loaded = 'true';
        } catch (error) { stats.querySelector('pre').textContent = 'Could not load statistics: ' + error.message + '. Close and reopen to retry.'; }
      });
    }
  });
}

function renderDebug(run) {
  const container = $('#debug-content');
  container.dataset.loaded = 'true';
  container.innerHTML = `<p class="meta">${esc(run.id)} · ${esc(fmt(run.started_at))} → ${esc(fmt(run.completed_at))}<br>${esc(run.image_provider || 'Provider unknown')} · ${esc(run.brief_source || 'Brief source unknown')}</p>
    <div class="debug-tabs" role="tablist" aria-label="Run debug information">${DEBUG_TABS.map(([key, label]) => `<button type="button" role="tab" id="debug-tab-${key}" data-debug-tab="${key}" aria-controls="debug-panel-${key}" aria-selected="false" tabindex="-1">${label}</button>`).join('')}</div>
    ${DEBUG_TABS.map(([key]) => `<div role="tabpanel" id="debug-panel-${key}" data-debug-panel="${key}" aria-labelledby="debug-tab-${key}" tabindex="0" hidden></div>`).join('')}`;
  container.querySelectorAll('[role="tab"]').forEach(tab => {
    tab.onclick = () => selectDebugTab(tab.dataset.debugTab);
    tab.onkeydown = event => {
      const index = DEBUG_TABS.findIndex(([key]) => key === tab.dataset.debugTab);
      const targets = {ArrowRight: (index + 1) % DEBUG_TABS.length, ArrowLeft: (index + DEBUG_TABS.length - 1) % DEBUG_TABS.length, Home: 0, End: DEBUG_TABS.length - 1};
      if (event.key in targets) {
        event.preventDefault();
        selectDebugTab(DEBUG_TABS[targets[event.key]][0], true);
      }
    };
  });
  selectDebugTab(state.debugTab);
}

function openDebug(tab) {
  if (!state.run) return;
  if (tab) state.debugTab = tab;
  $('#run-details').open = true;
  if (state.run.detailsLoaded) {
    if (!$('#debug-content').dataset.loaded) renderDebug(state.run);
    else selectDebugTab(state.debugTab);
  }
  $('#run-details').scrollIntoView({block: 'start'});
}

function toggleDebug() {
  if (!$('#run-details')) return;
  if (!$('#run-details').open) openDebug();
  else { $('#run-details').open = false; $('#workspace').scrollTop = 0; }
}

function togglePrompt() {
  const prompt = $('#prompt-context details[data-snapshot]');
  if (prompt) prompt.open = !prompt.open;
  $('#prompt-context')?.scrollIntoView({block: 'nearest'});
}

function wireImageErrors(root) {
  root.querySelectorAll('img').forEach(image => {
    image.onload = () => image.classList.add('loaded');
    image.onerror = () => {
      const placeholder = document.createElement('span');
      placeholder.className = image.classList.contains('board-image') ? 'missing-image' : '';
      placeholder.textContent = 'Image unavailable';
      const frame = image.closest('.image-frame');
      if (frame) frame.replaceChildren(placeholder);
      else image.replaceWith(placeholder);
    };
    if (image.complete && image.naturalWidth) image.classList.add('loaded');
  });
}

async function openFilters() {
  if (!await commitCurrent()) return;
  const form = $('#filters');
  form.reset();
  Object.entries(state.filters).forEach(([key, value]) => { form.elements.namedItem(key).value = value; });
  $('#filter-dialog').showModal();
  form.elements.namedItem('rating').focus();
}

async function toggleUnrated() {
  if (!await commitCurrent()) return;
  state.filters.rating = state.filters.rating === 'unrated' ? '' : 'unrated';
  await loadQueue();
}

$('#runs').onclick = event => {
  const row = event.target.closest('[data-run]');
  if (row) selectRun(row.dataset.run);
};
$('#menu-button').onclick = () => setQueueOpen(!document.body.classList.contains('queue-open'));
$('#queue-backdrop').onclick = () => setQueueOpen(false);
$('#filter-button').onclick = openFilters;
$('#unrated-button').onclick = toggleUnrated;
$('#refresh').onclick = async () => { if (await commitCurrent()) loadQueue(); };
$('#help-button').onclick = () => $('#help-dialog').showModal();
document.querySelectorAll('[data-close]').forEach(button => { button.onclick = () => button.closest('dialog').close(); });
$('#reset-filters').onclick = () => $('#filters').reset();
$('#filters').onsubmit = async event => {
  event.preventDefault();
  state.filters = Object.fromEntries(new FormData(event.target));
  $('#filter-dialog').close();
  await loadQueue();
};

document.addEventListener('keydown', event => {
  if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.keyCode === 229) return;
  if (event.key === 'Escape' && document.body.classList.contains('queue-open')) {
    event.preventDefault();
    setQueueOpen(false);
    return;
  }
  if (document.querySelector('dialog[open]')) return;
  if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
  const key = event.key.toLowerCase();
  const actions = {
    j: () => navigate(1), arrowdown: () => navigate(1), k: () => navigate(-1), arrowup: () => navigate(-1),
    n: () => $('#notes')?.focus(), p: () => $('#swap-view')?.click(), c: () => $('#compare-view')?.click(),
    d: toggleDebug, g: togglePrompt,
    '/': openFilters, '?': () => $('#help-dialog').showModal(), u: toggleUnrated,
    r: async () => { if (await commitCurrent()) loadQueue(); },
  };
  // Keep Enter/Space native on focused buttons, including dialog and list controls.
  if (key === 'enter' && !event.target.closest('button, summary, a')) actions.enter = saveAndAdvance;
  if (/^[1-5]$/.test(key)) actions[key] = () => rate(Number(key));
  if (actions[key]) {
    event.preventDefault();
    if (!event.repeat) actions[key]();
  }
});

window.addEventListener('beforeunload', event => {
  syncNotes();
  if (Array.from(state.drafts.values()).some(draft => draft.pending || isDirty(draft))) {
    event.preventDefault();
    event.returnValue = '';
  }
});

loadQueue();

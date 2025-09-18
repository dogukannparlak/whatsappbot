(function () {
  const METRICS_URL = (window.WPBOT && window.WPBOT.METRICS_URL) || "/metrics";
  const REFRESH_MS = 3000;

  const liveEl = document.getElementById('live-metrics');

  function cssSafe(s) {
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  function statusBadge(ok) {
    const cls = ok ? 'ok' : 'warn';
    const icon = ok ? '✅' : '⏳';
    return `${icon} <span class="${cls}">${String(ok).toLowerCase()}</span>`;
  }

  /* ---------- Windows (nested) ---------- */
  function renderWindowsHTML(windows) {
    if (!windows || !windows.length) {
      return `<div class="hint">Bu profil için pencere/sekme verisi yok.</div>`;
    }
    let html = `<div class="accordion nested">`;
    windows.forEach((w, idx) => {
      const title = `
        <div class="acc-title">
          <span class="dot ${w.wa_ready ? 'ok' : 'warn'}"></span>
          <span>${w.title || ('Window ' + (idx+1))}</span>
        </div>`;
      const meta = w.url ? `<div class="acc-meta"><code>${w.url}</code></div>` : `<div class="acc-meta"></div>`;
      html += `
        <div class="acc-item">
          <div class="acc-header">${title}${meta}</div>
          <div class="acc-body">
            <div class="kv">
              <div>WA Ready</div><div>${statusBadge(!!w.wa_ready)}</div>
              <div>Title</div><div>${w.title || '-'}</div>
              <div>URL</div><div>${w.url || '-'}</div>
              <div>Last Seen</div><div>${w.last_seen || '-'}</div>
            </div>
          </div>
        </div>`;
    });
    html += `</div>`;
    return html;
  }

  function buildProfileBodyHTML(p) {
    return `
      <div class="kv">
        <div>Ready</div><div>${statusBadge(!!p.ready)}</div>
        <div>Path</div><div>${p.path || '-'}</div>
        <div>PID</div><div>${p.pid || '-'}</div>
        <div>Last Seen</div><div>${p.last_seen || '-'}</div>
      </div>
      <div style="margin-top:8px;font-weight:600;">Windows / Tabs</div>
      ${renderWindowsHTML(p.windows)}
    `;
  }

  function buildProfilesPanelHTML(data) {
    const profiles = data.profiles || [];
    if (!profiles.length) return `<div class="hint">Hiç profil yok.</div>`;
    let html = ``;
    profiles.forEach((p) => {
      html += `
        <div class="profile-item">
          <div class="profile-summary" data-key="${p.key}">
            <div class="profile-left">
              <span class="dot ${p.ready ? 'ok' : 'warn'}"></span>
              <span>${p.key}</span>
            </div>
            <div class="profile-right">${p.ready ? 'ready' : 'not ready'}</div>
          </div>
          <div class="details-body hidden" id="body-${cssSafe(p.key)}"></div>
        </div>
      `;
    });
    return html;
  }

  function attachProfileToggles(container, data) {
    container.querySelectorAll('.profile-summary').forEach((row) => {
      row.addEventListener('click', () => {
        const key = row.getAttribute('data-key');
        const body = container.querySelector(`#body-${cssSafe(key)}`);
        if (!body) return;
        const prof = (data.profiles || []).find(p => p.key === key);
        if (!prof) return;

        const needBuild = body.innerHTML.trim() === '';
        if (needBuild) {
          body.innerHTML = buildProfileBodyHTML(prof); // lazy build
        }
        body.classList.toggle('hidden');
      });
    });
  }

  /* ---------- Monospace özet kartı ---------- */
  function renderSummaryMonoBox(data) {
    const readyStr = `${data.prof_ready}/${data.prof_total} ready`;
    const kv = `
      <div class="mono-kv">
        <div>WhatsApp Ready</div><div>${data.wa_ready ? '✅ true' : '⏳ false'}</div>
        <div>Profiles</div><div>${readyStr}</div>
        <div>Queued Jobs</div><div>${data.queued_jobs}</div>
        <div>Running Jobs</div><div>${data.running_jobs}</div>
        <div>Pending Targets</div><div>${data.pending_targets}</div>
      </div>
    `;
    return `
      <div class="mono-card">
        <div class="mono-header">
          <div class="mono-title">Aktif WP / Toplam Profil</div>
          <div class="mono-right">${readyStr}</div>
        </div>
        <div class="mono-divider"></div>
        ${kv}
        <div class="mono-footer">
          <button id="profilesToggle" class="btn">Profilleri Göster ▾</button>
        </div>
        <div id="profiles-panel" class="profiles-panel hidden"></div>
      </div>
    `;
  }

  function renderAll(data) {
    const wrapper = document.createElement('div');
    wrapper.innerHTML = renderSummaryMonoBox(data);

    const panel = wrapper.querySelector('#profiles-panel');
    const btn   = wrapper.querySelector('#profilesToggle');

    // İlk açılışta lazy build
    let panelBuilt = false;
    btn.addEventListener('click', () => {
      const hidden = panel.classList.contains('hidden');
      if (hidden && !panelBuilt) {
        panel.innerHTML = buildProfilesPanelHTML(data);
        attachProfileToggles(panel, data);
        panelBuilt = true;
      }
      panel.classList.toggle('hidden');
      btn.textContent = panel.classList.contains('hidden') ? 'Profilleri Göster ▾' : 'Profilleri Gizle ▴';
    });

    liveEl.innerHTML = '';
    liveEl.appendChild(wrapper);
  }

  async function fetchAndRender() {
    try {
      const res = await fetch(METRICS_URL, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderAll(data);
    } catch (e) {
      liveEl.innerHTML = `<div class="hint">Metrikler alınamadı: ${String(e.message || e)}</div>`;
    }
  }

  fetchAndRender();
  setInterval(fetchAndRender, REFRESH_MS);
})();

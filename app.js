'use strict';

const CATEGORY_LABELS = {
  championship: 'Championnat',
  endurance: 'Endurance',
  sprint: 'Sprint',
  daily: 'Daily',
  special: 'Spécial'
};

const MONTH_FR = ['JAN','FEV','MAR','AVR','MAI','JUIN','JUIL','AOU','SEP','OCT','NOV','DEC'];

const state = {
  events: [],
  filter: 'all',
  search: '',
  showPast: false,
  lastUpdated: null
};

// ---------- Boot ----------
async function boot() {
  startClock();
  try {
    const res = await fetch('events.json', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    state.events = (data.events || []).map(parseEvent).sort((a,b) => a.start - b.start);
    state.lastUpdated = data.lastUpdated || null;
  } catch (err) {
    console.error('Erreur de chargement du calendrier', err);
    document.getElementById('eventsList').innerHTML =
      '<p class="loading">Impossible de charger le calendrier.</p>';
    return;
  }

  bindControls();
  render();
  setInterval(render, 1000); // refresh countdowns + statuts live
}

function parseEvent(e) {
  return {
    ...e,
    start: new Date(e.start),
    end: new Date(e.end)
  };
}

// ---------- Clock ----------
function startClock() {
  const timeEl = document.getElementById('clockTime');
  const tzEl = document.getElementById('clockTz');
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  tzEl.textContent = tz;
  const tick = () => {
    const now = new Date();
    timeEl.textContent = now.toLocaleTimeString('fr-FR', { hour12: false });
  };
  tick();
  setInterval(tick, 1000);
}

// ---------- Controls ----------
function bindControls() {
  document.querySelectorAll('.filter').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.filter = btn.dataset.filter;
      render();
    });
  });

  document.getElementById('searchInput').addEventListener('input', (e) => {
    state.search = e.target.value.trim().toLowerCase();
    render();
  });

  const toggle = document.getElementById('toggleView');
  toggle.addEventListener('click', () => {
    state.showPast = !state.showPast;
    toggle.setAttribute('aria-pressed', String(state.showPast));
    toggle.querySelector('.view-on').textContent =
      state.showPast ? 'Masquer passées' : 'Afficher passées';
    render();
  });

  if (state.lastUpdated) {
    document.getElementById('lastUpdated').textContent =
      new Date(state.lastUpdated).toLocaleDateString('fr-FR', {
        day: '2-digit', month: 'long', year: 'numeric'
      });
  }
}

// ---------- Render ----------
function render() {
  const now = new Date();
  const filtered = state.events.filter(matchesFilters);
  const live = filtered.filter(e => isLive(e, now));
  const upcoming = filtered.filter(e => e.start > now);
  const past = filtered.filter(e => e.end < now);

  renderLiveBanner(live[0]);
  renderFeatured(live[0] || upcoming[0]);
  renderList(filtered, now);
}

function matchesFilters(e) {
  if (state.filter !== 'all' && e.category !== state.filter) return false;
  if (state.search) {
    const haystack = [e.name, e.series, e.track, e.country].join(' ').toLowerCase();
    if (!haystack.includes(state.search)) return false;
  }
  return true;
}

function isLive(e, now) {
  return e.start <= now && now <= e.end;
}

function renderLiveBanner(liveEvent) {
  const banner = document.getElementById('liveBanner');
  if (!liveEvent) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  document.getElementById('liveTitle').textContent =
    `${liveEvent.name} — ${liveEvent.track}`;
  const link = document.getElementById('liveLink');
  link.href = liveEvent.url || '#';
  link.target = liveEvent.url ? '_blank' : '_self';
  link.rel = 'noopener noreferrer';
}

function renderFeatured(event) {
  const root = document.getElementById('nextUp');
  if (!event) { root.innerHTML = ''; return; }

  const now = new Date();
  const live = isLive(event, now);
  const label = live ? 'EN COURS' : 'PROCHAIN ÉVÉNEMENT';
  const target = live ? event.end : event.start;
  const cd = computeCountdown(target - now);

  root.innerHTML = `
    <div class="featured-card">
      <div>
        <span class="featured-label">${label}</span>
        <h2 class="featured-title">${escapeHtml(event.name)}</h2>
        <div class="featured-meta">
          <span>${escapeHtml(event.series)}</span>
          <span>${escapeHtml(event.track)} · ${escapeHtml(event.country)}</span>
          <span>${formatDateTime(event.start)}</span>
          <span>Durée : ${escapeHtml(event.duration)}</span>
        </div>
      </div>
      <div class="countdown" aria-label="${live ? 'Temps restant' : 'Compte à rebours'}">
        ${countdownUnit(cd.days, 'JOURS')}
        ${countdownUnit(cd.hours, 'HEURES')}
        ${countdownUnit(cd.minutes, 'MIN')}
        ${countdownUnit(cd.seconds, 'SEC')}
      </div>
    </div>
  `;
}

function countdownUnit(value, label) {
  return `
    <div class="countdown-unit">
      <span class="countdown-value">${String(value).padStart(2,'0')}</span>
      <span class="countdown-label">${label}</span>
    </div>
  `;
}

function computeCountdown(ms) {
  if (ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  return {
    days:    Math.floor(s / 86400),
    hours:   Math.floor((s % 86400) / 3600),
    minutes: Math.floor((s % 3600) / 60),
    seconds: s % 60
  };
}

function renderList(events, now) {
  const list = document.getElementById('eventsList');
  const visible = events.filter(e => state.showPast || e.end >= now);

  if (!visible.length) {
    list.innerHTML = '<p class="loading">Aucun événement ne correspond à votre recherche.</p>';
    return;
  }

  list.innerHTML = visible.map(e => eventCard(e, now)).join('');
}

function eventCard(e, now) {
  const live = isLive(e, now);
  const past = e.end < now;
  const klass = live ? 'live' : past ? 'past' : '';
  const status = live
    ? `EN DIRECT — fin dans ${formatRelative(e.end - now)}`
    : past
      ? `Terminé il y a ${formatRelative(now - e.end)}`
      : `Dans ${formatRelative(e.start - now)}`;

  return `
    <article class="event ${klass}">
      <div class="event-date">
        <span class="day">${String(e.start.getDate()).padStart(2,'0')}</span>
        <span class="month">${MONTH_FR[e.start.getMonth()]}</span>
        <span class="year">${e.start.getFullYear()}</span>
      </div>
      <div class="event-body">
        <h3 class="event-name">${escapeHtml(e.name)}</h3>
        <div class="event-meta">
          <span><strong>${escapeHtml(e.series)}</strong></span>
          <span>${escapeHtml(e.track)} · ${escapeHtml(e.country)}</span>
          <span>Durée : ${escapeHtml(e.duration)}</span>
        </div>
      </div>
      <div class="event-aside">
        <span class="badge ${e.category}">${CATEGORY_LABELS[e.category] || e.category}</span>
        <span class="event-time">${formatTimeRange(e.start, e.end)}</span>
        <span class="event-status">${status}</span>
      </div>
    </article>
  `;
}

// ---------- Formatting ----------
function formatDateTime(d) {
  return d.toLocaleString('fr-FR', {
    weekday: 'long',
    day: '2-digit',
    month: 'long',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  });
}

function formatTimeRange(start, end) {
  const sameDay = start.toDateString() === end.toDateString();
  const t = (d) => d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  return sameDay ? `${t(start)} → ${t(end)}` : `${t(start)} → ${end.toLocaleDateString('fr-FR', { day:'2-digit', month:'short' })} ${t(end)}`;
}

function formatRelative(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}min`;
  const d = Math.floor(h / 24);
  return `${d} jours`;
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

boot();

/*
 * Bridge between the Perseus Night Watch bundle and the backend point scoring model.
 *
 * points-data.js sets window.PNW_DATA (built by tools/build_perseus.py from
 * backend/tidal/points/night_watch.py) with one result set per technology:
 * Fan_Turbine and Seabed_Buoy_Turbine. The patched app.js calls:
 *
 *   window.__pnwPin(pin, utilityTable)  - when a pin is dropped. A pin within SNAP map
 *       units of a NOAA current station snaps to it and shows the selected technology's
 *       score, status and reasons. Other water pins keep the app's schematic estimate,
 *       labeled SIMULATED.
 *   window.__pnwDots(theme, zoomed)     - extra map dots: every station, colored by score.
 *
 * The bridge also adds a Fan / Buoy toggle. With no window.PNW_DATA every hook is a
 * no-op and the app behaves as delivered. A missing model value is never shown as 0:
 * Excluded points have no score.
 */
(function () {
  'use strict';

  // 12 map units ≈ 2.2 km (app.js: 0.185 km per unit).
  var SNAP = 12;
  var CLASSES = [
    [80, '#FDE725'],
    [60, '#7AD151'],
    [40, '#1F9E89'],
    [20, '#3B528B'],
    [0, '#440154'],
  ];
  var TECH_INFO = {
    Fan_Turbine: { short: 'Fan', label: 'Fan turbine' },
    Seabed_Buoy_Turbine: { short: 'Buoy', label: 'Seabed buoy turbine' },
  };

  function store() {
    var d = window.PNW_DATA;
    return d && d.technologies && typeof d.technologies === 'object' ? d : null;
  }

  function techIds() {
    var d = store();
    return d ? Object.keys(d.technologies) : [];
  }

  function current() {
    var d = store();
    if (!d) return null;
    var tech = window.__pnwTech;
    if (!tech || !d.technologies[tech]) tech = d.technologies[d.default] ? d.default : techIds()[0];
    window.__pnwTech = tech;
    var t = d.technologies[tech];
    return t && Array.isArray(t.points) ? t : null;
  }

  function nearest(points, x, y) {
    var best = null;
    var bestDist = Infinity;
    for (var i = 0; i < points.length; i++) {
      var dist = Math.hypot(points[i].x - x, points[i].y - y);
      if (dist < bestDist) {
        bestDist = dist;
        best = points[i];
      }
    }
    return bestDist <= SNAP ? best : null;
  }

  function scoreColor(score) {
    for (var i = 0; i < CLASSES.length; i++) if (score >= CLASSES[i][0]) return CLASSES[i][1];
    return CLASSES[CLASSES.length - 1][1];
  }

  function titleCase(s) {
    if (!s) return 'Unknown';
    return String(s)
      .toLowerCase()
      .replace(/\b([a-z])/g, function (c) {
        return c.toUpperCase();
      });
  }

  function label(tech) {
    return (TECH_INFO[tech] && TECH_INFO[tech].label) || tech;
  }

  var SOURCE_NOTES = [
    'Peak current is a predicted current (harmonic): NOAA CO-OPS 2025 predictions at this station.',
    'Simulated: the tide clock, live output and yield use the app\u2019s M2 tide timing scaled by this station\u2019s peak current.',
    'Regulatory screening and jurisdiction are proxies. Not legal advice.',
  ];

  window.__pnwPin = function (s, utilityTable) {
    var t = current();
    if (!s || !t) return s;
    var p = nearest(t.points, s.x, s.y);
    if (!p) {
      if (!s.water) return s;
      return Object.assign({}, s, {
        badge: 'DROPPED PIN \u00b7 SIMULATED',
        notes: [
          'Simulated: no NOAA current station within about 2 km, so this score is the app\u2019s schematic estimate, not the ' +
            label(t.technology).toLowerCase() +
            ' model. Click a station dot for model scores.',
        ].concat(s.notes || []),
      });
    }
    var zoneNotes = s.water
      ? (s.notes || []).map(function (n) {
          return 'Approximate zone: ' + n;
        })
      : [];
    var util = (utilityTable && utilityTable[p.utilityIndex]) || {};
    return Object.assign({}, s, util, {
      water: true,
      name: p.name,
      badge: 'NOAA STATION ' + p.id + ' \u00b7 ' + label(t.technology).toUpperCase(),
      x: p.x,
      y: p.y,
      lx: p.lx,
      ly: p.ly,
      grp: p.grp,
      kn: p.kn,
      score: p.score,
      sk: p.sk,
      juris: p.juris + ' (proxy)',
      notes: p.notes.concat(zoneNotes, SOURCE_NOTES),
      landing: titleCase(p.landing),
      cable: p.cable,
      method: p.method,
      pd: p.pd,
      mwh: p.mwh,
      rev: p.rev,
      uKey: p.uKey,
    });
  };

  window.__pnwDots = function (theme, zoomed) {
    var t = current();
    if (!t) return [];
    var excluded = [];
    var scored = [];
    for (var i = 0; i < t.points.length; i++) {
      var p = t.points[i];
      if (p.status === 'Scored' && p.score != null) {
        scored.push({
          left: p.left,
          top: p.top,
          col: scoreColor(p.score),
          size: '11px',
          op: 1,
          lab: !!zoomed,
          kn: p.score.toFixed(0),
          ink: theme.text,
        });
      } else {
        excluded.push({
          left: p.left,
          top: p.top,
          col: theme.pro,
          size: '6px',
          op: 0.55,
          lab: false,
          kn: '',
          ink: theme.text,
        });
      }
    }
    scored.sort(function (a, b) {
      return Number(a.kn) - Number(b.kn);
    });
    return excluded.concat(scored);
  };

  /** Switch technology and redraw. Returns false for an unknown technology. */
  window.__pnwSetTech = function (tech) {
    var d = store();
    if (!d || !d.technologies[tech]) return false;
    window.__pnwTech = tech;
    writeUrlTech(tech);
    if (window.__pnwApp && typeof window.__pnwApp.forceUpdate === 'function') window.__pnwApp.forceUpdate();
    syncToggle();
    return true;
  };

  // ------------------------------------------------------------------ URL (?tech=fan|buoy)

  function techFromShort(value) {
    var v = String(value || '').toLowerCase();
    var ids = techIds();
    for (var i = 0; i < ids.length; i++) {
      var info = TECH_INFO[ids[i]];
      if (ids[i].toLowerCase() === v || (info && info.short.toLowerCase() === v)) return ids[i];
    }
    return null;
  }

  function readUrlTech() {
    if (typeof location === 'undefined' || typeof URLSearchParams === 'undefined') return;
    var tech = techFromShort(new URLSearchParams(location.search).get('tech'));
    if (tech) window.__pnwTech = tech;
  }

  function writeUrlTech(tech) {
    if (typeof history === 'undefined' || typeof location === 'undefined' || !history.replaceState) return;
    try {
      var url = new URL(location.href);
      var info = TECH_INFO[tech];
      url.searchParams.set('tech', info ? info.short.toLowerCase() : tech);
      history.replaceState(null, '', url);
    } catch (e) {
      /* file:// in some browsers: keep the selection, skip the URL */
    }
  }

  readUrlTech();

  // ------------------------------------------------------------------ toggle UI

  var PALETTE = {
    dark: { panel: '#111A24', border: '#2F4054', text: '#E6EDF3', muted: '#A5B3C1', on: '#1E2A38' },
    light: { panel: '#FFFFFF', border: '#9FB3C6', text: '#0F1B26', muted: '#4A5867', on: '#E1E8EF' },
  };
  var toggleEl = null;

  function theme() {
    var app = window.__pnwApp;
    return app && app.state && app.state.theme === 'light' ? 'light' : 'dark';
  }

  function summaryText(tech) {
    var t = store().technologies[tech];
    var s = (t && t.summary && t.summary.status) || {};
    return (
      label(tech) + ': ' + (s.Scored || 0) + ' of ' + (t ? t.points.length : 0) + ' NOAA stations scored'
    );
  }

  function syncToggle() {
    if (!toggleEl) return;
    var pal = PALETTE[theme()];
    var tech = window.__pnwTech;
    toggleEl.style.background = pal.panel;
    toggleEl.style.borderColor = pal.border;
    toggleEl.style.color = pal.text;
    var buttons = toggleEl.querySelectorAll('button');
    for (var i = 0; i < buttons.length; i++) {
      var on = buttons[i].getAttribute('data-tech') === tech;
      buttons[i].setAttribute('aria-pressed', String(on));
      buttons[i].style.background = on ? pal.on : 'transparent';
      buttons[i].style.color = on ? pal.text : pal.muted;
      buttons[i].style.fontWeight = on ? '600' : '500';
    }
    var caption = toggleEl.querySelector('[data-role="caption"]');
    if (caption) {
      caption.textContent = summaryText(tech);
      caption.style.color = pal.muted;
    }
  }

  function mountToggle() {
    if (!store() || toggleEl || !document.body) return;
    current();
    var group = document.createElement('div');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'Turbine technology');
    group.style.cssText =
      'position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:20;display:flex;' +
      'flex-direction:column;align-items:center;gap:4px;padding:6px 8px;border:1px solid;' +
      'border-radius:12px;font-family:"Space Grotesk","Segoe UI",system-ui,sans-serif;font-size:13px;' +
      'box-shadow:0 8px 24px rgba(0,0,0,0.25)';
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:4px';
    techIds().forEach(function (tech) {
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('data-tech', tech);
      b.textContent = (TECH_INFO[tech] && TECH_INFO[tech].short) || tech;
      b.title = label(tech);
      b.style.cssText =
        'min-height:36px;min-width:72px;padding:6px 14px;border:0;border-radius:8px;font:inherit;cursor:pointer';
      b.addEventListener('click', function () {
        window.__pnwSetTech(tech);
      });
      row.appendChild(b);
    });
    var caption = document.createElement('div');
    caption.setAttribute('data-role', 'caption');
    caption.setAttribute('aria-live', 'polite');
    caption.style.cssText = 'font-size:11px';
    group.appendChild(row);
    group.appendChild(caption);
    document.body.appendChild(group);
    toggleEl = group;
    syncToggle();
    // Follow the app's Light/Dark switch.
    setInterval(syncToggle, 500);
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountToggle);
    else mountToggle();
  }
})();

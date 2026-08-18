/* ============================================================
   site.js — behaviour for the public pages.

   The motion kernel is the dashboard's, lifted verbatim rather than
   written twice: one spring implementation, one rAF loop, the same
   projection and rubber-band constants. If the feel of a sheet in the
   app changes, the feel of the deck out here changes with it.
   ============================================================ */

/* ============================================================
   MOTION — one rAF loop. It starts on demand and STOPS when the
   last spring settles, so nothing ticks behind an idle page (fm-8).
   ============================================================ */
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');
const springs = new Set();
let rafId = 0, lastT = 0, looping = false;

function pump(now) {
  looping = false; rafId = 0;
  const dt = Math.min((now - lastT) / 1000, 1 / 20);
  lastT = now;
  for (const s of springs) if (!s.step(dt)) springs.delete(s);
  if (springs.size) { looping = true; rafId = requestAnimationFrame(pump); }
}
function wake() {
  // Don't trust `looping` alone. It is only ever cleared by pump(), so a frame
  // that was scheduled but never delivered would pin the flag on and every live
  // value would stop updating for the rest of the session.
  if (looping && performance.now() - lastT < 1000) return;
  if (rafId) cancelAnimationFrame(rafId);
  looping = true;
  lastT = performance.now();
  rafId = requestAnimationFrame(pump);
}
// A backgrounded tab pauses rAF, so the scheduled callback may never arrive.
// Re-arm explicitly on return instead of trusting a callback that never came.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  if (rafId) cancelAnimationFrame(rafId);
  looping = false; rafId = 0;
  if (springs.size) wake();
});

class Spring {
  // response = seconds to reach target (NOT a duration); damping 1 = no overshoot.
  constructor(value, onChange, opts = {}) {
    this.value = value; this.target = value; this.velocity = 0;
    this.damping = opts.damping ?? 1;
    this.response = opts.response ?? 0.35;
    this.epsilon = opts.epsilon ?? 0.01;
    this.onChange = onChange;
  }
  // Retarget without touching velocity — motion stays continuous mid-flight.
  to(target, velocity) {
    this.target = target;
    if (velocity !== undefined) this.velocity = velocity;
    // Reduced motion still COMPLETES the transition; it just skips the travel.
    if (REDUCED.matches) return this.set(target);
    springs.add(this); wake();
    return this;
  }
  // Jump with no animation. This is what a drag calls on every pointermove —
  // it is not a settle, so nothing downstream should treat it as one.
  set(value) {
    this.value = this.target = value; this.velocity = 0;
    springs.delete(this); this.onChange(this.value);
    return this;
  }
  // Seize the PRESENTATION value — this is what makes an interrupt seamless.
  seize() { springs.delete(this); this.velocity = 0; return this.value; }
  step(dt) {
    const w = (2 * Math.PI) / this.response;
    const n = Math.max(1, Math.ceil(dt * 240));   // fixed sub-steps: stable at any frame rate
    const h = dt / n;
    for (let i = 0; i < n; i++) {
      const a = -w * w * (this.value - this.target) - 2 * this.damping * w * this.velocity;
      this.velocity += a * h;
      this.value += this.velocity * h;
    }
    const settled = Math.abs(this.value - this.target) < this.epsilon && Math.abs(this.velocity) < this.epsilon * 12;
    if (settled) { this.value = this.target; this.velocity = 0; }
    this.onChange(this.value);
    return !settled;
  }
}

// Where a flick comes to rest — exponential decay, the form real scroll views
// use. The textbook v²/2a is NOT the shipped behaviour.
const project = (v, d = 0.998) => (v / 1000) * d / (1 - d);
// Progressive resistance past a boundary; never a hard stop.
const rubberband = (over, dim, c = 0.55) => (over * dim * c) / (dim + c * Math.abs(over));

// The last few pointer samples, so release velocity is measured, not guessed.
class Tracker {
  constructor() { this.samples = []; }
  push(v) { this.samples.push([performance.now(), v]); if (this.samples.length > 6) this.samples.shift(); }
  velocity() {
    const s = this.samples;
    if (s.length < 2) return 0;
    const [t0, v0] = s[0], [t1, v1] = s[s.length - 1];
    const dt = (t1 - t0) / 1000;
    return dt > 0.001 ? (v1 - v0) / dt : 0;
  }
  reset() { this.samples.length = 0; }
}

/* ============================================================
   LAW 1 — the press response fires on POINTERDOWN, not on click.
   Dragging ~12px away cancels it, exactly like a native tap.
   Listeners are torn down every time; nothing accumulates.
   ============================================================ */
document.addEventListener('pointerdown', (e) => {
  const t = e.target.closest('button, a[href]');
  if (!t || t.disabled || t.closest('.deck')) return;   // the deck has its own gesture
  t.classList.add('is-pressed');
  const x0 = e.clientX, y0 = e.clientY;
  const off = () => {
    t.classList.remove('is-pressed');
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', off);
    document.removeEventListener('pointercancel', off);
  };
  const move = (ev) => { if (Math.hypot(ev.clientX - x0, ev.clientY - y0) > 12) off(); };
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', off);
  document.addEventListener('pointercancel', off);
}, { passive: true });

/* ============================================================
   LAW 7 — an explicit theme choice outranks the media query, in
   BOTH directions, under the same `ar_theme` key the dashboard
   reads, so the choice survives sign-in. No key = follow the system.
   ============================================================ */
const SUN = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
const MOON = '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>';

function resolvedTheme() {
  const t = document.documentElement.dataset.theme;
  return t === 'light' || t === 'dark' ? t : (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
}
function paintToggle() {
  const dark = resolvedTheme() === 'dark';
  document.querySelectorAll('[data-theme-toggle]').forEach((b) => {
    b.querySelector('svg').innerHTML = dark ? SUN : MOON;
    b.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
  });
}
document.addEventListener('click', (e) => {
  if (!e.target.closest('[data-theme-toggle]')) return;
  const next = resolvedTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('ar_theme', next); } catch (err) {}
  paintToggle();
});
// The system can change under us; repaint only when no explicit choice is stored.
matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
  if (!document.documentElement.dataset.theme) paintToggle();
});
paintToggle();

/* ============================================================
   SCROLL EDGE EFFECT — the bar's hairline and its soft gradient
   appear only once content is genuinely underneath. Derived from
   the real scroll position every frame, never from a class flag,
   so it cannot get stuck lit.
   ============================================================ */
const bar = document.querySelector('.bar');
if (bar) {
  const syncEdge = () => bar.style.setProperty('--scrolled', String(Math.min(scrollY / 24, 1)));
  addEventListener('scroll', syncEdge, { passive: true });
  syncEdge();
}

/* ============================================================
   MATERIALIZE — blur, scale and opacity arrive together, so the
   surface reads as material rather than a picture fading in.
   Each element is unobserved the moment it lands: the observer
   set only ever shrinks (fm-8).
   ============================================================ */
const risers = document.querySelectorAll('.rise');
// The observer's job is the ANIMATION, never the visibility. It computes nothing
// while the tab is in the background, so a page opened in a new tab would sit
// there fully rendered and completely blank until it happened to be scrolled.
// No observer available, or nobody watching: show the page, skip the entrance.
if (risers.length && 'IntersectionObserver' in window && document.visibilityState === 'visible') {
  const io = new IntersectionObserver((entries, obs) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const el = entry.target;
      obs.unobserve(el);
      el.classList.add('in');
      // Under reduced motion Spring.to() jumps, leaving only the opacity
      // cross-fade — a non-vestibular equivalent, not the loss of feedback.
      // A surface that owns the screen arrives as material: it scales up as the
      // blur clears. A paragraph does not — it only rises. Same spring, one value.
      const pop = el.classList.contains('rise-pop');
      const s = new Spring(14, (v) => {
        el.style.transform = pop
          ? `translate3d(0,${v.toFixed(2)}px,0) scale(${(1 - v / 260).toFixed(4)})`
          : `translate3d(0,${v.toFixed(2)}px,0)`;
      }, { damping: 1, response: pop ? 0.5 : 0.45 });
      s.to(0);
    }
  }, { rootMargin: '0px 0px -12% 0px' });
  risers.forEach((el) => io.observe(el));
} else {
  risers.forEach((el) => el.classList.add('in'));
}

/* ============================================================
   THE DECK — laws 2 through 6, in one component.

   Grab it anywhere and it tracks 1:1 from the point you grabbed.
   Let go and it leaves at the velocity your finger had, landing
   where the momentum was actually going rather than at whatever
   card happened to be nearest when you released. Grab it again
   mid-flight and it continues from the pixel it is on, because the
   position is a spring and the drag seizes its presentation value.
   Push past either end and the resistance builds instead of the
   strip hitting a wall.
   ============================================================ */
function initDeck(root) {
  const track = root.querySelector('.deck-track');
  const cards = [...track.children];
  const dots = [...root.parentElement.querySelectorAll('.deck-dot')];
  const last = cards.length - 1;
  let step = 1, index = 0;

  const paint = (x) => {
    track.style.transform = `translate3d(${x.toFixed(2)}px,0,0)`;
    // Depth is continuous DURING the gesture, not applied once it ends.
    const focus = -x / step;
    cards.forEach((c, i) => {
      const d = Math.min(Math.abs(i - focus), 1.4);
      c.style.transform = `scale(${(1 - d * 0.055).toFixed(4)})`;
      c.style.opacity = (1 - d * 0.42).toFixed(3);
    });
  };
  const pos = new Spring(0, paint, { damping: 1, response: 0.4 });

  const syncDots = () => dots.forEach((d, i) => d.setAttribute('aria-current', String(i === index)));

  // Measure what the browser actually laid out — never assume the CSS width
  // resolved to the number we wrote, because a user text-size bump moves all of it.
  //
  // offsetLeft/offsetWidth, NOT getBoundingClientRect: the rect is the box AFTER
  // paint() has scaled it, and the scale depends on which card is focused. Feeding
  // that back in makes the step drift every time the deck moves, so the snap points
  // slowly stop lining up with the cards. Layout geometry has no such feedback loop.
  function measure() {
    const w = cards[0].offsetWidth;
    step = cards[1] ? cards[1].offsetLeft - cards[0].offsetLeft : w;
    if (!(step > 0)) step = 1;
    track.style.paddingLeft = track.style.paddingRight =
      Math.max(0, (root.clientWidth - w) / 2) + 'px';
    pos.set(-index * step);   // a resize is not an interaction: jump, don't animate
  }

  function goTo(i, velocity) {
    index = Math.max(0, Math.min(last, i));
    // Bounce belongs to a gesture that carried momentum — never to a tap.
    const flick = velocity !== undefined && Math.abs(velocity) > 40;
    pos.damping = flick ? 0.8 : 1;
    pos.response = flick ? 0.35 : 0.4;
    pos.to(-index * step, flick ? velocity : undefined);
    syncDots();
  }

  const track_ = new Tracker();
  let pointer = null, startX = 0, startPos = 0, committed = false;

  root.addEventListener('pointerdown', (e) => {
    if (e.button) return;
    // A new press always takes over. Refusing one while `pointer` is still set
    // would wedge the deck for the rest of the page's life the one time an up
    // event never arrives — and it looks alive the whole time it is dead.
    pointer = e.pointerId;
    // Record the grab BEFORE asking for capture. setPointerCapture throws if the
    // pointer is already gone, and a throw between these two lines would leave the
    // origin at its last value — every later move would then be measured from the
    // wrong place and the strip would jump on the first pixel of the drag.
    startX = e.clientX;
    startPos = pos.seize();       // the PRESENTATION value: no jump on interrupt
    committed = false;
    track_.reset(); track_.push(e.clientX);
    // Capture keeps tracking alive once the finger leaves the strip.
    try { root.setPointerCapture(e.pointerId); } catch (err) {}
  });

  root.addEventListener('pointermove', (e) => {
    if (e.pointerId !== pointer) return;
    const dx = e.clientX - startX;
    // ~12px before we commit to the horizontal axis, exactly like a native swipe.
    if (!committed) {
      if (Math.abs(dx) < 12) return;
      committed = true;
      root.classList.add('grabbing');
    }
    let x = startPos + dx;                       // 1:1, from where it was grabbed
    const min = -last * step;
    if (x > 0) x = rubberband(x, root.clientWidth);
    else if (x < min) x = min - rubberband(min - x, root.clientWidth);
    pos.set(x);
    track_.push(e.clientX);
  });

  const release = (e) => {
    if (e.pointerId !== pointer) return;
    pointer = null;
    root.classList.remove('grabbing');
    if (!committed) return;                      // a tap, not a throw
    const v = track_.velocity();
    // Land where the throw was GOING, then snap to the card nearest that point.
    const projected = pos.value + project(v);
    goTo(Math.round(-projected / step), v);
  };
  root.addEventListener('pointerup', release);
  root.addEventListener('pointercancel', release);
  // Capture can be taken away without an up or a cancel ever arriving.
  root.addEventListener('lostpointercapture', release);

  root.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    goTo(index + (e.key === 'ArrowRight' ? 1 : -1));
  });
  dots.forEach((d, i) => d.addEventListener('click', () => goTo(i)));

  measure();
  syncDots();
  // One observer for the page's life, watching the thing whose size actually
  // matters. A font-size change moves the step without firing `resize`.
  new ResizeObserver(measure).observe(root);
}
document.querySelectorAll('.deck').forEach(initDeck);

/* ============================================================
   SEGMENTED CONTROL — the pill's position and width are separate
   springs, because one spring across two axes desyncs the moment
   the distances differ. Press a third option while the pill is
   still travelling and it retargets from where it actually is.
   ============================================================ */
function initSeg(seg) {
  const pill = seg.querySelector('.seg-pill');
  const opts = [...seg.querySelectorAll('button')];
  const out = document.getElementById(seg.dataset.output || '');
  // The figure animates every frame; a live region cannot. The spoken update is
  // a separate, static announcement fired once per choice.
  const live = document.getElementById(seg.dataset.live || '');
  let index = Math.max(0, opts.findIndex((b) => b.getAttribute('aria-pressed') === 'true'));

  const x = new Spring(0, (v) => { pill.style.transform = `translate3d(${v.toFixed(2)}px,0,0)`; }, { damping: 1, response: 0.35 });
  const w = new Spring(0, (v) => { pill.style.width = `${Math.max(0, v).toFixed(2)}px`; }, { damping: 1, response: 0.35 });
  // The lag figure is half the timeframe: the average wait for the bar boundary.
  const secs = new Spring(0, (v) => {
    if (!out) return;
    out.firstElementChild.textContent = v < 60 ? String(Math.round(v)) : (v / 60).toFixed(1);
    out.lastElementChild.textContent = v < 60 ? 'sec' : 'min';
  });

  const place = (animate) => {
    const b = opts[index].getBoundingClientRect(), s = seg.getBoundingClientRect();
    const left = b.left - s.left, width = b.width;
    const target = Number(opts[index].dataset.seconds || 0);
    if (animate) { x.to(left); w.to(width); secs.to(target); }
    else { x.set(left); w.set(width); secs.set(target); }
  };

  seg.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    index = opts.indexOf(b);
    opts.forEach((o, i) => o.setAttribute('aria-pressed', String(i === index)));
    place(true);
    if (live) live.textContent = `${b.textContent.trim()} rule: ${b.dataset.text || ''}`;
  });

  place(false);
  new ResizeObserver(() => place(false)).observe(seg);
}
document.querySelectorAll('.seg').forEach(initSeg);

document.querySelectorAll('[data-year]').forEach((n) => { n.textContent = new Date().getFullYear(); });

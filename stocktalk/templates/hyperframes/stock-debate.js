(() => {
  const root = document.querySelector('#root');
  if (!root || typeof gsap === 'undefined') return;

  const tl = gsap.timeline({ paused: true, defaults: { ease: 'power2.out' } });

  const fullDuration = Number(root.dataset.duration || 1);
  // The permanent stage fades in this early because the opening title card only
  // covers the panel area: the headline and captions are live from word one.
  const stageIn = Number(root.dataset.stageIn || 0);
  // How long an outgoing layer takes to dissolve away from above the next one.
  const DISSOLVE = .34;
  // z-index bands.  Each slot keeps its own band so a slot's layers can never
  // land on top of another slot's.
  const SLOT_Z = { tint: 2000, kicker: 3000, keywords: 4000, visual: 5000, caption: 6000 };

  // Preview-only aid: `#guides` in the URL reveals the bands each platform's
  // player UI will sit on, so the layout can be checked before publishing.
  // Nothing else reads the attribute, so a render never shows them.
  if (location.hash.includes('guides')) {
    root.dataset.guides = '1';
  }

  const intro = document.querySelector('#intro-card');
  const outro = document.querySelector('#outro-card');

  // ---- Permanent stage: complete from frame zero ----------------------------
  // 视频号等平台的聊天分享卡直接取视频首帧（2026-09-20 实测：分享卡=半空首帧），
  // 所以开场不做淡入——第 0 帧就是完整排版，入场感交给开场卡的溶解。
  tl.set('#headline', { opacity: 1, y: 0 }, 0)
    .set('#visual-frame', { opacity: 1, y: 0 }, 0)
    .set('#caption-zone', { opacity: 1, y: 0 }, 0);

  // ---- Content slots: updates happen in place, never as a page turn --------
  // Every slot stacks its versions with a DESCENDING z-index, so version N+1
  // sits underneath version N.  A change therefore parks the new content fully
  // opaque (unseen, below) and dissolves the old layer away above it: no
  // translation, no scaling, and never two half-transparent layers printed on
  // top of each other.  Neighbouring turns with identical content were already
  // merged into one long window by the builder, so they get no tween at all —
  // an unchanged panel simply does not move.
  const wireSlot = (slot) => {
    const items = Array.from(document.querySelectorAll(`[data-slot="${slot}"]`));
    items.forEach((el, i) => {
      el.style.zIndex = String((SLOT_Z[slot] || 1000) - i);
      const start = Math.max(0, Number(el.dataset.start || 0));
      if (i === 0) {
        if (start === 0) {
          // 首个版本从第 0 帧起就是完成态（海报帧），不做淡入。
          tl.set(el, { visibility: 'visible', opacity: 1 }, 0);
        } else {
          tl.set(el, { visibility: 'visible' }, start);
          tl.fromTo(el, { opacity: 0 }, { opacity: 1, duration: .5 }, start);
        }
        return;
      }
      const previous = items[i - 1];
      tl.set(el, { visibility: 'visible', opacity: 1 }, start);
      tl.to(previous, { opacity: 0, duration: DISSOLVE, ease: 'power1.inOut' }, start);
      tl.set(previous, { visibility: 'hidden' }, start + DISSOLVE + .02);
    });
  };
  ['tint', 'kicker', 'keywords', 'visual'].forEach(wireSlot);

  // ---- Captions: one row at a time, swapped on the spoken word -------------
  document.querySelectorAll('.caption-line').forEach(el => {
    const at = Math.max(0, Number(el.dataset.start || 0));
    const duration = Math.max(.3, Number(el.dataset.duration || .8));
    if (at === 0) {
      tl.set(el, { visibility: 'visible', opacity: 1, y: 0 }, 0);
    } else {
      tl.set(el, { visibility: 'visible' }, at);
      tl.fromTo(el, { opacity: 0, y: 10 }, { opacity: 1, y: 0, duration: .13, ease: 'power2.out' }, at);
    }
    tl.set(el, { visibility: 'hidden' }, at + duration);
  });
  document.querySelectorAll('.progress-fill').forEach(el => {
    const start = Math.max(0, Number(el.dataset.start || 0));
    const duration = Number(el.dataset.duration || 1);
    tl.fromTo(el, { scaleX: 0 }, { scaleX: 1, duration: Math.max(.2, duration - .1), ease: 'none' }, start + .05);
  });

  // ---- Detail animations, anchored to each panel's own window --------------
  document.querySelectorAll('.visual-item').forEach(el => {
    const start = Math.max(0, Number(el.dataset.start || 0));
    const isMarket = el.classList.contains('visual-market');
    // 首个图板（start=0）直接以完成态上屏：柱状/蜡烛/画线的生长动画会掏空
    // 第 0 帧，而首帧正是平台抓分享卡的地方；后续图板照旧播生长动画。
    const born = start === 0;
    const bars = el.querySelectorAll('.bar');
    if (bars.length) {
      if (born) tl.set(bars, { scaleY: 1, opacity: 1 }, 0);
      else tl.fromTo(bars, { scaleY: 0, opacity: .3, transformOrigin: 'center bottom' }, { scaleY: 1, opacity: 1, stagger: .05, duration: .34 }, start + .2);
    }
    const gauge = el.querySelector('.gauge-fill');
    if (gauge) {
      if (born) tl.set(gauge, { strokeDasharray: 240, strokeDashoffset: 0 }, 0);
      else tl.fromTo(gauge, { strokeDasharray: 240, strokeDashoffset: 240 }, { strokeDashoffset: 0, duration: .6, ease: 'power1.inOut' }, start + .18);
    }
    const risk = el.querySelectorAll('.risk-line,.risk-area');
    if (risk.length) {
      if (born) tl.set(risk, { opacity: 1, y: 0 }, 0);
      else tl.fromTo(risk, { opacity: 0, y: 10 }, { opacity: 1, y: 0, stagger: .08, duration: .4 }, start + .18);
    }
    const sentiment = el.querySelector('.sentiment-positive,.sentiment-negative');
    if (sentiment) {
      if (born) tl.set(sentiment, { scaleX: 1 }, 0);
      else tl.fromTo(sentiment, { scaleX: 0, transformOrigin: 'left center' }, { scaleX: 1, duration: .5 }, start + .18);
    }
    if (!isMarket) return;
    el.querySelectorAll('.draw-line').forEach((node, i) => {
      if (born) tl.set(node, { strokeDashoffset: 0 }, 0);
      else tl.to(node, { strokeDashoffset: 0, duration: 1.05, ease: 'power1.inOut' }, start + .2 + i * .07);
    });
    const candles = el.querySelectorAll('.candle');
    if (candles.length) {
      if (born) tl.set(candles, { opacity: 1, scaleY: 1 }, 0);
      else tl.fromTo(candles, { opacity: .15, scaleY: .08, transformOrigin: 'center bottom' }, { opacity: 1, scaleY: 1, stagger: .016, duration: .26 }, start + .24);
    }
    const metrics = el.querySelectorAll('.metric');
    if (metrics.length) {
      if (born) tl.set(metrics, { opacity: 1, x: 0 }, 0);
      else tl.fromTo(metrics, { opacity: 0, x: 14 }, { opacity: 1, x: 0, stagger: .06, duration: .3 }, start + .3);
    }
  });

  // ---- Ambient drift -------------------------------------------------------
  tl.fromTo('.orb-a', { x: 0, y: 0, scale: .9 }, { x: 130, y: -70, scale: 1.14, duration: fullDuration, ease: 'sine.inOut' }, 0);
  tl.fromTo('.orb-b', { x: 0, y: 0, scale: 1.1 }, { x: -110, y: 90, scale: .88, duration: fullDuration, ease: 'sine.inOut' }, 0);

  // ---- Opening / closing title cards --------------------------------------
  if (intro) {
    // 开场卡第 0 帧即完整上屏（分享卡=首帧），随后在原时刻溶解，露出完成态的舞台。
    tl.set(intro, { opacity: 1 }, 0)
      .set(intro.querySelector('.intro-kicker'), { opacity: 1, y: 0, letterSpacing: '10px' }, 0)
      .set(intro.querySelector('.intro-title'), { opacity: 1, y: 0 }, 0)
      .to(intro, { opacity: 0, duration: .45, ease: 'power1.in' }, Math.max(.6, stageIn + .9));
  }
  if (outro) {
    const outroStart = Number(outro.dataset.start || Math.max(0, fullDuration - .45));
    tl.fromTo(outro, { opacity: 0 }, { opacity: 1, duration: .3, ease: 'power1.inOut' }, outroStart)
      .fromTo(outro.querySelector('.outro-kicker'), { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: .35 }, outroStart + .05)
      .fromTo(outro.querySelector('.outro-title'), { opacity: 0, y: 22 }, { opacity: 1, y: 0, duration: .4 }, outroStart + .08)
      .fromTo(outro.querySelector('.outro-tip'), { opacity: 0 }, { opacity: 1, duration: .35 }, outroStart + .16);
  }

  window.__timelines = window.__timelines || {};
  window.__timelines['stock-debate'] = tl;
})();

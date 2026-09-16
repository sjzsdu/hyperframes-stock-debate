(() => {
  const root = document.querySelector('#root');
  if (!root || typeof gsap === 'undefined') return;

  const tl = gsap.timeline({ paused: true, defaults: { ease: 'power2.out' } });

  const fullDuration = Number(root.dataset.duration || 1);
  const introCard = document.querySelector('#intro-card');
  // The intro card carries the title at the opening; the corner headline takes
  // over only once the card dissolves, so the name is never shown twice.
  const headlineIn = introCard ? 2.0 : 0;
  if (introCard) {
    // Park the headline/chart out of sight while the intro card owns the stage
    // (fromTo tweens placed later don't render their start state up front).
    tl.set('#headline', { opacity: 0, y: -24 }, 0)
      .set('#market-stage', { opacity: 0, scale: .975, y: 24 }, 0)
      .set('.metric', { opacity: 0, x: 16 }, 0);
  }
  tl.fromTo('#headline', { opacity: 0, y: -24 }, { opacity: 1, y: 0, duration: .55 }, headlineIn)
    .fromTo('#market-stage', { opacity: 0, scale: .975, y: 24 }, { opacity: 1, scale: 1, y: 0, duration: .6 }, headlineIn + .18)
    .fromTo('.metric', { opacity: 0, x: 16 }, { opacity: 1, x: 0, stagger: .06, duration: .3 }, headlineIn + .55);

  document.querySelectorAll('.draw-line').forEach((node, i) => {
    tl.to(node, { strokeDashoffset: 0, duration: 1.15, ease: 'power1.inOut' }, .5 + i * .07);
  });
  tl.fromTo('.candle', { opacity: .15, scaleY: .08, transformOrigin: 'center bottom' },
    { opacity: 1, scaleY: 1, stagger: .018, duration: .26 }, .52);

  // ---- Natural opening: intro title card rides over the real chart, then dissolves into the first dialogue slide ----
  const intro = introCard;
  if (intro) {
    const introEnd = Number(document.querySelector('#slide-2')?.dataset.start || 2.2);
    tl.fromTo(intro, { opacity: 0, scale: 1.04 }, { opacity: 1, scale: 1, duration: .45, ease: 'power2.out' }, 0)
      .fromTo(intro.querySelector('.intro-kicker'), { opacity: 0, y: 18, letterSpacing: '24px' }, { opacity: 1, y: 0, letterSpacing: '10px', duration: .5 }, .12)
      .fromTo(intro.querySelector('.intro-title'), { opacity: 0, y: 26 }, { opacity: 1, y: 0, duration: .5 }, .22)
      .to(intro, { opacity: 0, y: -20, duration: .4, ease: 'power1.in' }, Math.max(.5, introEnd - .5));
  }

  // ---- Natural closing: recap card fades over the last dialogue slide ----
  const outro = document.querySelector('#outro-card');
  if (outro) {
    const outroStart = Number(outro.dataset.start || Math.max(0, fullDuration - .45));
    tl.fromTo(outro, { opacity: 0 }, { opacity: 1, duration: .3, ease: 'power1.inOut' }, outroStart)
      .fromTo(outro.querySelector('.outro-kicker'), { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: .35 }, outroStart + .05)
      .fromTo(outro.querySelector('.outro-title'), { opacity: 0, y: 22 }, { opacity: 1, y: 0, duration: .4 }, outroStart + .08)
      .fromTo(outro.querySelector('.outro-tip'), { opacity: 0 }, { opacity: 1, duration: .35 }, outroStart + .16);
  }

  tl.fromTo('.orb-a', { x: 0, y: 0, scale: .9 }, { x: 130, y: -70, scale: 1.14, duration: fullDuration, ease: 'sine.inOut' }, 0);
  tl.fromTo('.orb-b', { x: 0, y: 0, scale: 1.1 }, { x: -110, y: 90, scale: .88, duration: fullDuration, ease: 'sine.inOut' }, 0);

  // ---- PPT-style slides: one full-screen page per dialogue turn ----
  document.querySelectorAll('.slide').forEach(slide => {
    const index = Number(slide.id.split('-')[1]);
    const start = Number(slide.dataset.start || 0);
    const duration = Number(slide.dataset.duration || 1);
    const isCover = index === 1;
    const accent = slide.classList.contains('bull') ? '#ff78bc' : '#65baff';

    // Slides carry opaque backgrounds, so hand-offs do not cross-fade: the old
    // slide stays fully opaque underneath while the new one fades in on top,
    // and is only hidden once the new slide has fully covered it.  A cross-fade
    // here briefly revealed the stage behind both slides (the visible "flash").
    const isLast = index === document.querySelectorAll('.slide').length;

    if (!isCover) {
      tl.set(slide, { opacity: 0, visibility: 'visible' }, start);
      tl.fromTo(slide, { opacity: 0, scale: 1.018, y: 26 }, { opacity: 1, scale: 1, y: 0, duration: .42 }, start);
      tl.fromTo(slide.querySelector('.slide-kicker'), { opacity: 0, x: -26 }, { opacity: 1, x: 0, duration: .34 }, start + .06);
      tl.fromTo(slide.querySelectorAll('.slide-keywords span'),
        { opacity: 0, y: 18, scale: .94 }, { opacity: 1, y: 0, scale: 1, stagger: .07, duration: .3 }, start + .12);
      tl.fromTo(slide.querySelector('.slide-visual'), { opacity: 0, y: 34, scale: .975 }, { opacity: 1, y: 0, scale: 1, duration: .4 }, start + .1);
      const speech = slide.querySelector('.speech');
      if (speech) {
        const captions = speech.querySelectorAll('.speech-line-item');
        if (captions.length) {
          // One-line captions: each chunk owns a slice of the turn, so only one
          // row is ever on screen and it matches what is being spoken.
          captions.forEach(caption => {
            const at = start + Number(caption.dataset.offset || 0);
            tl.set(caption, { opacity: 1 }, Math.max(0, at));
            tl.set(caption, { opacity: 0 }, Math.max(0, at + Number(caption.dataset.duration || .8)));
          });
        } else {
          tl.fromTo(speech, { opacity: 0, y: 30 }, { opacity: 1, y: 0, duration: .36 }, start + .14);
        }
        tl.fromTo(slide.querySelector('.speech-progress i'), { scaleX: 0 }, { scaleX: 1, duration: Math.max(.2, duration - .2), ease: 'none' }, start + .1);
      }
      if (!isLast && index > 2) {
        const previous = document.querySelector('#slide-' + (index - 1));
        if (previous) tl.set(previous, { visibility: 'hidden' }, start + .42);
      }
    } else {
      // The cover slide is a transparent title card: the headline, real chart
      // and data panel stay visible while the first line plays over them.
      tl.set(slide, { opacity: 1, visibility: 'visible' }, 0);
      tl.fromTo(slide.querySelector('.slide-kicker'), { opacity: 0, x: -26 }, { opacity: 1, x: 0, duration: .34 }, .5);
      tl.fromTo(slide.querySelectorAll('.slide-keywords span'),
        { opacity: 0, y: 18, scale: .94 }, { opacity: 1, y: 0, scale: 1, stagger: .07, duration: .3 }, .62);
      const speech = slide.querySelector('.speech');
      if (speech) {
        const captions = speech.querySelectorAll('.speech-line-item');
        if (captions.length) {
          captions.forEach(caption => {
            const at = Number(caption.dataset.offset || 0);
            tl.set(caption, { opacity: 1 }, Math.max(0, at));
            tl.set(caption, { opacity: 0 }, Math.max(0, at + Number(caption.dataset.duration || .8)));
          });
        } else {
          tl.fromTo(speech, { opacity: 0, y: 30 }, { opacity: 1, y: 0, duration: .4 }, .7);
        }
        tl.fromTo(slide.querySelector('.speech-progress i'), { scaleX: 0 }, { scaleX: 1, duration: Math.max(.2, duration - .2), ease: 'none' }, .6);
      }
      tl.set(slide, { opacity: 0, visibility: 'hidden' }, Number(document.querySelector('#slide-2')?.dataset.start || 1e9));
    }

    // Topic visuals animate in on their slide.
    const bars = slide.querySelectorAll('.slide-visual .bar');
    if (bars.length) tl.fromTo(bars, { scaleY: 0, opacity: .3, transformOrigin: 'center bottom' }, { scaleY: 1, opacity: 1, stagger: .05, duration: .34 }, start + .3);
    const gauge = slide.querySelector('.slide-visual .gauge-fill');
    if (gauge) tl.fromTo(gauge, { strokeDasharray: 240, strokeDashoffset: 240 }, { strokeDashoffset: 0, duration: .65, ease: 'power1.inOut' }, start + .26);
    const sentiment = slide.querySelector('.slide-visual .sentiment-positive,.slide-visual .sentiment-negative');
    if (sentiment) tl.fromTo(sentiment, { scaleX: 0, transformOrigin: 'left center' }, { scaleX: 1, duration: .55 }, start + .26);
    const risk = slide.querySelectorAll('.slide-visual .risk-line,.slide-visual .risk-area');
    if (risk.length) tl.fromTo(risk, { opacity: 0, y: 12 }, { opacity: 1, y: 0, stagger: .08, duration: .42 }, start + .26);
  });

  window.__timelines = window.__timelines || {};
  window.__timelines['stock-debate'] = tl;
})();

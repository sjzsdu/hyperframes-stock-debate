(() => {
  const root = document.querySelector('#root');
  if (!root || typeof gsap === 'undefined') return;

  const tl = gsap.timeline({ paused: true, defaults: { ease: 'power2.out' } });
  tl.fromTo('#headline', { opacity: 0, y: -24 }, { opacity: 1, y: 0, duration: .55 }, 0)
    .fromTo('#market-stage', { opacity: 0, scale: .975, y: 24 }, { opacity: 1, scale: 1, y: 0, duration: .6 }, .18)
    .fromTo('.character-host', { opacity: 0, y: 20 }, { opacity: 1, y: 0, stagger: .12, duration: .48 }, .35)
    .fromTo('.metric', { opacity: 0, x: 16 }, { opacity: 1, x: 0, stagger: .06, duration: .3 }, .55);

  document.querySelectorAll('.draw-line').forEach((node, i) => {
    tl.to(node, { strokeDashoffset: 0, duration: 1.15, ease: 'power1.inOut' }, .5 + i * .07);
  });
  tl.fromTo('.candle', { opacity: .15, scaleY: .08, transformOrigin: 'center bottom' },
    { opacity: 1, scaleY: 1, stagger: .018, duration: .26 }, .52);

  const fullDuration = Number(root.dataset.duration || 1);
  tl.fromTo('.grid', { backgroundPosition: '0px 0px' }, { backgroundPosition: '112px 56px', duration: fullDuration, ease: 'none' }, 0);
  tl.fromTo('.orb-a', { x: 0, y: 0, scale: .9 }, { x: 130, y: -70, scale: 1.14, duration: fullDuration, ease: 'sine.inOut' }, 0);
  tl.fromTo('.orb-b', { x: 0, y: 0, scale: 1.1 }, { x: -110, y: 90, scale: .88, duration: fullDuration, ease: 'sine.inOut' }, 0);
  const scanPasses = Math.max(1, Math.ceil(fullDuration / 6));
  const railPasses = Math.max(1, Math.ceil(fullDuration / 8));
  const scanTl = gsap.timeline();
  const railTl = gsap.timeline();
  for (let pass = 0; pass < scanPasses; pass += 1) {
    scanTl.fromTo('.scan-beam', { x: 0, opacity: .12 }, { x: 770, opacity: .62, duration: 3, ease: 'none' })
      .to('.scan-beam', { x: 0, opacity: .12, duration: 3, ease: 'none' });
  }
  for (let pass = 0; pass < railPasses; pass += 1) {
    railTl.fromTo('#debate-rail span', { x: -450, opacity: .45 }, { x: 450, opacity: 1, duration: 4, ease: 'sine.inOut' })
      .to('#debate-rail span', { x: -450, opacity: .45, duration: 4, ease: 'sine.inOut' });
  }
  tl.add(scanTl, 0).add(railTl, 0);

  document.querySelectorAll('.speech').forEach(card => {
    const index = card.id.split('-')[1];
    const start = Number(card.dataset.start || 0);
    const duration = Number(card.dataset.duration || 1);
    const visual = document.querySelector(`#visual-${index}`);
    const host = card.classList.contains('bull') ? document.querySelector('#bull-host') : document.querySelector('#bear-host');
    const other = card.classList.contains('bull') ? document.querySelector('#bear-host') : document.querySelector('#bull-host');
    const accent = card.classList.contains('bull') ? '#ff78bc' : '#65baff';
    const lead = Math.min(.34, duration * .12);
    const exitAt = start + Math.max(lead, duration - .22);

    tl.fromTo(card, { opacity: 0, y: 22, scale: .985 }, { opacity: 1, y: 0, scale: 1, duration: lead }, start);
    tl.fromTo(card.querySelector('.type-text'), { opacity: 0, x: card.classList.contains('bull') ? -18 : 18 }, { opacity: 1, x: 0, duration: .34 }, start + .12);
    tl.fromTo(card.querySelector('.speech-progress i'), { scaleX: 0 }, { scaleX: 1, duration: Math.max(.2, duration - .1), ease: 'none' }, start + .05);
    tl.fromTo(card.querySelectorAll('.equalizer i'), { scaleY: .28 }, {
      keyframes: [{ scaleY: 1 }, { scaleY: .45 }, { scaleY: .8 }, { scaleY: .3 }],
      stagger: .035, duration: Math.min(1.1, Math.max(.4, duration * .25)), ease: 'sine.inOut'
    }, start + .08);
    tl.to(card, { opacity: 0, y: -10, duration: Math.min(.2, duration * .08), ease: 'power1.in' }, exitAt);

    if (host) {
      tl.to(host.querySelector('.avatar'), { scale: 1.1, y: -5, boxShadow: `0 20px 56px ${accent}44`, duration: .28 }, start)
        .fromTo(host.querySelector('.voice-ring'), { opacity: .15, scale: .85 }, { opacity: .72, scale: 1.15, duration: .42 }, start + .04)
        .to(host.querySelector('.voice-ring'), { opacity: .2, scale: 1.34, duration: Math.max(.2, duration - .5), ease: 'none' }, start + .46)
        .to(host.querySelector('.avatar'), { scale: 1, y: 0, boxShadow: '0 18px 42px rgba(0,0,0,.4)', duration: .2 }, exitAt);
    }
    if (other) {
      tl.to(other, { opacity: .48, scale: .96, duration: .25 }, start)
        .to(other, { opacity: 1, scale: 1, duration: .2 }, exitAt);
    }

    if (visual) {
      tl.fromTo(visual, { opacity: 0, x: 28, scale: .94 }, { opacity: 1, x: 0, scale: 1, duration: .34 }, start + .1);
      const bars = visual.querySelectorAll('.bar');
      if (bars.length) tl.fromTo(bars, { scaleY: 0, opacity: .3, transformOrigin: 'center bottom' }, { scaleY: 1, opacity: 1, stagger: .055, duration: .34 }, start + .24);
      const gauge = visual.querySelector('.gauge-fill');
      if (gauge) tl.fromTo(gauge, { strokeDasharray: 240, strokeDashoffset: 240 }, { strokeDashoffset: 0, duration: .65, ease: 'power1.inOut' }, start + .2);
      const sentiment = visual.querySelector('.sentiment-positive,.sentiment-negative');
      if (sentiment) tl.fromTo(sentiment, { scaleX: 0, transformOrigin: 'left center' }, { scaleX: 1, duration: .55 }, start + .2);
      const risk = visual.querySelectorAll('.risk-line,.risk-area');
      if (risk.length) tl.fromTo(risk, { opacity: 0, y: 12 }, { opacity: 1, y: 0, stagger: .08, duration: .42 }, start + .2);
      tl.to(visual, { opacity: 0, x: 18, duration: .18, ease: 'power1.in' }, exitAt);
    }

    const camera = document.querySelector('.chart-camera');
    if (camera) {
      const direction = Number(index) % 2 ? -1 : 1;
      tl.to(camera, { scale: 1.035, x: 7 * direction, y: -4, duration: .45, ease: 'power1.inOut' }, start + .3)
        .to(camera, { scale: 1, x: 0, y: 0, duration: .55, ease: 'power1.inOut' }, Math.min(exitAt - .55, start + Math.max(.9, duration * .58)));
    }
  });

  window.__timelines = window.__timelines || {};
  window.__timelines['stock-debate'] = tl;
})();

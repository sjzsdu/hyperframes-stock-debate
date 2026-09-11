(() => {
  const root = document.querySelector('#root');
  if (!root || typeof gsap === 'undefined') return;  // Never leave animated panels invisible.
  const tl = gsap.timeline({ paused: true });
  tl.from('#headline', { opacity: 0, y: -28, duration: .6 })
    .from('.finance,.chart', { opacity: 0, y: 25, stagger: .12, duration: .45 }, .3);
  document.querySelectorAll('.draw-line').forEach((node, i) => tl.to(node, { strokeDashoffset: 0, duration: .9, ease: 'power1.inOut' }, .55 + i * .08));
  document.querySelectorAll('.speech').forEach(card => {
    const start = Number(card.dataset.start || 0);
    const visual = document.querySelector(`#visual-${card.id.split('-')[1]}`);
    tl.fromTo(card, { opacity: 0, y: 18 }, { opacity: 1, y: 0, duration: .28 }, start);
    if (visual) {
      tl.fromTo(visual, { opacity: 0, scale: .92 }, { opacity: 1, scale: 1, duration: .32 }, start + .12);
      const bars = visual.querySelectorAll('.bar');
      if (bars.length) tl.from(bars, { scaleY: 0, transformOrigin: 'center bottom', stagger: .08, duration: .38, ease: 'power2.out' }, start + .2);
      const gauge = visual.querySelector('.gauge-fill');
      if (gauge) tl.from(gauge, { strokeDasharray: 220, strokeDashoffset: 220, duration: .55, ease: 'power1.out' }, start + .2);
      const radar = visual.querySelector('.radar-shape');
      if (radar) tl.from(radar, { scale: 0, transformOrigin: 'center', duration: .45, ease: 'back.out(1.5)' }, start + .2);
      const sentiment = visual.querySelector('.sentiment-positive,.sentiment-negative');
      if (sentiment) tl.from(sentiment, { scaleX: 0, transformOrigin: 'left center', duration: .45, ease: 'power2.out' }, start + .2);
      const risk = visual.querySelector('.risk-line,.risk-area');
      if (risk) tl.from(risk, { opacity: 0, y: 10, duration: .4 }, start + .2);
    }
  });
  window.__timelines = window.__timelines || {};
  window.__timelines['stock-debate'] = tl;
})();

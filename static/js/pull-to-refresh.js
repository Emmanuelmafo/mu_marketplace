(function () {
  if (window.__muPullToRefreshInstalled) return;
  window.__muPullToRefreshInstalled = true;
  var startY = 0, pulling = false, distance = 0, threshold = 92;
  var indicator = document.createElement('div');
  indicator.className = 'mu-pull-refresh-indicator';
  indicator.innerHTML = '<span class="mu-pull-refresh-arrow" aria-hidden="true">↓</span><span class="mu-pull-refresh-label">Pull down to refresh</span>';
  document.body.appendChild(indicator);
  var style = document.createElement('style');
  style.textContent = `
    .mu-pull-refresh-indicator{position:fixed;top:0;left:50%;z-index:100000;display:flex;align-items:center;gap:9px;padding:9px 15px;border-radius:0 0 14px 14px;background:#123c2c;color:#fff;font:600 12px/1.2 Arial,sans-serif;box-shadow:0 5px 16px #123c2c30;opacity:0;transform:translate(-50%,-110%);pointer-events:none;transition:opacity .12s}
    .mu-pull-refresh-indicator.visible{opacity:1}
    .mu-pull-refresh-arrow{font-size:17px;display:inline-block;transition:transform .1s}
    .mu-pull-refresh-indicator.ready .mu-pull-refresh-arrow{transform:rotate(180deg)}
  `;
  document.head.appendChild(style);
  function setIndicator(y) {
    var progress = Math.min(y / threshold, 1);
    indicator.style.transform = 'translate(-50%,' + (-110 + progress * 110) + '%)';
    indicator.classList.add('visible');
    indicator.classList.toggle('ready', y >= threshold);
    indicator.querySelector('.mu-pull-refresh-label').textContent = y >= threshold ? 'Release to refresh' : 'Pull down to refresh';
  }
  document.addEventListener('touchstart', function (event) {
    if (!event.touches || event.touches.length !== 1) return;
    if (window.scrollY <= 2 && document.documentElement.scrollTop <= 2) {
      startY = event.touches[0].clientY; pulling = true; distance = 0;
    } else pulling = false;
  }, {passive:true});
  document.addEventListener('touchmove', function (event) {
    if (!pulling || !event.touches || event.touches.length !== 1) return;
    var delta = event.touches[0].clientY - startY;
    if (delta <= 0 || window.scrollY > 2 || document.documentElement.scrollTop > 2) {
      pulling = false; indicator.classList.remove('visible'); return;
    }
    distance = delta;
    if (distance > 8) { setIndicator(distance); if (event.cancelable) event.preventDefault(); }
  }, {passive:false});
  document.addEventListener('touchend', function () {
    if (!pulling) return;
    pulling = false;
    if (distance >= threshold) {
      indicator.querySelector('.mu-pull-refresh-label').textContent = 'Refreshing...';
      indicator.classList.add('ready'); window.location.reload();
    } else {
      indicator.classList.remove('visible','ready');
      indicator.style.transform = 'translate(-50%,-110%)';
    }
    distance = 0;
  }, {passive:true});
  document.addEventListener('touchcancel', function () {
    pulling = false; distance = 0;
    indicator.classList.remove('visible','ready');
    indicator.style.transform = 'translate(-50%,-110%)';
  }, {passive:true});
})();
/* =============================================================================
   Wings Theory — shared brand intro.

     WingsIntro.play()            -> Promise, resolves when the sequence ends
     WingsIntro.play({hold:600})  -> extra dwell on the final frame

   Injects its own markup so a page only needs to link brand.css + brand.js.
   Click anywhere or press Skip/Esc to cut it short; the promise still
   resolves, so callers can always rely on it to continue the flow.

   Deliberately never blocks anything real: authentication, menu loading and
   navigation all run independently of whether this finished playing.
   ============================================================================= */
(function (global) {
  'use strict';

  var TOTAL_MS = 3150;          // storyboard runtime before the overlay clears
  var el = null;

  function build() {
    if (el) return el;
    el = document.createElement('div');
    el.className = 'wt-intro';
    el.setAttribute('hidden', '');
    el.innerHTML =
      '<div class="wt-smoke"><i></i><i></i><i></i></div>' +
      '<div class="wt-stage">' +
        '<div class="wt-lockup">' +
          '<span class="wt-flash"></span>' +
          '<img class="wt-wing wt-wing-l" src="/img/wing.png" alt="">' +
          '<img class="wt-wing wt-wing-r" src="/img/wing.png" alt="">' +
          '<img class="wt-logo" src="/img/logo.png" alt="Wings Theory">' +
          '<span class="wt-ring"></span>' +
          '<span class="wt-ring wt-ring2"></span>' +
          '<span class="wt-streaks"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span>' +
          '<span class="wt-embers"><i></i><i></i><i></i><i></i><i></i><i></i></span>' +
        '</div>' +
        '<img class="wt-tag" src="/img/tagline.png" alt="Flavor. Science. Perfection.">' +
      '</div>' +
      '<button type="button" class="wt-skip">Skip &#9193;</button>';
    document.body.appendChild(el);
    return el;
  }

  function play(opts) {
    opts = opts || {};
    var node = build();
    var reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    var runtime = reduced ? 500 : TOTAL_MS + (opts.hold || 0);

    node.removeAttribute('hidden');
    node.classList.remove('wt-out', 'wt-play');
    void node.offsetWidth;                 // restart the CSS animations
    node.classList.add('wt-play');

    return new Promise(function (resolve) {
      var done = false;
      var timer;

      function finish() {
        if (done) return;
        done = true;
        clearTimeout(timer);
        document.removeEventListener('keydown', onKey);
        node.classList.add('wt-out');
        setTimeout(function () {
          node.setAttribute('hidden', '');
          node.classList.remove('wt-play', 'wt-out');
          resolve();
        }, 450);
      }

      function onKey(e) { if (e.key === 'Escape' || e.key === 'Enter' || e.key === ' ') finish(); }

      node.onclick = finish;
      node.querySelector('.wt-skip').onclick = function (e) { e.stopPropagation(); finish(); };
      document.addEventListener('keydown', onKey);
      timer = setTimeout(finish, runtime);
    });
  }

  global.WingsIntro = { play: play };
})(window);

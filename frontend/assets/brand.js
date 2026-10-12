/* =============================================================================
   Wings Theory — shared brand boot screen.

     WingsIntro.boot({ roleLabel, destLabel })  -> Promise, resolves when done
     WingsIntro.boot({ ..., hold: 600 })        -> extra dwell on the final frame

   Injects its own markup so a page only needs to link brand.css + brand.js.
   One overlay, used for every login everywhere in the system (landing page,
   and a cold direct visit to /pos, /kds or /console) — there used to be a
   second, different overlay (a wing-strike cold-open, played before sign-in)
   but that made login look like two unrelated animations depending on how
   you arrived. This is the only one now.

   Deliberately never blocks anything real: authentication, menu loading and
   navigation all run independently of whether this finished playing.
   ============================================================================= */
(function (global) {
  "use strict";

  var BOOT_MS = 4000; // fast and confident, not a captive cutscene
  var bootEl = null;

  function buildBoot() {
    if (bootEl) return bootEl;
    bootEl = document.createElement("div");
    bootEl.className = "wt-boot";
    bootEl.setAttribute("hidden", "");
    bootEl.innerHTML =
      '<div class="wt-boot-grain"></div>' +
      '<div class="wt-boot-mid">' +
      '<div class="wt-boot-tagline">' +
      '<span class="wt-boot-line wt-boot-l1"><span class="wt-boot-mask">FLAVOR<i class="wt-boot-dot">.</i></span></span>' +
      '<span class="wt-boot-line wt-boot-l2"><span class="wt-boot-mask">SCIENCE<i class="wt-boot-dot">.</i></span></span>' +
      '<span class="wt-boot-line wt-boot-l3"><span class="wt-boot-mask">PERFECTION<i class="wt-boot-dot wt-boot-dot-alt">.</i></span></span>' +
      "</div>" +
      '<div class="wt-boot-access"></div>' +
      '<div class="wt-boot-dest"></div>' +
      '<div class="wt-boot-bar"><i></i></div>' +
      "</div>";
    document.body.appendChild(bootEl);
    return bootEl;
  }

  function boot(opts) {
    opts = opts || {};
    var node = buildBoot();
    var reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    var runtime = reduced ? 450 : BOOT_MS + (opts.hold || 0);

    node.querySelector(".wt-boot-access").textContent = opts.roleLabel
      ? "ACCESS GRANTED · " + String(opts.roleLabel).toUpperCase()
      : "";
    node.querySelector(".wt-boot-dest").textContent =
      "LOADING " + String(opts.destLabel || "WORKSPACE").toUpperCase();

    node.removeAttribute("hidden");
    node.classList.remove("wt-boot-out", "wt-boot-play");
    void node.offsetWidth; // restart the CSS animations
    node.classList.add("wt-boot-play");

    return new Promise(function (resolve) {
      var done = false;
      var timer;
      function finish() {
        if (done) return;
        done = true;
        clearTimeout(timer);
        node.classList.add("wt-boot-out");
        setTimeout(function () {
          node.setAttribute("hidden", "");
          node.classList.remove("wt-boot-play", "wt-boot-out");
          resolve();
        }, 380);
      }
      timer = setTimeout(finish, runtime);
    });
  }

  global.WingsIntro = { boot: boot };
})(window);

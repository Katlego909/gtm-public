// Subtle fade/rise-in animation for elements marked `.reveal`, shared across
// landing.html, gtm/base.html and dashboard/base.html. Only hides content
// (via the `.reveal-ready` class on <html>) once IntersectionObserver support
// is confirmed, so a script failure never leaves sections stuck invisible.
(function () {
  if (!('IntersectionObserver' in window)) return;

  document.documentElement.classList.add('reveal-ready');

  var observer = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    },
    { root: null, rootMargin: '0px 0px -8% 0px', threshold: 0.1 }
  );

  // Anything already on screen is shown at once, with no transition: fading
  // in content the user is already looking at only delays it. Only elements
  // below the fold are left to the observer's scroll-in animation.
  function observeAll() {
    var viewH = window.innerHeight || document.documentElement.clientHeight;
    document.querySelectorAll('.reveal:not(.is-visible)').forEach(function (el) {
      var rect = el.getBoundingClientRect();
      if (rect.top < viewH && rect.bottom > 0) {
        el.classList.add('reveal-instant', 'is-visible');
      } else {
        observer.observe(el);
      }
    });
  }

  observeAll();

  // Re-scan after HTMX swaps content in (dashboard partials, results polling).
  document.body.addEventListener('htmx:afterSettle', observeAll);
})();

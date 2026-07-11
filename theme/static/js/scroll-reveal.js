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

  function observeAll() {
    document.querySelectorAll('.reveal:not(.is-visible)').forEach(function (el) {
      observer.observe(el);
    });
  }

  observeAll();

  // Re-scan after HTMX swaps content in (dashboard partials, results polling).
  document.body.addEventListener('htmx:afterSettle', observeAll);
})();

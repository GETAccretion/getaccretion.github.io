// Ezoic ads. Loaded only when the build's EZOIC switch is on (scripts/site.py adds it to every page,
// after Ezoic's consent and header scripts). Ad spots are empty <div class="ezoicad"> markers.
// Supporters, moderators and admins get the acc_adfree cookie (site/social.js); for them every
// format is switched off and nothing is placed (an Ezoic cookie page rule does the same server-side).
(function () {
  "use strict";
  var ez = (window.ezstandalone = window.ezstandalone || {});
  ez.cmd = ez.cmd || [];
  var store = Boolean(document.getElementById("home")); // the single-page store (hash routes)

  function adFree() {
    return document.cookie.split("; ").indexOf("acc_adfree=1") >= 0;
  }

  if (store) {
    ez.cmd.push(function () {
      ez.setIsSinglePageApplication(true);
    });
  }

  // first: the page's first view (a full load); later views come from hash changes in the store
  function show(first) {
    var free = adFree();
    ez.cmd.push(function () {
      ez.config({
        disableInterstitial: free,
        disableLeftSideRail: free,
        disableRightSideRail: free,
        disableVideo: free,
        disableSidebarFloating: free,
      });
      ez.setEzoicAnchorAd(!free);
      ez.setInterstitialAllowed(!free);
      ez.setOutstreamAllowed(!free);
      if (!first) {
        ez.newPage();
        ez.destroyAll();
      } else if (free) {
        ez.destroyAll();
      }
      if (!free) ez.showAds(".ezoicad");
    });
  }

  window.AccretionEzoic = { show: show, adFree: adFree };
  // static pages place their spots on load; header-only pages (EZOIC=header) have none and stay untouched
  if (!store) {
    document.addEventListener("DOMContentLoaded", function () {
      if (document.querySelector(".ezoicad")) show(true);
    });
  }
})();

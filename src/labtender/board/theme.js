"use strict";
// Dark or light, chosen with the moon or sun button and remembered in this browser. Loaded in
// <head> so the page is drawn in its theme from the first frame.
(() => {
  const KEY = "labtender-theme";
  const get = () => { try { return localStorage.getItem(KEY) === "light" ? "light" : "dark"; } catch { return "dark"; } };
  const apply = (t) => {
    if (t === "light") document.documentElement.dataset.theme = "light";
    else delete document.documentElement.dataset.theme;
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => {
      b.innerHTML = t === "light" ? MOON : SUN;
      b.title = b.ariaLabel = t === "light" ? "Dark theme" : "Light theme";
    });
  };
  const MOON = '<svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M21 14.5A9 9 0 0 1 9.5 3a9 9 0 1 0 11.5 11.5z"/></svg>';
  const SUN = '<svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.5" fill="currentColor"/><g stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></g></svg>';
  apply(get());
  document.addEventListener("DOMContentLoaded", () => apply(get()));
  document.addEventListener("click", (e) => {
    if (!e.target.closest("[data-theme-toggle]")) return;
    const next = get() === "light" ? "dark" : "light";
    try { localStorage.setItem(KEY, next); } catch { /* private window: not remembered */ }
    apply(next);
  });
})();

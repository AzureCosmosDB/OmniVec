/* Shared display-only resource labels for both consoles. */
(() => {
'use strict';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(value) {
  const name = String(value ?? ''), chars = Array.from(name);
  const split = Math.max(0, chars.length - 5);
  return `<span class="resource-name" title="${esc(name)}"><span class="resource-name-prefix">${esc(chars.slice(0, split).join(''))}</span><span class="resource-name-suffix">${esc(chars.slice(split).join(''))}</span></span>`;
}
function shorten(value, limit = 40) {
  const name = String(value ?? ''), chars = Array.from(name);
  return chars.length <= limit ? name : chars.slice(0, limit - 8).join('') + '...' + chars.slice(-5).join('');
}
window.OVNames = { render, shorten };
})();

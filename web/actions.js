/* One click listener for the whole page.
 *
 * Every button carries `data-action="name"` and the handlers live here, which
 * is what killed the last class of injection bug: the page this replaces built
 * seventeen `onclick="go('...')"` attributes by interpolating values into
 * single quotes, with an `esc()` that escaped `& < > "` and not `'`, over
 * fixture ids that come from Kalshi and ESPN rather than from us.
 *
 * Its own module rather than a corner of app.js so that a view can register a
 * handler without importing the router that imports the view.
 */

let actions = Object.create(null);

/** Add handlers for the current route. Called by a view in its `ready`. */
export function addActions(map) { Object.assign(actions, map); }

/** Drop every handler and install the route's own. Called by the router. */
export function resetActions(map) {
  actions = Object.assign(Object.create(null), map || {});
}

document.addEventListener("click", ev => {
  const el = ev.target.closest("[data-action]");
  if (!el) return;
  const fn = actions[el.dataset.action];
  if (!fn) return;
  ev.preventDefault();
  fn(el, ev);
});

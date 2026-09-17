/* One opaque id per browser, shared by every surface that takes feedback.
 *
 * Not a person: localStorage, random, never sent anywhere except as the
 * dedup key the RPCs use. It lived inside compare.js until the window flags
 * and the crowd guess needed the same id — three copies of this would mean a
 * visitor who flags a trace and casts a guess counts as three people.
 */
export const VOTER = (() => {
  try {
    let v = localStorage.getItem("rsi_voter");
    if (!v) { v = Math.random().toString(36).slice(2, 12); localStorage.setItem("rsi_voter", v); }
    return v;
  } catch (e) { return "no-storage"; }         // private mode still gets a voice
})();

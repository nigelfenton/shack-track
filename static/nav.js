/* The one top bar, shared by every page so the order and the look cannot drift
   apart again. A page includes it where the bar should appear:

       <script src="/static/nav.js" data-here="passes"></script>

   data-here names the current tab: passes, weather, live, bird or setup.
   A ?grid= in the address is carried to the pages that understand it, so a
   visitor looking at their own square stays on it while moving between tabs. */
(() => {
  const me = document.currentScript;
  // The pass list and the Weather page are one file at two addresses.
  const here = /^\/weather/.test(location.pathname) ? 'weather' : (me.dataset.here || '');
  const grid = (new URLSearchParams(location.search).get('grid') || '').trim().toUpperCase();
  const q = grid ? `?grid=${encodeURIComponent(grid)}` : '';
  const tabs = [
    ['passes', '/' + q, 'Passes', 'Every pass coming up'],
    ['weather', '/weather' + q, 'Weather', 'Geostationary weather satellites and Meteor passes'],
    ['live', '/live', 'Operating', 'The pass in progress: frequencies, Doppler and Track'],
    ['bird', '/bird' + q, 'Bird', 'One pass at a time, for a second screen'],
    ['strip', '/strip', 'Strip', 'A one-line status strip'],
  ];
  const css = `
.stnav{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.stnav .brand{font-family:Chivo,ui-sans-serif,system-ui,sans-serif;font-weight:900;font-size:1.02rem;
  letter-spacing:-.01em;color:var(--ink);text-decoration:none;white-space:nowrap}
.stnav .brand i{font-style:normal;color:var(--accent,var(--track))}
.stnav .tabs{display:flex;flex-wrap:wrap}
.stnav .tabs a{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:.7rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--label);text-decoration:none;padding:7px 14px;
  border:1px solid var(--edge);border-right-width:0;background:var(--panel)}
.stnav .tabs a:first-child{border-radius:6px 0 0 6px}
.stnav .tabs a:last-child{border-right-width:1px;border-radius:0 6px 6px 0}
.stnav .tabs a:hover{color:var(--ink-2)}
.stnav .tabs a[aria-current="page"]{color:var(--ink);background:var(--panel-2);border-color:var(--accent,var(--track));
  border-right-width:1px}
.stnav .tabs a[aria-current="page"] + a{border-left-width:0}
.stnav a:focus-visible{outline:2px solid var(--accent,var(--track));outline-offset:2px}
.stnav .sp{flex:1}
.stnav [hidden]{display:none!important}
.stnav .gear{display:inline-flex;align-items:center;gap:6px;font-family:"JetBrains Mono",ui-monospace,monospace;
  font-size:.7rem;letter-spacing:.12em;text-transform:uppercase;color:var(--label);text-decoration:none;
  padding:6px 10px;border:1px solid transparent;border-radius:6px}
.stnav .gear:hover{color:var(--ink-2);border-color:var(--edge)}
.stnav .gear[aria-current="page"]{color:var(--ink);background:var(--panel-2);border-color:var(--accent,var(--track))}
.stnav .gear svg{width:14px;height:14px;display:block}
/* Phone: name and Setup share the first line, the tabs fill the second. */
@media (max-width:560px){
  .stnav{gap:8px}
  .stnav .tabs{order:3;flex:1 0 100%}
  .stnav .tabs a{flex:1 1 auto;text-align:center;padding:8px 6px;letter-spacing:.04em}
}`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const cur = (k) => (k === here ? ' aria-current="page"' : '');
  const nav = document.createElement('nav');
  nav.className = 'stnav';
  nav.setAttribute('aria-label', 'Shack-Track pages');
  nav.innerHTML = `<a class="brand" href="/${q}">Shack<i>-</i>Track</a>
    <span class="tabs">${tabs.map(([k, href, label, tip]) =>
      `<a href="${href}" title="${tip}"${cur(k)}>${label}</a>`).join('')}</span>
    <span class="sp"></span>
    <a class="gear" href="/setup" title="Station setup: location, radio, receiver"${cur('setup')}${here === 'setup' ? '' : ' hidden'}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
           stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>
      <span>Setup</span></a>`;
  me.replaceWith(nav);

  // The gear shows only where Setup is open to this visitor. A station on the
  // public internet puts /setup behind a login at its web server; a visitor
  // there should not be offered a door that only asks for a password. The
  // question is asked without credentials so it can never raise a login
  // prompt, which also means the owner, on the public address, reaches Setup
  // by typing /setup. The answer is kept for the browser session.
  if (here !== 'setup') {
    const gear = nav.querySelector('.gear');
    const show = (open) => { gear.hidden = !open; };
    let known = null;
    try { known = sessionStorage.getItem('st.setupOpen'); } catch (e) {}
    if (known !== null) show(known === '1');
    else fetch('/api/setup', {credentials: 'omit', cache: 'no-store'})
      .then((r) => r.ok, () => false)
      .then((open) => { show(open); try { sessionStorage.setItem('st.setupOpen', open ? '1' : '0'); } catch (e) {} });
  }
})();

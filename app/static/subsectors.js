// Populate the sub-sector dropdown from the selected sector.
(function () {
  const sector = document.getElementById("sector");
  const sub = document.getElementById("sub_sector");
  if (!sector || !sub) return;

  async function load() {
    try {
      const r = await fetch("/api/subsectors?sector=" + encodeURIComponent(sector.value));
      const opts = await r.json();
      sub.innerHTML = "";
      opts.forEach(function (o) {
        const el = document.createElement("option");
        el.textContent = o;
        sub.appendChild(el);
      });
    } catch (e) { /* leave empty on error */ }
  }
  sector.addEventListener("change", load);
  load();
})();

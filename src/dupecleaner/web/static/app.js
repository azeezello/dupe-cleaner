let currentScanId = null;
let currentReport = null;
let pollTimer = null;
let currentDetailHash = null;

function selectedMode() {
  const picked = document.querySelector('input[name="scan-mode"]:checked');
  return picked ? picked.value : "quick";
}

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function fmtBytes(n) {
  if (!n) return "0 Б";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s} сек`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} мин ${s % 60} сек`;
  return `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

function fmtNumber(n) {
  return (n ?? 0).toLocaleString("ru-RU");
}

// --- index stats (proof that work is cached across runs) --------------------

async function refreshIndexStats() {
  try {
    const stats = await (await fetch("/api/index-stats")).json();
    $("index-stats").textContent =
      `В индексе: ${fmtNumber(stats.files_indexed)} файлов (${fmtBytes(stats.bytes_indexed)}), ` +
      `из них с готовым хэшем — ${fmtNumber(stats.files_with_valid_hash)}. ` +
      `Эти файлы при повторном скане перечитываться не будут.`;
  } catch {
    $("index-stats").textContent = "";
  }
}

// --- quarantine directory: remembered across reloads -------------------
// A real, local product (unlike a one-off page) — the reviewer sets this
// once per machine and it should stay set, not reset every time the tab
// is reopened mid-review.
const QDIR_STORAGE_KEY = "dupecleaner.quarantineDir";
try {
  const savedDir = localStorage.getItem(QDIR_STORAGE_KEY);
  if (savedDir) $("quarantine-dir").value = savedDir;
} catch {
  // Private browsing / blocked storage: just start blank, same as before.
}
$("quarantine-dir").addEventListener("change", () => {
  try {
    localStorage.setItem(QDIR_STORAGE_KEY, $("quarantine-dir").value.trim());
  } catch {
    /* non-fatal */
  }
});

// --- scanning ---------------------------------------------------------------

$("scan-btn").addEventListener("click", async () => {
  const paths = $("paths").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (paths.length === 0) {
    $("scan-error").hidden = false;
    $("scan-error").textContent = "Укажите хотя бы одну папку.";
    return;
  }

  const resp = await fetch("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, mode: selectedMode() }),
  });

  if (!resp.ok) {
    $("scan-error").hidden = false;
    $("scan-error").textContent = `Ошибка: ${await resp.text()}`;
    return;
  }

  const started = await resp.json();
  currentScanId = started.scan_id;
  currentReport = null;

  $("progress-card").hidden = false;
  $("results").hidden = true;
  $("warnings-card").hidden = true;
  $("scan-error").hidden = true;
  $("scan-btn").disabled = true;
  $("cancel-btn").hidden = false;

  startPolling();
});

$("cancel-btn").addEventListener("click", async () => {
  if (!currentScanId) return;
  $("cancel-btn").disabled = true;
  await fetch(`/api/scan/${currentScanId}/cancel`, { method: "POST" });
});

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(pollProgress, 500);
  pollProgress();
}

async function pollProgress() {
  if (!currentScanId) return;
  let progress;
  try {
    const resp = await fetch(`/api/scan/${currentScanId}/progress`);
    if (!resp.ok) return;
    progress = await resp.json();
  } catch {
    return; // server momentarily unreachable; keep polling
  }

  renderProgress(progress);

  if (["done", "cancelled", "failed"].includes(progress.status)) {
    clearInterval(pollTimer);
    $("scan-btn").disabled = false;
    $("cancel-btn").hidden = true;
    $("cancel-btn").disabled = false;
    refreshIndexStats();
    renderWarnings(progress.warnings);

    if (progress.status === "done") {
      await loadResult();
    } else if (progress.status === "failed") {
      $("scan-error").hidden = false;
      $("scan-error").textContent = `Скан прерван: ${progress.error}`;
    }
  }
}

function renderProgress(p) {
  $("phase-label").textContent = p.phase_label;
  $("elapsed").textContent = `прошло ${fmtDuration(p.elapsed_seconds)}`;

  const bar = $("bar");
  if (p.percent == null) {
    bar.classList.add("indeterminate");
  } else {
    bar.classList.remove("indeterminate");
    bar.style.width = `${p.percent.toFixed(1)}%`;
  }

  if (p.phase_files_total > 0) {
    const pct = p.percent == null ? "" : ` — ${p.percent.toFixed(1)}%`;
    $("phase-detail").textContent =
      `${fmtNumber(p.phase_files_done)} / ${fmtNumber(p.phase_files_total)} файлов` +
      (p.phase_bytes_total > 0 ? ` · ${fmtBytes(p.phase_bytes_done)} / ${fmtBytes(p.phase_bytes_total)}` : "") +
      pct;
  } else if (p.status === "enumerating") {
    $("phase-detail").textContent = "общее число файлов пока неизвестно — идёт обход";
  } else {
    $("phase-detail").textContent = "";
  }

  $("eta").textContent = p.eta_seconds != null ? `осталось ~${fmtDuration(p.eta_seconds)}` : "";
  $("stat-files").textContent = fmtNumber(p.files_seen);
  $("stat-bytes").textContent = fmtBytes(p.bytes_seen);
  $("stat-hashed").textContent = fmtNumber(p.files_hashed);
  $("stat-cache").textContent = fmtNumber(p.files_from_cache);
  $("stat-rate").textContent = p.bytes_per_second ? `${fmtBytes(p.bytes_per_second)}/с` : "—";
  $("current-path").textContent = p.current_path || "—";

  const stall = $("stall-warning");
  if (p.is_stalled) {
    stall.hidden = false;
    stall.textContent =
      `Ничего не менялось ${fmtDuration(p.seconds_since_update)} — похоже, застряли на этом файле. ` +
      `Это бывает на сетевых дисках и сбойных секторах. Можно нажать «Остановить»: ` +
      `посчитанное сохранено, повторный запуск продолжит с этого места.`;
  } else {
    stall.hidden = true;
  }
}

function renderWarnings(warnings) {
  if (!warnings || warnings.length === 0) return;
  $("warnings-card").hidden = false;
  const list = $("warnings-list");
  list.innerHTML = "";
  warnings.slice(0, 200).forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    list.appendChild(li);
  });
  if (warnings.length > 200) {
    const li = document.createElement("li");
    li.textContent = `…и ещё ${warnings.length - 200}`;
    list.appendChild(li);
  }
}

// --- results ------------------------------------------------------------
//
// Task 11: a virtualized tile grid over the groups a scan found. One tile
// per *group*, not per record — every copy in a group is byte-identical
// (that is what makes it a group), so a thumbnail repeated three times in
// a row would only lengthen the scroll, not add information (see the
// grid-full screen in docs/UX-MOCKUPS.html).
//
// Task 12 builds the actual review workflow on top of that grid: keyboard
// navigation that tracks one *logical* active index (not a DOM node —
// nodes are recycled, see createVirtualGrid below), a per-group decision
// that is recorded instantly and separately from ever moving a file, and
// a batch "apply what's been reviewed" step that is the only thing that
// touches the filesystem. See pilot finding P1.1 and
// claude/plan.md's task 12 for why that split exists.
//
// The report itself still arrives as one JSON response (pilot finding
// P2.9: no pagination, 10.4 MB / 8814 groups on the real disk this was
// built against — see claude/design-decisions.md for the numbers behind
// that call and the gzip middleware added in app.py to shrink the actual
// transfer).

// --- pooled/windowed virtualization ----------------------------------------
//
// Chosen over an IntersectionObserver-per-tile approach (docs/design-
// decisions.md's original sketch) once the actual layout turned out to be
// a fixed-size tile *grid*, not a variable-height list: observing 8814
// elements individually buys nothing a grid with known tile dimensions
// doesn't already get for free from arithmetic. A fixed pool of DOM nodes
// is positioned with `transform: translate(...)` from the scroll offset,
// and content is rewritten into whichever pool node a given index maps to
// — the same technique the reference implementation in
// docs/UX-MOCKUPS.html (screens 3 and 4) demonstrates. Memory is bounded
// by the pool size (visible rows + overscan) times columns, never by the
// item count, which is the property task 11 actually needs.
//
// Task 12 adds one more piece of state on top: `activeIndex`, the
// logically "current" group for keyboard navigation and decisions. It
// lives here (not on a DOM node) for exactly the same reason a group's
// decision lives in the index and not in the browser: a pool node gets
// handed to a different item every time you scroll two rows, so "the
// focused tile" can never mean "that DOM element" without breaking the
// moment scrolling happens.
function createVirtualGrid({ viewport, sizer, pool, tileWidth, tileHeight, gap, overscan, renderTile }) {
  let items = [];
  let columns = 1;
  let slots = [];
  let rafPending = false;
  let activeIndex = -1;

  function computeColumns() {
    const w = viewport.clientWidth || tileWidth;
    columns = Math.max(1, Math.floor((w + gap) / (tileWidth + gap)));
    const rows = Math.ceil(items.length / columns);
    sizer.style.height = items.length ? `${rows * (tileHeight + gap)}px` : "0px";
  }

  function ensurePool() {
    const visibleRows = Math.ceil((viewport.clientHeight || 1) / (tileHeight + gap)) + overscan * 2;
    const needed = Math.max(1, visibleRows) * columns;
    while (slots.length < needed) {
      const node = document.createElement("div");
      node.className = "tile";
      node.tabIndex = 0;
      pool.appendChild(node);
      slots.push({ node, hash: null });
    }
    while (slots.length > needed && slots.length > columns * 4) {
      const extra = slots.pop();
      extra.node.remove();
    }
  }

  function render() {
    rafPending = false;
    const scrollTop = viewport.scrollTop;
    const startRow = Math.max(0, Math.floor(scrollTop / (tileHeight + gap)) - overscan);
    const startIndex = startRow * columns;

    slots.forEach((slot, i) => {
      const idx = startIndex + i;
      if (idx >= items.length) {
        slot.node.style.display = "none";
        slot.node.classList.remove("active");
        slot.hash = null;
        return;
      }
      const item = items[idx];
      slot.node.style.display = "";
      const row = Math.floor(idx / columns);
      const col = idx % columns;
      slot.node.style.transform = `translate(${col * (tileWidth + gap)}px, ${row * (tileHeight + gap)}px)`;
      if (slot.hash !== item.content_hash) {
        renderTile(slot.node, item);
        slot.hash = item.content_hash;
      }
      slot.node.classList.toggle("active", idx === activeIndex);
    });
  }

  function scheduleRender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(render);
  }

  function scrollToIndex(idx) {
    if (idx < 0 || idx >= items.length) return;
    const row = Math.floor(idx / columns);
    const rowTop = row * (tileHeight + gap);
    const rowBottom = rowTop + tileHeight;
    const viewTop = viewport.scrollTop;
    const viewBottom = viewTop + viewport.clientHeight;
    if (rowTop < viewTop) {
      viewport.scrollTop = rowTop;
    } else if (rowBottom > viewBottom) {
      viewport.scrollTop = rowBottom - viewport.clientHeight;
    }
  }

  viewport.addEventListener("scroll", scheduleRender);
  window.addEventListener("resize", () => {
    computeColumns();
    ensurePool();
    scheduleRender();
  });

  return {
    setItems(newItems) {
      items = newItems;
      activeIndex = -1;
      viewport.scrollTop = 0;
      computeColumns();
      ensurePool();
      render();
    },
    setActive(idx) {
      if (idx < 0 || idx >= items.length) return;
      activeIndex = idx;
      scrollToIndex(idx);
      scheduleRender();
    },
    getItem(idx) {
      return items[idx];
    },
    // Forces every currently visible tile to be rebuilt from its item data
    // on the next render, bypassing the hash-equality memoisation above.
    // Used after a decision changes a group's `decision` field in place —
    // the content_hash the memo key is keyed on hasn't changed, so without
    // this the tile's decision badge would silently go stale until the
    // user scrolled it out of view and back.
    refresh() {
      slots.forEach((slot) => { slot.hash = null; });
      render();
    },
    get activeIndex() {
      return activeIndex;
    },
    get columns() {
      return columns;
    },
    get itemCount() {
      return items.length;
    },
    get domNodeCount() {
      return slots.length;
    },
  };
}

const IMAGE_RE = /\.(jpg|jpeg|png|gif|bmp|webp|heic|heif)$/i;
const VIDEO_RE = /\.(mp4|mov|avi|mkv|webm|m4v|3gp)$/i;

function recordName(record) {
  return record.is_archive_member ? record.member_name : record.real_path;
}

// The record a tile's thumbnail (or fallback icon) is based on: the copy
// Р8 would keep when one exists, because that is the copy most worth
// looking at, and otherwise the first plain-file image in the group —
// archive members never have cached previews (thumbnails.py skips them
// to avoid reopening the archive, finding A2's whole point).
function representativeRecord(group) {
  const keeper = group.records.find((r) => r.display_path === group.keeper_display_path);
  const candidates = keeper ? [keeper, ...group.records] : group.records;
  return candidates.find((r) => !r.is_archive_member && IMAGE_RE.test(recordName(r))) || keeper || group.records[0];
}

function tileKind(group) {
  const rec = representativeRecord(group);
  const name = recordName(rec) || "";
  if (IMAGE_RE.test(name)) return { kind: "image", label: "изображение" };
  if (VIDEO_RE.test(name)) return { kind: "video", label: "видео" };
  return { kind: "file", label: "файл" };
}

function fileExtension(record) {
  const name = recordName(record) || "";
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toUpperCase() : "—";
}

function showFallbackThumb(thumbEl, group) {
  const { kind, label } = tileKind(group);
  thumbEl.className = "tile-thumb no-preview kind-" + kind;
  thumbEl.innerHTML = "";
  const ext = document.createElement("div");
  ext.className = "tile-ext";
  ext.textContent = fileExtension(representativeRecord(group));
  const sub = document.createElement("div");
  sub.className = "tile-kind";
  sub.textContent = label;
  thumbEl.appendChild(ext);
  thumbEl.appendChild(sub);
}

// Icon for the keeper's folder classification (Р8's NAMED/DATED/GENERIC),
// server-computed (`group.keeper_reason_kind`) rather than re-classified
// here — see keeper.py's `keeper_reason` docstring for why re-deriving
// this in the browser is exactly the mistake Р8 already fixed once.
const KIND_ICON = { named: "📁 ", dated: "🗓 ", generic: "" };

// Builds one tile's DOM structure once; renderGridTile() below only ever
// rewrites its content, never recreates it, since it is reused from the
// pool.
function buildTileSkeleton() {
  const thumb = document.createElement("div");
  thumb.className = "tile-thumb";
  const countBadge = document.createElement("div");
  countBadge.className = "tile-count-badge";
  const decisionBadge = document.createElement("div");
  decisionBadge.className = "tile-decision-badge";
  decisionBadge.hidden = true;
  const info = document.createElement("div");
  info.className = "tile-info";
  const sizeEl = document.createElement("div");
  sizeEl.className = "tile-size";
  const keeperEl = document.createElement("div");
  keeperEl.className = "tile-keeper";
  info.appendChild(sizeEl);
  info.appendChild(keeperEl);
  return { thumb, countBadge, decisionBadge, info, sizeEl, keeperEl };
}

let gridState = null; // { withPreview: bool }

function renderGridTile(node, group) {
  node.dataset.hash = group.content_hash;
  node.setAttribute("aria-label", `${group.records.length} копии, ${fmtBytes(group.size)}`);

  let refs = node._refs;
  if (!refs) {
    refs = buildTileSkeleton();
    node.appendChild(refs.thumb);
    node.appendChild(refs.countBadge);
    node.appendChild(refs.decisionBadge);
    node.appendChild(refs.info);
    node._refs = refs;
    node.addEventListener("click", () => selectGroupByHash(node.dataset.hash));
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectGroupByHash(node.dataset.hash);
      }
    });
  }

  refs.countBadge.textContent = `×${group.records.length}`;

  const withPreview = gridState && gridState.withPreview;
  const rec = representativeRecord(group);
  const isImage = withPreview && rec && !rec.is_archive_member && IMAGE_RE.test(recordName(rec));

  if (isImage) {
    refs.thumb.className = "tile-thumb";
    refs.thumb.innerHTML = "";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = `/api/thumbnail?path=${encodeURIComponent(rec.real_path)}&hash=${encodeURIComponent(group.content_hash)}`;
    img.addEventListener("error", () => showFallbackThumb(refs.thumb, group), { once: true });
    refs.thumb.appendChild(img);
  } else {
    showFallbackThumb(refs.thumb, group);
  }

  refs.sizeEl.textContent = `${fmtBytes(group.size)} · освободится ${fmtBytes(group.wasted_bytes)}`;
  const kindIcon = KIND_ICON[group.keeper_reason_kind] || "";
  refs.keeperEl.textContent = group.keeper_display_path
    ? kindIcon + shortenPath(group.keeper_display_path)
    : "—";
  refs.keeperEl.title = group.keeper_reason || group.keeper_display_path || "";

  const decision = group.decision;
  if (decision && decision.action === "quarantine") {
    refs.decisionBadge.hidden = false;
    refs.decisionBadge.textContent = decision.applied_at ? "✓ перемещено" : "✓ в очереди";
    refs.decisionBadge.className = "tile-decision-badge queued" + (decision.applied_at ? " applied" : "");
  } else if (decision && decision.action === "keep") {
    refs.decisionBadge.hidden = false;
    refs.decisionBadge.textContent = "оставить обе";
    refs.decisionBadge.className = "tile-decision-badge kept";
  } else {
    refs.decisionBadge.hidden = true;
  }

  node.classList.toggle("has-quality-flag", Boolean(group.quality));
}

function shortenPath(path) {
  // Compact display for the tile face — the full path is always in the
  // detail panel and in the title attribute above.
  const parts = path.replace(/\\/g, "/").split("/");
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

let grid = null;
let groupsByHash = new Map();

function initGrid() {
  if (grid) return grid;
  grid = createVirtualGrid({
    viewport: $("grid-viewport"),
    sizer: $("grid-sizer"),
    pool: $("grid-pool"),
    tileWidth: 168,
    tileHeight: 196,
    gap: 9,
    overscan: 3,
    renderTile: renderGridTile,
  });
  return grid;
}

function bucketGroups(groups) {
  const plain = [], media = [], archive = [];
  groups.forEach((g) => {
    if (g.only_archive_members) archive.push(g);
    else if (g.is_media) media.push(g);
    else plain.push(g);
  });
  return { plain, media, archive };
}

let buckets = { plain: [], media: [], archive: [] };
let activeTab = "plain";

// Clicking a tile directly (mouse path) should land on the same "active
// index" state keyboard navigation uses, so a mouse click and an arrow
// key never disagree about which group Q/K/1-9 would act on next.
function selectGroupByHash(hash) {
  const items = buckets[activeTab] || [];
  const idx = items.findIndex((g) => g.content_hash === hash);
  if (idx < 0) return;
  initGrid().setActive(idx);
  openActiveDetail();
}

function setActiveTab(tab) {
  activeTab = tab;
  document.querySelectorAll(".type-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  hideGroupDetail();

  if (tab === "archive") {
    $("grid-viewport").hidden = true;
    $("grid-empty").hidden = true;
    $("archive-panel").hidden = false;
    renderArchiveCards();
    renderArchiveDuplicateGroups(buckets.archive);
    return;
  }

  $("archive-panel").hidden = true;
  const items = buckets[tab];
  $("grid-empty").hidden = items.length > 0;
  $("grid-viewport").hidden = items.length === 0;
  const g = initGrid();
  g.setItems(items);
  if (items.length > 0) {
    g.setActive(0);
    openActiveDetail();
  }
}

// --- archive verdict cards (screen 5) ---------------------------------
//
// A duplicate *group* that happens to live only inside archives (the
// `only_archive_members` bucket task 11 already built) is a different,
// narrower thing than an archive's Р1 verdict: the former is "these bytes
// have a twin, and both copies are stuck in archives"; the latter is "is
// this whole archive file safe to quarantine". Screen 5 is about the
// latter, and it needs its own card per *archive*, not per group — a
// partially-redundant Google Takeout with 6286 members would otherwise be
// invisible (its members mostly don't have archive-side twins, so they
// barely touch the group-based bucket at all; see pilot-findings.md).

const ARCHIVE_VERDICT_LABEL = {
  fully_redundant: { text: "полностью избыточен", cls: "ok", icon: "✓" },
  partially_redundant: { text: "частично избыточен", cls: "warn", icon: "◐" },
  unique: { text: "уникален", cls: "neutral", icon: "—" },
  unread: { text: "не прочитан", cls: "danger", icon: "✕" },
};

function renderArchiveCards() {
  const container = $("archive-cards");
  container.innerHTML = "";
  const verdicts = (currentReport && currentReport.archive_verdicts) || [];
  $("archive-empty").hidden = verdicts.length > 0;

  verdicts.forEach((verdict) => {
    container.appendChild(buildArchiveCard(verdict));
  });
}

function buildArchiveCard(verdict) {
  const label = ARCHIVE_VERDICT_LABEL[verdict.verdict] || ARCHIVE_VERDICT_LABEL.unique;
  const card = el("div", "archive-card");
  if (currentReport && currentReport.mode !== "full") card.classList.add("archive-card-quick");

  card.appendChild(el("div", "ac-icon", "📦"));

  const body = el("div", "ac-body");
  body.appendChild(el("div", `ac-verdict ${label.cls}`, `${label.icon} ${label.text}`));
  body.appendChild(el("div", "ac-path", verdict.path));

  if (verdict.verdict === "fully_redundant") {
    body.appendChild(el(
      "div", "ac-detail",
      `${fmtNumber(verdict.members_total)} файлов, ${fmtBytes(verdict.size)} — все уже есть на диске обычными файлами.`
    ));
  } else if (verdict.verdict === "partially_redundant") {
    const share = verdict.members_total ? (100 * verdict.members_redundant / verdict.members_total) : 0;
    const barOuter = el("div", "ac-bar-outer");
    const barInner = el("div", "ac-bar-inner");
    barInner.style.width = `${share.toFixed(1)}%`;
    barOuter.appendChild(barInner);
    body.appendChild(barOuter);
    body.appendChild(el(
      "div", "ac-detail",
      `${fmtNumber(verdict.members_redundant)} из ${fmtNumber(verdict.members_total)} участников ` +
      `(${share.toFixed(1)}%, ${fmtBytes(verdict.redundant_bytes)}) уже лежат на диске обычными файлами.`
    ));
  } else if (verdict.verdict === "unique") {
    body.appendChild(el("div", "ac-detail", "Ни одного участника с двойником на диске."));
  } else {
    body.appendChild(el(
      "div", "ac-detail",
      `Причина: ${verdict.reason || "неизвестна"}. Неизвестные байты избыточными не бывают.`
    ));
  }

  const actionWrap = el("div", "ac-action");
  if (verdict.is_actionable) {
    const btn = el("button", "btn primary sm", "В карантин целиком");
    const status = el("div", "ac-status");
    btn.addEventListener("click", () => runArchiveQuarantine(verdict.path, btn, status));
    actionWrap.appendChild(btn);
    actionWrap.appendChild(el(
      "span", "hint",
      "двойники будут перечитаны и сверены заново в момент нажатия, не по данным скана"
    ));
    actionWrap.appendChild(status);
  } else if (verdict.verdict === "partially_redundant") {
    const btn = el("button", "btn sm", "В карантин целиком");
    btn.disabled = true;
    actionWrap.appendChild(btn);
    actionWrap.appendChild(el("span", "hint", "недоступно — в архиве остаётся уникальное содержимое"));
  }

  card.appendChild(body);
  card.appendChild(actionWrap);
  return card;
}

async function runArchiveQuarantine(archivePath, button, statusEl) {
  const quarantineDir = $("quarantine-dir").value.trim();
  if (!quarantineDir) {
    statusEl.className = "ac-status bad";
    statusEl.textContent = "Укажите папку карантина (ниже, в разделе «Карантин»).";
    return;
  }
  button.disabled = true;
  statusEl.className = "ac-status pending";
  statusEl.textContent = "Перечитываю и сверяю двойники...";

  try {
    const resp = await fetch(`/api/scan/${currentScanId}/archive-quarantine`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        quarantine_dir: quarantineDir,
        archive_path: archivePath,
        confirm_media: $("confirm-media-apply").checked,
      }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      statusEl.className = "ac-status bad";
      statusEl.textContent = `Ошибка: ${JSON.stringify(result)}`;
      button.disabled = false;
      return;
    }
    if (result.moved && result.moved.length) {
      statusEl.className = "ac-status ok";
      statusEl.textContent = `Перемещён (${fmtBytes(result.freed_bytes)} свободно).`;
      button.textContent = "Перемещён";
    } else if (result.pending_media_review && result.pending_media_review.length) {
      statusEl.className = "ac-status pending";
      statusEl.textContent = "В архиве есть медиа — включите подтверждение медиа ниже и повторите.";
      button.disabled = false;
    } else if (result.refused && result.refused.length) {
      statusEl.className = "ac-status bad";
      statusEl.textContent = result.refused[0].reason;
      button.disabled = false;
    } else if (result.failed && result.failed.length) {
      statusEl.className = "ac-status bad";
      statusEl.textContent = result.failed[0].error;
      button.disabled = false;
    } else {
      statusEl.className = "ac-status bad";
      statusEl.textContent = "Ничего не перемещено.";
      button.disabled = false;
    }
  } catch (e) {
    statusEl.className = "ac-status bad";
    statusEl.textContent = "Ошибка сети.";
    button.disabled = false;
  }
}

// The narrower, group-level "both copies are stuck in an archive" case —
// task 11's original `archive` bucket. Kept as a short read-only list
// under the verdict cards rather than dropped: real data has only 2 such
// groups (pilot-findings.md), so a full tile treatment would be overkill,
// but the information still belongs somewhere on this tab.
function renderArchiveDuplicateGroups(groups) {
  const container = $("archive-duplicate-groups") || (() => {
    const div = document.createElement("div");
    div.id = "archive-duplicate-groups";
    $("archive-panel").appendChild(div);
    return div;
  })();
  container.innerHTML = "";
  if (!groups.length) return;
  container.appendChild(el(
    "p", "hint",
    `Точные дубли, у которых все копии лежат внутри архивов (файл нельзя вынуть, ` +
    `не переписав архив): ${fmtNumber(groups.length)}.`
  ));
  const list = el("ul", "detail-records");
  groups.slice(0, 200).forEach((g) => {
    g.records.forEach((r) => list.appendChild(el("li", "detail-record", r.display_path)));
  });
  container.appendChild(list);
}

// --- group detail panel + review decisions (task 12) --------------------

function showGroupDetail(hash) {
  const group = groupsByHash.get(hash);
  if (!group) return;
  currentDetailHash = hash;
  const panel = $("group-detail");
  panel.hidden = false;

  const keeperPath = group.keeper_display_path;
  $("detail-title").textContent =
    `${group.records.length} копии, по ${fmtBytes(group.size)} — освободится ${fmtBytes(group.wasted_bytes)}`;

  const list = $("detail-records");
  list.innerHTML = "";
  group.records.forEach((r, i) => {
    const li = document.createElement("li");
    const isKeeper = r.display_path === keeperPath;
    li.className = "detail-record" + (isKeeper ? " keeper-badge" : "");
    const archiveNote = r.is_archive_member ? " (внутри архива)" : "";
    const overrideHint = !r.is_archive_member && i < 9 ? ` [клавиша ${i + 1}]` : "";
    li.textContent = r.display_path + archiveNote + (isKeeper ? " — оставить" : "") + overrideHint;
    if (!r.is_archive_member) {
      li.classList.add("selectable");
      li.title = `Оставить эту копию, остальные — в карантин (клавиша ${i + 1})`;
      li.addEventListener("click", () => quickDecide("quarantine", r.display_path));
    }
    list.appendChild(li);
  });

  const reasonEl = $("detail-reason");
  if (group.keeper_reason) {
    reasonEl.hidden = false;
    reasonEl.textContent = group.keeper_reason;
  } else {
    reasonEl.hidden = true;
    reasonEl.textContent = "";
  }

  const qualityEl = $("detail-quality");
  if (group.quality) {
    const q = group.quality;
    const bits = [];
    if (q.source_width && q.source_height) bits.push(`${q.source_width}×${q.source_height}` + (q.megapixels ? ` (${q.megapixels} МП)` : ""));
    if (q.sharpness != null) bits.push(`резкость: ${q.sharpness}`);
    if (q.jpeg_quality != null) bits.push(`качество сжатия: ~${q.jpeg_quality}`);
    if (q.recompression != null) bits.push(`признак пережатия: ${q.recompression} (${q.recompression_basis})`);
    qualityEl.hidden = bits.length === 0;
    qualityEl.textContent = "Метрики качества: " + bits.join(" · ");
  } else {
    qualityEl.hidden = true;
    qualityEl.textContent = "";
  }

  renderDecisionStatus(group);
  wireVerify(group);
  panel.scrollIntoView({ block: "nearest" });
}

function hideGroupDetail() {
  $("group-detail").hidden = true;
  currentDetailHash = null;
}

function renderDecisionStatus(group) {
  const status = $("decision-status");
  const undoBtn = $("decide-undo-btn");
  const d = group.decision;

  $("decide-quarantine-btn").classList.toggle("active", Boolean(d && d.action === "quarantine"));
  $("decide-keep-btn").classList.toggle("active", Boolean(d && d.action === "keep"));

  if (!d) {
    status.textContent = "Ещё не просмотрено.";
    undoBtn.hidden = true;
  } else if (d.applied_at) {
    status.textContent = "Уже перемещено в карантин этим прогоном.";
    undoBtn.hidden = true;
  } else if (d.action === "quarantine") {
    status.textContent = d.keeper_path
      ? `В очереди на карантин — остаётся ${shortenPath(d.keeper_path)} (выбрано вручную).`
      : "В очереди на карантин — остаётся копия по умолчанию (см. причину выше).";
    undoBtn.hidden = false;
  } else {
    status.textContent = "Решено: обе копии остаются.";
    undoBtn.hidden = false;
  }
}

async function sendDecision(group, action, keeperPath) {
  try {
    const resp = await fetch(
      `/api/scan/${currentScanId}/group/${encodeURIComponent(group.content_hash)}/decision`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, keeper_path: keeperPath || null }),
      }
    );
    if (!resp.ok) {
      $("decision-status").textContent = `Не удалось сохранить решение: ${await resp.text()}`;
      return false;
    }
    const saved = await resp.json();
    group.decision = { action: saved.action, keeper_path: saved.keeper_path, applied_at: null };
    return true;
  } catch {
    $("decision-status").textContent = "Ошибка сети при сохранении решения.";
    return false;
  }
}

// One place both the detail-panel buttons and the keyboard shortcuts call
// into, so a mouse click and a keystroke behave identically — including
// the "rifle through" auto-advance to the next group, which is the whole
// point of a one-second-per-group workflow.
async function quickDecide(action, keeperPath) {
  if (!currentDetailHash) return;
  const group = groupsByHash.get(currentDetailHash);
  if (!group || group.only_archive_members) return;
  const ok = await sendDecision(group, action, keeperPath);
  if (!ok) return;
  renderDecisionStatus(group);
  initGrid().refresh();
  refreshQueueSummary();
  advanceAfterDecision();
}

async function undoDecide() {
  if (!currentDetailHash) return;
  const group = groupsByHash.get(currentDetailHash);
  if (!group) return;
  try {
    await fetch(
      `/api/scan/${currentScanId}/group/${encodeURIComponent(currentDetailHash)}/decision`,
      { method: "DELETE" }
    );
  } catch {
    return;
  }
  group.decision = null;
  renderDecisionStatus(group);
  initGrid().refresh();
  refreshQueueSummary();
}

function advanceAfterDecision() {
  const g = initGrid();
  const idx = g.activeIndex;
  if (idx < 0 || idx >= g.itemCount - 1) return; // last group: stay put, nothing to rifle to
  g.setActive(idx + 1);
  openActiveDetail();
}

function openActiveDetail() {
  const g = initGrid();
  const idx = g.activeIndex;
  if (idx < 0) {
    hideGroupDetail();
    return;
  }
  const item = g.getItem(idx);
  if (item) showGroupDetail(item.content_hash);
}

function refreshQueueSummary() {
  if (!currentReport) return;
  let plainCount = 0, mediaCount = 0, bytes = 0;
  currentReport.groups.forEach((g) => {
    if (g.decision && g.decision.action === "quarantine" && !g.decision.applied_at) {
      if (g.is_media) mediaCount++; else plainCount++;
      bytes += g.wasted_bytes;
    }
  });
  const total = plainCount + mediaCount;
  $("queue-summary").textContent = total
    ? `В очереди: ${fmtNumber(total)} групп (${fmtNumber(plainCount)} обычных, ${fmtNumber(mediaCount)} медиа) — освободится ${fmtBytes(bytes)}.`
    : "В очереди: 0 групп — просмотренные и отмеченные группы появятся здесь.";
}

// --- keyboard navigation (task 12) ---------------------------------------
//
// One global listener rather than one per tile: tiles are recycled pool
// nodes (see createVirtualGrid), so "the focused element" cannot be the
// thing that carries navigation state. `activeIndex` on the grid is.
function isTypingTarget(target) {
  return Boolean(target) && (target.tagName === "INPUT" || target.tagName === "TEXTAREA");
}

document.addEventListener("keydown", (e) => {
  if (isTypingTarget(e.target)) return;
  if (!currentReport || $("results").hidden) return;
  if (activeTab === "archive") return; // no per-group grid on this tab

  const g = initGrid();
  const count = g.itemCount;
  if (count === 0) return;

  const idx = g.activeIndex;
  const cols = g.columns || 1;

  const moveTo = (newIdx) => {
    g.setActive(Math.max(0, Math.min(count - 1, newIdx)));
    openActiveDetail();
  };

  switch (e.key) {
    case "ArrowDown":
      e.preventDefault();
      moveTo(idx < 0 ? 0 : idx + cols);
      return;
    case "ArrowUp":
      e.preventDefault();
      moveTo(idx < 0 ? 0 : idx - cols);
      return;
    case "ArrowRight":
      e.preventDefault();
      moveTo(idx < 0 ? 0 : idx + 1);
      return;
    case "ArrowLeft":
      e.preventDefault();
      moveTo(idx < 0 ? 0 : idx - 1);
      return;
    case "Enter":
    case " ":
      e.preventDefault();
      moveTo(idx < 0 ? 0 : idx);
      return;
    default:
      break;
  }

  if (idx < 0) return; // decision keys need a group to act on

  if (e.key === "q" || e.key === "Q") {
    e.preventDefault();
    quickDecide("quarantine", null);
    return;
  }
  if (e.key === "k" || e.key === "K") {
    e.preventDefault();
    quickDecide("keep", null);
    return;
  }
  if (e.key === "u" || e.key === "U") {
    e.preventDefault();
    undoDecide();
    return;
  }
  if (/^[1-9]$/.test(e.key)) {
    e.preventDefault();
    const item = g.getItem(idx);
    const record = item && item.records[Number(e.key) - 1];
    if (record && !record.is_archive_member) {
      quickDecide("quarantine", record.display_path);
    }
  }
});

$("decide-quarantine-btn").addEventListener("click", () => quickDecide("quarantine", null));
$("decide-keep-btn").addEventListener("click", () => quickDecide("keep", null));
$("decide-undo-btn").addEventListener("click", () => undoDecide());
$("detail-close-btn").addEventListener("click", hideGroupDetail);

// "Сверить полностью" for one group — unchanged from before task 11, just
// re-homed into the detail panel. Not a promotion from a weaker kind of
// match (there is no weaker kind, in either mode) but a re-read against
// the disk as it is now: a report on thousands of groups is reviewed over
// hours, during which a copy can be edited, truncated by a failed sync, or
// replaced by a different file of the same size.
function wireVerify(group) {
  const button = $("detail-verify-btn");
  const out = $("detail-verify-result");
  out.className = "verify-result";
  out.textContent = "";
  button.disabled = false;

  button.onclick = async () => {
    if (!currentScanId) return;
    button.disabled = true;
    out.className = "verify-result pending";
    out.textContent = "Перечитываю копии...";
    try {
      const resp = await fetch(
        `/api/scan/${currentScanId}/group/${encodeURIComponent(group.content_hash)}/verify`,
        { method: "POST" }
      );
      if (!resp.ok) {
        out.className = "verify-result bad";
        out.textContent = `Ошибка: ${await resp.text()}`;
        return;
      }
      const result = await resp.json();
      if (result.ok) {
        out.className = "verify-result ok";
        out.textContent =
          `Подтверждено: ${result.confirmed} из ${result.checked.length} копий ` +
          `совпадают байт-в-байт прямо сейчас.`;
      } else {
        out.className = "verify-result bad";
        const bad = result.checked.filter((c) => !c.ok);
        out.textContent =
          `Не подтверждено (живых одинаковых копий ${result.confirmed}): ` +
          bad.map((c) => `${c.display_path} — ${c.reason}`).join("; ");
      }
    } finally {
      button.disabled = false;
    }
  };
}

async function loadResult() {
  const resp = await fetch(`/api/scan/${currentScanId}/result`);
  if (!resp.ok) return;
  currentReport = await resp.json();
  renderReport(currentReport);
}

// A quick run's results are complete about what it looked at and silent
// about what it didn't — which is exactly the shape of misreading finding
// A1 describes. This banner says what was skipped, in archives and bytes,
// and offers the way to finish the job.
function renderModeNote(report) {
  const note = $("mode-note");
  if (report.mode !== "quick") {
    note.hidden = true;
    return;
  }
  note.hidden = false;
  const count = report.skipped_by_mode_count ?? 0;
  $("mode-note-text").textContent = count
    ? `Быстрый режим: ${fmtNumber(count)} арх. на ${fmtBytes(report.skipped_by_mode_bytes ?? 0)} ` +
      `не проверено — внутрь не заглядывали, и дубликаты внутри них здесь не показаны. ` +
      `Превью тоже не строились — ниже показаны иконки вместо снимков.`
    : `Быстрый режим: архивов не встретилось, но превью не строились — ниже показаны иконки вместо снимков.`;
  $("upgrade-status").textContent = "";
  $("upgrade-btn").disabled = false;
}

function renderReport(report) {
  $("results").hidden = false;
  $("quarantine-card").hidden = false;
  $("journal-card").hidden = false;
  $("summary").textContent =
    `Групп дублей: ${fmtNumber(report.groups.length)}. ` +
    `Потенциально можно освободить: ${fmtBytes(report.total_wasted_bytes)}.`;
  renderModeNote(report);

  // Screen 4 (docs/UX-MOCKUPS.html): a quick-mode report never generated
  // previews (task 7), so the grid must not even try to fetch thumbnails —
  // `/api/thumbnail` would decode every requested file on the spot, one
  // synchronous image decode per tile scrolled into view, which is exactly
  // the "6000 grey rectangles, slowly" failure this task exists to avoid.
  gridState = { withPreview: report.mode === "full" };

  groupsByHash = new Map(report.groups.map((g) => [g.content_hash, g]));
  buckets = bucketGroups(report.groups);

  const archiveVerdictCount = (report.archive_verdicts || []).length;
  const nonEmpty = {
    plain: buckets.plain.length > 0,
    media: buckets.media.length > 0,
    archive: archiveVerdictCount > 0 || buckets.archive.length > 0,
  };

  document.querySelectorAll(".type-tab").forEach((btn) => {
    const tab = btn.dataset.tab;
    const count = tab === "archive" ? archiveVerdictCount : buckets[tab].length;
    btn.querySelector(".cnt").textContent = fmtNumber(count);
  });

  // "Обычные файлы" is the natural first tab, but a real personal photo
  // library (the one this was tested against: D:\Photos plus a Google
  // Photos takeout) can be almost entirely media -- 8812 of 8814 groups on
  // that report, 0 in "plain". Landing on an empty tab reads as "no
  // duplicates found", which is the opposite of true, so the default
  // follows the data: the first tab in the usual order that actually has
  // something in it.
  const tabOrder = ["plain", "media", "archive"];
  const defaultTab = tabOrder.find((t) => nonEmpty[t]) || "plain";
  setActiveTab(defaultTab);
  refreshQueueSummary();
}

document.querySelectorAll(".type-tab").forEach((btn) => {
  btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
});

// "Досчитать полностью": start a full-mode run over the same roots and the
// same index. Nothing the quick run already hashed is read again — the
// index answers for it (Р6) — so this costs the archive contents and the
// previews, which is precisely what the quick run skipped.
$("upgrade-btn").addEventListener("click", async () => {
  if (!currentScanId) return;
  const status = $("upgrade-status");
  $("upgrade-btn").disabled = true;
  status.textContent = "Запускаю полный проход...";

  const resp = await fetch(`/api/scan/${currentScanId}/upgrade`, { method: "POST" });
  if (!resp.ok) {
    status.textContent = `Ошибка: ${await resp.text()}`;
    $("upgrade-btn").disabled = false;
    return;
  }

  const started = await resp.json();
  currentScanId = started.scan_id;
  currentReport = null;
  status.textContent = "";

  $("mode-note").hidden = true;
  $("progress-card").hidden = false;
  $("results").hidden = true;
  $("warnings-card").hidden = true;
  $("scan-error").hidden = true;
  $("scan-btn").disabled = true;
  $("cancel-btn").hidden = false;

  startPolling();
});

// --- batch apply (task 12, half two) -------------------------------------
//
// The only function in this file that causes a file to move. Everything
// above it — decisions, the queue summary, the keyboard shortcuts — only
// ever writes to `review_decisions` in the index. This is deliberate: the
// gap pilot finding P1.1 asked for is *between* "reviewed" and "moved",
// and collapsing it back into one step here would remove the gap the rest
// of this file exists to create.
$("apply-btn").addEventListener("click", async () => {
  if (!currentScanId) return;
  const dir = $("quarantine-dir").value.trim();
  if (!dir) {
    $("apply-status").textContent = "Укажите папку карантина.";
    return;
  }
  $("apply-btn").disabled = true;
  $("apply-status").textContent = "Применяю отмеченное...";
  $("apply-result").textContent = "";
  try {
    const resp = await fetch(`/api/scan/${currentScanId}/apply-decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quarantine_dir: dir, confirm_media: $("confirm-media-apply").checked }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      $("apply-status").textContent = `Ошибка: ${JSON.stringify(result)}`;
      return;
    }
    $("apply-status").textContent = "";
    const hasFailures = Boolean(result.failed && result.failed.length);
    $("apply-result").className = "verify-result " + (hasFailures ? "bad" : "ok");
    $("apply-result").textContent =
      `Применено групп: ${fmtNumber(result.applied)}. Перемещено файлов: ${fmtNumber((result.moved || []).length)}.` +
      (hasFailures ? ` Ошибок: ${fmtNumber(result.failed.length)}.` : "") +
      ((result.pending_media_review || []).length
        ? ` Отложено (нужно подтверждение медиа): ${fmtNumber(result.pending_media_review.length)}.`
        : "");
    // The applied groups' `applied_at` changed server-side; reloading is
    // the same "re-fetch the truth" the "Досчитать полностью" flow already
    // relies on, and far less error-prone than reconciling partial success
    // against `currentReport` by hand.
    await loadResult();
  } catch (e) {
    $("apply-status").textContent = "Ошибка сети при применении.";
  } finally {
    $("apply-btn").disabled = false;
  }
});

// --- journal + restore (task 12: "веб-обвязка поверх restore_from_journal") --

let journalEntries = [];

const JOURNAL_STATUS_LABEL = {
  moved: "в карантине",
  restored: "возвращён",
  failed: "ошибка перемещения",
  pending: "не завершено (сбой во время переноса)",
};

$("journal-refresh-btn").addEventListener("click", loadJournal);

async function loadJournal() {
  const dir = $("quarantine-dir").value.trim();
  if (!dir) {
    $("journal-status").textContent = "Укажите папку карантина.";
    return;
  }
  $("journal-status").textContent = "Загружаю журнал...";
  try {
    const resp = await fetch(`/api/quarantine/journal?quarantine_dir=${encodeURIComponent(dir)}`);
    const data = await resp.json();
    if (!resp.ok) {
      $("journal-status").textContent = `Ошибка: ${JSON.stringify(data)}`;
      return;
    }
    journalEntries = data.entries || [];
    renderJournalList();
    $("journal-status").textContent = journalEntries.length
      ? `Записей: ${fmtNumber(journalEntries.length)}.`
      : "Журнал пуст — карантин из этой папки ещё ничего не перемещал.";
  } catch {
    $("journal-status").textContent = "Не удалось прочитать журнал.";
  }
}

function renderJournalList() {
  const list = $("journal-list");
  list.innerHTML = "";
  journalEntries.forEach((entry) => {
    const li = document.createElement("li");
    li.className = "journal-entry status-" + entry.status;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.opId = entry.op_id;
    checkbox.disabled = entry.status !== "moved"; // only a completed move can be undone
    checkbox.addEventListener("change", updateRestoreButtonState);

    const label = document.createElement("span");
    label.className = "journal-path";
    label.textContent = (entry.kind === "archive" ? "📦 " : "") + (entry.original || "");

    const status = document.createElement("span");
    status.className = "journal-status-badge";
    status.textContent = JOURNAL_STATUS_LABEL[entry.status] || entry.status;
    if (entry.status === "failed" && entry.error) status.title = entry.error;

    li.appendChild(checkbox);
    li.appendChild(label);
    li.appendChild(status);
    list.appendChild(li);
  });
  updateRestoreButtonState();
}

function updateRestoreButtonState() {
  const anyChecked = $("journal-list").querySelectorAll("input[type=checkbox]:checked").length > 0;
  $("journal-restore-selected-btn").disabled = !anyChecked;
}

$("journal-restore-selected-btn").addEventListener("click", async () => {
  const dir = $("quarantine-dir").value.trim();
  const opIds = Array.from($("journal-list").querySelectorAll("input[type=checkbox]:checked")).map(
    (c) => c.dataset.opId
  );
  if (!dir || opIds.length === 0) return;
  $("journal-restore-selected-btn").disabled = true;
  $("journal-status").textContent = "Возвращаю...";
  try {
    const resp = await fetch("/api/quarantine/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quarantine_dir: dir, op_ids: opIds }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      $("journal-status").textContent = `Ошибка: ${JSON.stringify(result)}`;
      return;
    }
    $("journal-status").textContent =
      `Вернул: ${fmtNumber((result.restored || []).length)}. ` +
      `Пропустил: ${fmtNumber((result.skipped || []).length)}.`;
    await loadJournal();
  } catch {
    $("journal-status").textContent = "Ошибка сети при восстановлении.";
  }
});

refreshIndexStats();

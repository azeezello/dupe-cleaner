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
  document.querySelectorAll("#results .type-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  hideGroupDetail();
  hideSimilarDetail();
  $("similar-panel").hidden = tab !== "similar";

  if (tab === "similar") {
    $("grid-viewport").hidden = true;
    $("grid-empty").hidden = true;
    $("archive-panel").hidden = true;
    openSimilarTab();
    return;
  }

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
  // Экран разбора альбома (задача 24) делит клавиатуру с этим экраном, и
  // 1-9 там значат другое. Арбитр — последнее место, куда ткнул человек;
  // см. `reviewIsEngaged`.
  if (typeof reviewIsEngaged === "function" && reviewIsEngaged()) return;
  if (!currentReport || $("results").hidden) return;
  if (activeTab === "archive") return; // no per-group grid on this tab
  if (activeTab === "similar") {
    // Своя навигация и никаких клавиш решения: у группы похожих решения
    // не существует (Р2), поэтому Q/K/U/1-9 сюда не доходят.
    handleSimilarKey(e);
    return;
  }

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

  document.querySelectorAll("#results .type-tab").forEach((btn) => {
    const tab = btn.dataset.tab;
    // "Похожие" не живут в отчёте: это запрос над отпечатками в индексе,
    // со своим порогом, и до первой загрузки у вкладки нет числа — там
    // стоит прочерк, а не ноль, который читался бы как "не нашлось".
    if (tab === "similar") return;
    const count = tab === "archive" ? archiveVerdictCount : buckets[tab].length;
    btn.querySelector(".cnt").textContent = fmtNumber(count);
  });
  resetSimilarTab(report);

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

// Селектор сужен до #results намеренно: экран разбора альбома (задача 24)
// переиспользует тот же класс `.type-tab` для своих двух вкладок, и
// неспецифичный селектор вешал на них обработчик отчёта. Он вызывался с
// `dataset.tab === undefined` и падал внутри `setActiveTab` на
// `buckets[undefined]`. Поймано jsdom-прогоном поверх настоящего сервера,
// а не чтением: в консоли это выглядело как одна строка "Uncaught
// TypeError" без следа, ведущего к вкладкам.
document.querySelectorAll("#results .type-tab").forEach((btn) => {
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

// --- похожие снимки: четвёртая вкладка (задача 14) ------------------------
//
// Отдельная сетка, отдельная панель деталей и ни одной кнопки действия.
// Переиспользовать плитку дублей было бы короче по коду и неверно по сути:
// у группы похожих нет ни `keeper`, ни `wasted_bytes`, ни решения, потому
// что ей нечем их обосновать (Р0, ось B; Р2). Плитка, у которой эти поля
// просто пустые, читается как «ещё не посчитано», а не как «такого вопроса
// здесь не существует» — поэтому у похожих своя плитка, пунктирная, с
// бейджем «≈N» вместо «×N».
//
// Группы приходят с сервера на каждое движение ручки порога
// (/api/scan/{id}/similar?max_distance=N): кластеризация — это запрос над
// уже посчитанными отпечатками, а не часть отчёта. Поэтому вкладка грузится
// по требованию, а не вместе с отчётом, и её счётчик до первой загрузки —
// прочерк, а не ноль.

let similarGrid = null;
let similarState = {
  loaded: false,
  loading: false,
  threshold: 6,
  filter: "all",
  data: null,
  items: [],
  detailId: null,
  modeIsFull: false,
};

function initSimilarGrid() {
  if (similarGrid) return similarGrid;
  similarGrid = createVirtualGrid({
    viewport: $("similar-viewport"),
    sizer: $("similar-sizer"),
    pool: $("similar-pool"),
    tileWidth: 168,
    tileHeight: 210,
    gap: 9,
    overscan: 3,
    renderTile: renderSimilarTile,
  });
  return similarGrid;
}

function similarTabCount() {
  return document.querySelector('#results .type-tab[data-tab="similar"] .cnt');
}

// Вызывается при каждом новом отчёте: вкладка обнуляется, но не грузится —
// кластеризация стоит реального времени на сервере, и платить за неё должен
// тот, кто на вкладку зашёл.
function resetSimilarTab(report) {
  similarState = {
    loaded: false,
    loading: false,
    threshold: similarState.threshold || 6,
    filter: similarState.filter || "all",
    data: null,
    items: [],
    detailId: null,
    modeIsFull: report.mode === "full",
  };
  similarTabCount().textContent = "—";
  $("similar-threshold").value = String(similarState.threshold);
  $("similar-threshold-value").textContent = String(similarState.threshold);
  hideSimilarDetail();
}

function openSimilarTab() {
  if (!similarState.modeIsFull) {
    // Р7: отпечатков в быстром режиме не существует. Пустая сетка здесь
    // читалась бы как «похожих нет» — ровно то недоразумение, про которое
    // находка A1.
    $("similar-viewport").hidden = true;
    $("similar-empty").hidden = false;
    $("similar-empty").textContent =
      "Перцептивные отпечатки считаются только в полном режиме — в быстром " +
      "сканировании их нет вовсе, поэтому похожие здесь не «не нашлись», а " +
      "не искались. Нажмите «Досчитать полностью» выше.";
    $("similar-summary").textContent = "";
    $("similar-threshold-note").textContent = "";
    return;
  }
  if (!similarState.loaded && !similarState.loading) {
    loadSimilar(similarState.threshold);
    return;
  }
  renderSimilar();
}

let similarRequestToken = 0;

async function loadSimilar(threshold) {
  if (!currentScanId) return;
  const token = ++similarRequestToken;
  similarState.loading = true;
  similarState.threshold = threshold;
  // Пересчёт идёт на сервере, и он дорожает с порогом быстрее, чем линейно:
  // на библиотеке в 6000 отпечатков порог 6 — треть секунды, порог 12 —
  // около восьми, порог 16 — две дюжины (замер в отчёте задачи 14; причина
  // в similar.py: с ростом порога блоки индекса кандидатов сужаются, а
  // корзины толстеют, и число пар-кандидатов растёт квадратично). Поэтому
  // ручка на время расчёта блокируется вместо того, чтобы копить запросы, а
  // ответ запроса, который обогнали, отбрасывается по токену.
  $("similar-threshold").disabled = true;
  $("similar-summary").textContent =
    threshold >= 12
      ? `Группирую отпечатки при пороге ${threshold} — на большой библиотеке это десятки секунд...`
      : "Группирую отпечатки...";
  $("similar-viewport").hidden = true;
  $("similar-empty").hidden = true;
  hideSimilarDetail();
  try {
    const resp = await fetch(
      `/api/scan/${currentScanId}/similar?max_distance=${encodeURIComponent(threshold)}`
    );
    if (token !== similarRequestToken) return; // обогнали следующим движением ручки
    if (!resp.ok) {
      $("similar-summary").textContent = `Не удалось посчитать похожие: ${await resp.text()}`;
      return;
    }
    similarState.data = await resp.json();
    similarState.loaded = true;
    // Сервер округляет нечётный порог вниз — ручка обязана показать то, что
    // реально применено, а не то, что было запрошено.
    const applied = similarState.data.threshold.max_distance;
    similarState.threshold = applied;
    $("similar-threshold").value = String(applied);
    $("similar-threshold-value").textContent = String(applied);
    renderSimilar();
  } catch {
    $("similar-summary").textContent = "Ошибка сети при запросе похожих.";
  } finally {
    if (token === similarRequestToken) {
      similarState.loading = false;
      $("similar-threshold").disabled = false;
    }
  }
}

function renderSimilarThresholdNote() {
  const t = similarState.data.threshold;
  const measured = t.measured[String(t.max_distance)];
  const parts = [t.even_only_note];
  if (t.max_distance >= 12) {
    // Замеры задачи 13: при 12 и выше группы перестают быть группами копий
    // (крупнейшая разрастается до 94 снимков при 16), а вето по форме кадра
    // начинает выбрасывать настоящие пары пачками. Это сказано рядом с
    // ручкой, а не только в отчёте, потому что ручку двигают здесь.
    parts.push(
      "Выше 12 группы перестают быть группами копий и становятся сценами, " +
      "а пересчёт на большой библиотеке занимает десятки секунд."
    );
  }
  if (measured) {
    parts.push(
      `Замер на ${t.measured_on}: при пороге ${t.max_distance} — ` +
      `${fmtNumber(measured.groups)} групп, крупнейшая ${measured.largest} снимков, ` +
      `из них собранных цепочкой ${fmtNumber(measured.chains)}.`
    );
  }
  $("similar-threshold-note").textContent = parts.join(" ");
}

function renderSimilar() {
  const data = similarState.data;
  if (!data) return;

  renderSimilarThresholdNote();

  const s = data.summary;
  const cov = data.coverage || {};
  $("similar-summary").textContent =
    `Групп похожих: ${fmtNumber(s.groups)} — из них копий ${fmtNumber(s.copy_groups)}, ` +
    `похожих сцен (цепочек) ${fmtNumber(s.chain_groups)}. ` +
    `Снимков в группах ${fmtNumber(s.contents_in_groups)}, файлов ${fmtNumber(s.files_in_groups)}, ` +
    `крупнейшая группа ${fmtNumber(s.largest_group)}. ` +
    `Отпечаток есть у ${fmtNumber(cov.with_phash || 0)} снимков из ${fmtNumber(cov.photos || 0)}` +
    (cov.without_phash ? `, без структуры (пустой кадр) ${fmtNumber(cov.without_phash)}` : "") +
    (cov.not_looked ? `, не смотрели ${fmtNumber(cov.not_looked)}` : "") + ". " +
    // Задача 17: размер дыры «метрик нет» и размер случая «подсказке
    // верить не стоит» — числами на этой библиотеке, а не обещанием.
    `Качество удалось сравнить в ${fmtNumber(s.ranked_groups || 0)} группах` +
    (s.contested_groups
      ? `, в ${fmtNumber(s.contested_groups)} из них признаки спорят между собой.`
      : ".");

  const warnings = data.warnings || [];
  $("similar-warnings").hidden = warnings.length === 0;
  $("similar-warnings").textContent = warnings.join(" ");

  similarTabCount().textContent = fmtNumber(s.groups);

  const filter = similarState.filter;
  similarState.items = (data.groups || []).filter(
    (g) => filter === "all" || g.kind === filter
  );

  const g = initSimilarGrid();
  $("similar-viewport").hidden = similarState.items.length === 0;
  $("similar-empty").hidden = similarState.items.length > 0;
  if (similarState.items.length === 0) {
    $("similar-empty").textContent = data.phash_available
      ? `При пороге ${similarState.threshold} таких групп нет. Подвиньте ручку вправо — ` +
        "но помните, что дальше 12 группы перестают быть группами копий."
      : "Ни у одного снимка этого скана нет перцептивного отпечатка.";
    hideSimilarDetail();
    return;
  }
  g.setItems(similarState.items);
  // setItems не сбрасывает memo-ключи пула: при смене порога у группы может
  // поменяться разброс и тип при том же первом хэше, и плитка показала бы
  // старое. refresh() заставляет перерисовать всё видимое.
  g.refresh();
  g.setActive(0);
  openActiveSimilarDetail();
}

function referenceMember(group) {
  return group.members.find((m) => m.is_reference) || group.members[0];
}

function renderSimilarTile(node, group) {
  node.dataset.sid = group.id;
  node.className = "tile similar-tile" + (group.kind === "scene" ? " scene" : "");
  node.setAttribute(
    "aria-label",
    `${group.size} похожих снимков, ${group.kind === "scene" ? "похожая сцена" : "копии"}`
  );

  let refs = node._srefs;
  if (!refs) {
    refs = {
      thumb: el("div", "tile-thumb"),
      badge: el("div", "tile-similar-badge"),
      ribbon: el("div", "tile-scene-ribbon"),
      info: el("div", "tile-info"),
      spread: el("div", "tile-spread"),
      labels: el("div", "tile-labels"),
      best: el("div", "tile-best"),
    };
    refs.info.appendChild(refs.spread);
    refs.info.appendChild(refs.labels);
    refs.info.appendChild(refs.best);
    node.appendChild(refs.thumb);
    node.appendChild(refs.badge);
    node.appendChild(refs.ribbon);
    node.appendChild(refs.info);
    node._srefs = refs;
    node.addEventListener("click", () => selectSimilarById(node.dataset.sid));
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectSimilarById(node.dataset.sid);
      }
    });
  }

  refs.badge.textContent = `≈${group.size}`;
  refs.ribbon.hidden = group.kind !== "scene";
  refs.ribbon.textContent = "сцена";

  const ref = referenceMember(group);
  refs.thumb.className = "tile-thumb";
  refs.thumb.innerHTML = "";
  const path = ref && ref.paths && ref.paths[0];
  if (path) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    // store=0: эти снимки в большинстве не попадали в группы дублей, поэтому
    // готовой миниатюры у них нет, и `/api/thumbnail` декодирует на месте.
    // Записывать результат в кэш превью нельзя — его 512 МБ (Р9) рассчитаны
    // на группы дублей, и просмотр похожих вытеснил бы именно их.
    img.src =
      `/api/thumbnail?path=${encodeURIComponent(path)}` +
      `&hash=${encodeURIComponent(ref.content_hash)}&store=0`;
    img.addEventListener(
      "error",
      () => {
        refs.thumb.className = "tile-thumb no-preview kind-image";
        refs.thumb.innerHTML = "";
        refs.thumb.appendChild(el("div", "tile-ext", "IMG"));
        refs.thumb.appendChild(el("div", "tile-kind", "превью не вышло"));
      },
      { once: true }
    );
    refs.thumb.appendChild(img);
  }

  refs.spread.textContent =
    group.spread == null
      ? "разброс не измерялся"
      : `разброс ${group.spread} бит${group.kind === "scene" ? " — цепочка" : ""}`;

  const labels = new Set();
  group.members.forEach((m) => m.labels.forEach((l) => labels.add(l)));
  refs.labels.textContent = labels.size ? Array.from(labels).join(" · ") : `${group.file_count} файлов`;

  // Задача 17 одной строкой: удалось ли вообще отранжировать копии и
  // спорят ли признаки. Строка намеренно не содержит самого вывода —
  // «выше разрешение» без второй копии рядом ничего не значит, а плитка
  // показывает только опорное превью. Вывод — в панели деталей.
  const best = group.best_copy;
  refs.best.className = "tile-best";
  if (!best || best.measured < 2) {
    refs.best.textContent = "качество не сравнить: метрик нет";
    refs.best.classList.add("muted");
  } else if (best.contested.length) {
    refs.best.textContent = "лучшая копия спорна";
    refs.best.classList.add("warn");
  } else if (!best.is_choice) {
    refs.best.textContent = "сравнение качества кадров";
    refs.best.classList.add("muted");
  } else {
    refs.best.textContent = best.confident
      ? "лучшая копия выбрана"
      : "лучшая копия выбрана (перевес малый)";
  }
}

function selectSimilarById(id) {
  const idx = similarState.items.findIndex((g) => g.id === id);
  if (idx < 0) return;
  initSimilarGrid().setActive(idx);
  openActiveSimilarDetail();
}

function openActiveSimilarDetail() {
  const g = initSimilarGrid();
  const item = g.getItem(g.activeIndex);
  if (item) showSimilarDetail(item);
  else hideSimilarDetail();
}

// Подписи оснований шкалы сжатия — те же три, что в best_copy.py.
const BASIS_LABELS = {
  jpeg_quant_tables: "таблицы квантования JPEG",
  bits_per_pixel: "бит на пиксель",
  lossless: "сжатие без потерь",
};

// Задача 17. Блок объясняет выбор так же, как `keeper.keeper_reason`
// объясняет Р8 на вкладке дублей: называется не победитель, а признак, на
// котором он впервые обошёл следующую копию. Разница в том, что здесь за
// объяснением не стоит никакого действия — ни кнопки, ни горячей клавиши.
function renderBestCopy(group) {
  const box = $("similar-detail-best");
  const best = group.best_copy;
  box.innerHTML = "";
  if (!best) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.className =
    "similar-best" +
    (best.contested.length ? " contested" : "") +
    (best.is_choice ? "" : " scene");

  box.appendChild(el("div", "similar-best-head", best.headline));
  if (best.reason) {
    box.appendChild(
      el(
        "div",
        "similar-best-reason",
        best.reason + (best.confident ? "" : " — перевес малый")
      )
    );
  }
  best.notes.forEach((note) => box.appendChild(el("p", "similar-best-note", note)));
}

function fmtGap(seconds) {
  if (seconds == null) return null;
  if (seconds < 1) return "в тот же миг";
  if (seconds < 90) return `${Math.round(seconds)} с спустя`;
  return fmtDuration(seconds) + " спустя";
}

function showSimilarDetail(group) {
  similarState.detailId = group.id;
  $("similar-detail").hidden = false;
  $("similar-detail-title").textContent =
    (group.kind === "scene" ? "Похожая сцена: " : "Похожие копии: ") +
    `${group.size} снимков, ${group.file_count} файлов`;

  const why = $("similar-detail-why");
  why.innerHTML = "";
  group.explanation.forEach((line) => why.appendChild(el("p", null, line)));

  renderBestCopy(group);

  const list = $("similar-detail-members");
  list.innerHTML = "";
  group.members.forEach((m) => {
    const li = el("li", "similar-member" + (m.is_reference ? " reference" : ""));

    const thumb = el("div", "similar-member-thumb");
    const path = m.paths && m.paths[0];
    if (path) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "";
      img.src =
        `/api/thumbnail?path=${encodeURIComponent(path)}` +
        `&hash=${encodeURIComponent(m.content_hash)}&store=0`;
      thumb.appendChild(img);
    }
    li.appendChild(thumb);

    const body = el("div", "similar-member-body");
    body.appendChild(
      el(
        "div",
        "similar-member-dist",
        m.is_reference
          ? "опорный для сравнения (самый тяжёлый файл в группе)"
          : `${m.distance} бит из ${similarState.data.threshold.bits} от опорного`
      )
    );

    if (m.is_best_copy || m.quality_rank) {
      const mark = el(
        "div",
        "similar-member-rank" + (m.is_best_copy ? " best" : ""),
        m.is_best_copy
          ? group.best_copy && group.best_copy.is_choice
            ? "★ лучшая копия по качеству"
            : "★ лучшая по техническому качеству"
          : `${m.quality_rank}-я по качеству`
      );
      body.appendChild(mark);
    }

    const facts = [];
    facts.push(fmtBytes(m.size));
    if (m.size_ratio != null && !m.is_reference) {
      if (m.size_ratio >= 1.05) facts.push(`в ${m.size_ratio.toFixed(1)} раза легче опорного`);
      else if (m.size_ratio <= 0.95) facts.push(`в ${(1 / m.size_ratio).toFixed(1)} раза тяжелее опорного`);
      else facts.push("тот же вес");
    }
    if (m.resolution) facts.push(m.resolution + (m.megapixels ? ` (${m.megapixels} МП)` : ""));
    else facts.push("разрешение не измерялось");
    const gap = m.is_reference ? null : fmtGap(m.seconds_from_reference);
    if (gap) facts.push(gap);
    body.appendChild(el("div", "similar-member-facts", facts.join(" · ")));

    // Числа, по которым посчитан порядок (задача 17). Рядом, а не вместо
    // фразы: фраза говорит, какая копия лучше, числа дают её проверить.
    // Оценка сжатия показывается только вместе со своим основанием — Р2
    // запрещает сравнивать оценки с разными основаниями, а число без
    // основания приглашает это сделать.
    const qf = [];
    if (m.sharpness != null) qf.push(`резкость ${m.sharpness.toFixed(0)}`);
    if (m.jpeg_quality != null) qf.push(`качество JPEG ${m.jpeg_quality}`);
    else if (m.recompression != null && m.recompression_basis)
      qf.push(`сжатие ${m.recompression.toFixed(2)} (${BASIS_LABELS[m.recompression_basis] || m.recompression_basis})`);
    if (qf.length) body.appendChild(el("div", "similar-member-quality", qf.join(" · ")));

    if (m.labels.length) {
      const row = el("div");
      m.labels.forEach((label) => {
        row.appendChild(
          el("span", "member-label" + (label === "дальше порога" ? " far" : ""), label)
        );
      });
      body.appendChild(row);
    }

    m.paths.forEach((p) => body.appendChild(el("div", "similar-member-path", p)));
    li.appendChild(body);
    list.appendChild(li);
  });

  $("similar-detail").scrollIntoView({ block: "nearest" });
}

function hideSimilarDetail() {
  $("similar-detail").hidden = true;
  similarState.detailId = null;
}

// Клавиатура на вкладке похожих — только перемещение и открытие. Q/K/U/1-9
// сюда не попадают вовсе: не «ничего не делают», а не доходят, потому что
// решения у этой вкладки нет (Р2). Если когда-нибудь появится действие над
// группой похожих, оно начнётся с переписывания этого комментария и Р2, а
// не с добавления ветки в switch.
function handleSimilarKey(e) {
  const g = initSimilarGrid();
  const count = g.itemCount;
  if (count === 0) return;
  const idx = g.activeIndex;
  const cols = g.columns || 1;

  const moveTo = (newIdx) => {
    g.setActive(Math.max(0, Math.min(count - 1, newIdx)));
    openActiveSimilarDetail();
  };

  switch (e.key) {
    case "ArrowDown": e.preventDefault(); moveTo(idx < 0 ? 0 : idx + cols); return;
    case "ArrowUp": e.preventDefault(); moveTo(idx < 0 ? 0 : idx - cols); return;
    case "ArrowRight": e.preventDefault(); moveTo(idx < 0 ? 0 : idx + 1); return;
    case "ArrowLeft": e.preventDefault(); moveTo(idx < 0 ? 0 : idx - 1); return;
    case "Enter":
    case " ": e.preventDefault(); moveTo(idx < 0 ? 0 : idx); return;
    default: return;
  }
}

// `change`, а не `input`: кластеризация считается на сервере, и пересчитывать
// её на каждый шаг перетаскивания ручки значило бы заказать девять расчётов
// по дороге к одному нужному. Цифра рядом с ручкой при этом двигается сразу
// (слушатель `input` ниже), так что ручка не кажется залипшей.
$("similar-threshold").addEventListener("input", (e) => {
  $("similar-threshold-value").textContent = e.target.value;
});
$("similar-threshold").addEventListener("change", (e) => {
  loadSimilar(Number(e.target.value));
});
$("similar-filter").addEventListener("change", (e) => {
  similarState.filter = e.target.value;
  if (similarState.loaded) renderSimilar();
});
$("similar-detail-close").addEventListener("click", hideSimilarDetail);

// --- разбор альбома: по одному снимку и целиком сеткой (задача 24) ---------
//
// Пятый экран, и единственный, который не живёт внутри #results: он не про
// скан и не про группы дублей, а про альбомы собранной библиотеки (задача
// 22). Всё, что ему нужно, лежит в индексе по хэшу содержимого — состав
// альбомов, метрики, лица, решения, — поэтому он открывается без единого
// скана в этой сессии и переживает перезапуск сервера.
//
// Сетка — тот же `createVirtualGrid`, что у задач 11 и 14: разбор это та же
// механика над другим запросом, а не новый экран с нуля. Клавиатура — та
// же идея, что у задачи 12 (состояние на индексе сетки, не на DOM-узле), но
// свои клавиши: 1/2/3 это три состояния снимка, а не выбор хранителя в
// группе, и путать их нельзя.

const REVIEW_STATE_LABEL = { keep: "оставить", print: "в печать", drop: "убрать" };
const REVIEW_STATE_KEY = { "1": "keep", "2": "print", "3": "drop" };

let reviewGrid = null;
const reviewState = {
  engaged: false,
  albums: [],
  folder: null,
  payload: null,
  tab: "queue",
  single: true,
  index: 0,
  totals: null,
};

// Экран разбора и экран дублей слушают одну и ту же клавиатуру, и у них
// общие клавиши с разным смыслом (1-9 там — «оставить вот эту копию»,
// 1-3 здесь — «состояние этого снимка»). Арбитр — не фокус на узле (узлы
// в пуле переиспользуются), а последнее место, куда человек ткнул.
function reviewIsEngaged() {
  return reviewState.engaged && Boolean(reviewState.payload);
}

$("review-card").addEventListener("pointerdown", () => {
  reviewState.engaged = true;
});
$("results").addEventListener("pointerdown", () => {
  reviewState.engaged = false;
});

function reviewList() {
  if (!reviewState.payload) return [];
  return reviewState.tab === "unfit"
    ? reviewState.payload.unfit
    : reviewState.payload.queue;
}

function reviewCard() {
  const list = reviewList();
  if (!list.length) return null;
  return list[Math.max(0, Math.min(list.length - 1, reviewState.index))] || null;
}

function reviewQuarantineDir() {
  return $("review-quarantine-dir").value.trim();
}

// Та же папка карантина, что у экрана дублей, и та же запись в
// localStorage: две копии одного значения разошлись бы ровно в тот момент,
// когда человек поправил одну из них.
try {
  const savedDir = localStorage.getItem(QDIR_STORAGE_KEY);
  if (savedDir) $("review-quarantine-dir").value = savedDir;
} catch {
  /* приватный режим — начинаем с пустого, как и экран дублей */
}
$("review-quarantine-dir").addEventListener("change", () => {
  const value = reviewQuarantineDir();
  try {
    localStorage.setItem(QDIR_STORAGE_KEY, value);
  } catch {
    /* non-fatal */
  }
  const other = $("quarantine-dir");
  if (other && !other.value.trim()) other.value = value;
});

async function loadReviewAlbums() {
  const status = $("review-status");
  status.textContent = "Читаю индекс…";
  let data;
  try {
    const resp = await fetch("/api/library/albums");
    if (!resp.ok) throw new Error(await resp.text());
    data = await resp.json();
  } catch (err) {
    status.textContent = `Не удалось прочитать альбомы: ${err.message}`;
    return;
  }

  reviewState.albums = data.albums || [];
  reviewState.totals = data.totals || null;
  renderReviewPrintNote(data.formats || []);

  if (!reviewState.albums.length) {
    // Пустой список — честный ответ «библиотека ещё не собрана», а не
    // пустая сетка: та читалась бы как «альбомов нет», что ровно наоборот
    // (находка задачи 11 про вкладку «Обычные файлы»).
    status.textContent =
      "В индексе нет ни одного перемещения в библиотеку — разбор идёт по альбомам " +
      "собранной библиотеки (пункт 22), а она ещё не собрана.";
    $("review-album").hidden = true;
    $("review-body").hidden = true;
    return;
  }

  const select = $("review-album");
  select.innerHTML = "";
  reviewState.albums.forEach((album) => {
    const option = document.createElement("option");
    option.value = album.folder;
    const decided = album.photos - (album.states.none || 0);
    option.textContent = `${album.name} — ${album.photos} снимков` +
      (decided ? ` (решено ${decided})` : "");
    select.appendChild(option);
  });
  select.hidden = false;

  const totals = data.totals || {};
  status.textContent =
    `Альбомов ${reviewState.albums.length}, снимков ${data.photos}. ` +
    `Решено: оставить ${totals.keep || 0}, в печать ${totals.print || 0}, ` +
    `убрать ${totals.drop || 0} (ждут перемещения ${totals.drop_pending || 0}).`;

  await loadReviewAlbum(select.value);
}

function renderReviewPrintNote(formats) {
  if (!formats.length) {
    $("review-print-note").textContent = "";
    return;
  }
  const parts = formats.map(
    (f) => `${f.label} — ${f.megapixels} МП (${f.width_px}×${f.height_px})`
  );
  $("review-print-note").textContent =
    "Порог печати считается, а не берётся из таблицы: " + parts.join(", ") +
    " при 300 dpi. Снимок, которому не хватает и на самый мелкий формат, уходит " +
    "в отдельный фильтр, а не вниз очереди.";
}

async function loadReviewAlbum(folder) {
  const status = $("review-status");
  let data;
  try {
    const resp = await fetch(`/api/library/album?folder=${encodeURIComponent(folder)}`);
    if (!resp.ok) throw new Error(await resp.text());
    data = await resp.json();
  } catch (err) {
    status.textContent = `Не удалось открыть альбом: ${err.message}`;
    return;
  }
  reviewState.folder = folder;
  reviewState.payload = data;
  reviewState.index = 0;
  setReviewTab("queue");
  $("review-body").hidden = false;
}

function renderReviewSummary() {
  const payload = reviewState.payload;
  if (!payload) return;
  const s = payload.summary;
  const bits = [
    `Снимков ${s.photos}: в очереди ${s.queue}, не годится для печати ${s.unfit}.`,
    `Решено: оставить ${s.states.keep}, в печать ${s.states.print}, ` +
      `убрать ${s.states.drop}; не просмотрено ${s.states.none}.`,
  ];
  if (s.unmeasured) {
    bits.push(
      `Без измеренных метрик ${s.unmeasured} — они стоят в конце очереди с пометкой ` +
      `«не измерялось», а не выброшены из неё.`
    );
  }
  if (s.sharpness_reference === null) {
    bits.push("Резкость с альбомом не сравнивается: измеренных снимков слишком мало.");
  } else {
    bits.push(`Медиана резкости в альбоме — ${s.sharpness_reference}.`);
  }
  $("review-summary").textContent = bits.join(" ");

  const queueTab = document.querySelector('.type-tab[data-rtab="queue"] .cnt');
  const unfitTab = document.querySelector('.type-tab[data-rtab="unfit"] .cnt');
  if (queueTab) queueTab.textContent = String(s.queue);
  if (unfitTab) unfitTab.textContent = String(s.unfit);

  $("review-drop-count").textContent = s.drop_pending
    ? `Помечено «убрать» и ждёт перемещения в этом альбоме: ${s.drop_pending}.`
    : "Помеченных «убрать» в этом альбоме нет.";
}

function setReviewTab(tab) {
  reviewState.tab = tab;
  reviewState.index = 0;
  document.querySelectorAll(".type-tab[data-rtab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.rtab === tab);
  });
  renderReviewSummary();
  renderReviewList();
}

function renderReviewList() {
  const list = reviewList();
  $("review-empty").hidden = list.length > 0;
  if (!list.length) {
    $("review-empty").textContent =
      reviewState.tab === "unfit"
        ? "Непригодных для печати в этом альбоме нет — ни у одного снимка разрешение не оказалось ниже порога."
        : "В очереди нет снимков.";
  }
  if (reviewState.single) {
    renderReviewSingle();
  } else {
    const g = initReviewGrid();
    g.setItems(list);
    if (list.length) g.setActive(reviewState.index);
  }
}

function toggleReviewView(single) {
  reviewState.single = single;
  $("review-single").hidden = !single;
  $("review-grid-card").hidden = single;
  $("review-view-btn").textContent = single ? "Сеткой (G)" : "По одному (G)";
  renderReviewList();
}

function initReviewGrid() {
  if (reviewGrid) return reviewGrid;
  reviewGrid = createVirtualGrid({
    viewport: $("review-viewport"),
    sizer: $("review-sizer"),
    pool: $("review-pool"),
    tileWidth: 168,
    tileHeight: 206,
    gap: 9,
    overscan: 3,
    renderTile: renderReviewTile,
  });
  return reviewGrid;
}

function reviewThumbUrl(card) {
  // store=0 по той же причине, что и на вкладке похожих (задача 14):
  // отпечатки и метрики есть у всей библиотеки, а кэш превью на 512 МБ
  // (Р9) рассчитан на группы дублей. Разбор тридцати тысяч снимков
  // вытеснил бы из него именно то, за чем он нужен.
  return `/api/thumbnail?path=${encodeURIComponent(card.path)}` +
    `&hash=${encodeURIComponent(card.content_hash)}&store=0`;
}

function renderReviewTile(node, card) {
  node.dataset.hash = card.content_hash;
  node.setAttribute("aria-label", `${card.name}. ${card.reason}`);

  let refs = node._reviewRefs;
  if (!refs) {
    const thumb = document.createElement("div");
    thumb.className = "tile-thumb";
    const badge = document.createElement("div");
    badge.className = "tile-decision-badge";
    const info = document.createElement("div");
    info.className = "tile-info";
    const nameEl = document.createElement("div");
    nameEl.className = "tile-size";
    const whyEl = document.createElement("div");
    whyEl.className = "tile-keeper";
    info.appendChild(nameEl);
    info.appendChild(whyEl);
    node.appendChild(thumb);
    node.appendChild(badge);
    node.appendChild(info);
    refs = { thumb, badge, nameEl, whyEl };
    node._reviewRefs = refs;
    node.addEventListener("click", () => selectReviewByHash(node.dataset.hash));
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectReviewByHash(node.dataset.hash);
        toggleReviewView(true);
      }
    });
  }

  refs.thumb.className = "tile-thumb";
  refs.thumb.innerHTML = "";
  if (IMAGE_RE.test(card.name)) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = reviewThumbUrl(card);
    img.addEventListener(
      "error",
      () => {
        refs.thumb.className = "tile-thumb no-preview kind-generic";
        refs.thumb.innerHTML = "";
        const ext = document.createElement("div");
        ext.className = "tile-ext";
        ext.textContent = (card.name.split(".").pop() || "").toUpperCase();
        refs.thumb.appendChild(ext);
      },
      { once: true }
    );
    refs.thumb.appendChild(img);
  } else {
    refs.thumb.className = "tile-thumb no-preview kind-generic";
    const ext = document.createElement("div");
    ext.className = "tile-ext";
    ext.textContent = (card.name.split(".").pop() || "").toUpperCase();
    refs.thumb.appendChild(ext);
  }

  if (card.state) {
    refs.badge.hidden = false;
    refs.badge.textContent =
      REVIEW_STATE_LABEL[card.state] + (card.applied_at ? " ✓" : "");
    refs.badge.className = `tile-decision-badge review-${card.state}`;
  } else {
    refs.badge.hidden = true;
  }

  refs.nameEl.textContent = card.name;
  refs.whyEl.textContent = card.print.text;
  refs.whyEl.title = card.reason;
  node.classList.toggle("review-unfit", card.print.unfit);
}

function selectReviewByHash(hash) {
  const list = reviewList();
  const idx = list.findIndex((c) => c.content_hash === hash);
  if (idx < 0) return;
  reviewState.index = idx;
  if (reviewGrid && !reviewState.single) reviewGrid.setActive(idx);
  if (reviewState.single) renderReviewSingle();
  $("review-state-status").textContent = "";
}

function renderReviewSingle() {
  const list = reviewList();
  const card = reviewCard();
  const img = $("review-photo");
  const fallback = $("review-fallback");

  if (!card) {
    img.hidden = true;
    fallback.hidden = false;
    fallback.textContent = "Нечего показывать.";
    $("review-position").textContent = "";
    $("review-name").textContent = "";
    $("review-path").textContent = "";
    $("review-reason").textContent = "";
    $("review-print").textContent = "";
    $("review-metrics").innerHTML = "";
    return;
  }

  img.hidden = false;
  fallback.hidden = true;
  img.src = reviewThumbUrl(card);
  img.onerror = () => {
    img.hidden = true;
    fallback.hidden = false;
    fallback.textContent = "Превью не построилось — файл на месте, показать его нечем.";
  };

  $("review-position").textContent =
    `${reviewState.index + 1} из ${list.length}` +
    (reviewState.tab === "unfit" ? " · фильтр «не годится для печати»" : "");
  $("review-name").textContent = card.name;
  $("review-path").textContent = card.path;
  $("review-reason").textContent = card.reason;
  $("review-print").textContent = card.print.text;
  $("review-print").className = "review-print" + (card.print.unfit ? " unfit" : "");

  const metrics = $("review-metrics");
  metrics.innerHTML = "";
  const rows = [
    ["Разрешение", card.width && card.height
      ? `${card.width}×${card.height} (${card.megapixels} МП)`
      : "не измерялось"],
    ["Резкость", card.sharpness === null ? "не измерялась" : String(card.sharpness)],
    ["Следы пережатия", card.recompression === null
      ? "не измерялись"
      : `${card.recompression}` +
        (card.jpeg_quality ? ` (качество JPEG ≈${card.jpeg_quality})` : "")],
    ["Лица в кадре", card.faces_scanned
      ? `${card.faces}, из них крупных ${card.prominent_faces}`
      : "детектор по этому снимку не работал"],
    ["Хэш содержимого", card.content_hash.slice(0, 16)],
  ];
  rows.forEach(([label, value]) => {
    const li = document.createElement("li");
    const strong = document.createElement("strong");
    strong.textContent = label + ": ";
    li.appendChild(strong);
    li.appendChild(document.createTextNode(value));
    metrics.appendChild(li);
  });

  document.querySelectorAll("#review-states .btn-decide[data-state]").forEach((btn) => {
    btn.classList.toggle("chosen", card.state === btn.dataset.state);
  });
  $("review-undo-btn").hidden = !card.state;
  $("review-state-status").textContent = card.state
    ? `Решено: ${REVIEW_STATE_LABEL[card.state]}` +
      (card.applied_at ? " — файл уже в карантине." : " — файл пока на месте.")
    : "Не просмотрено.";
}

// Счётчики пересчитываются из самих карточек, а не инкрементируются:
// пачка меняет сотни состояний, и сложение «было плюс одно» разошлось бы
// с правдой на первой же неудачной записи.
function recountReviewStates() {
  if (!reviewState.payload) return;
  const s = reviewState.payload.summary;
  const counts = { keep: 0, print: 0, drop: 0, none: 0 };
  let dropPending = 0;
  [...reviewState.payload.queue, ...reviewState.payload.unfit].forEach((c) => {
    counts[c.state || "none"] += 1;
    if (c.state === "drop" && !c.applied_at) dropPending += 1;
  });
  s.states = counts;
  s.reviewed = s.photos - counts.none;
  s.drop_pending = dropPending;
}

function applyLocalState(card, state) {
  card.state = state;
  card.applied_at = null;
  recountReviewStates();
}

async function setReviewCardState(card, state) {
  if (!card) return;
  try {
    const resp = await fetch("/api/library/review/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content_hash: card.content_hash,
        path: card.path,
        state,
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());
  } catch (err) {
    $("review-state-status").textContent = `Решение не записано: ${err.message}`;
    return;
  }
  applyLocalState(card, state);
  afterReviewDecision();
}

async function clearReviewCardState(card) {
  if (!card || !card.state) return;
  try {
    const resp = await fetch(
      `/api/library/review/state?content_hash=${encodeURIComponent(card.content_hash)}`,
      { method: "DELETE" }
    );
    if (!resp.ok) throw new Error(await resp.text());
  } catch (err) {
    $("review-state-status").textContent = `Отмена не прошла: ${err.message}`;
    return;
  }
  applyLocalState(card, null);
  renderReviewSummary();
  if (reviewGrid && !reviewState.single) reviewGrid.refresh();
  if (reviewState.single) renderReviewSingle();
}

function afterReviewDecision() {
  renderReviewSummary();
  if (reviewGrid && !reviewState.single) reviewGrid.refresh();
  // Автопереход к следующему снимку — та же механика, что у задачи 12:
  // решение по одному снимку и есть единица работы, и возвращаться к
  // решённому незачем. На последнем остаёмся.
  const list = reviewList();
  if (reviewState.index < list.length - 1) {
    reviewState.index += 1;
    if (reviewGrid && !reviewState.single) reviewGrid.setActive(reviewState.index);
  }
  if (reviewState.single) renderReviewSingle();
}

async function batchReview(state) {
  const list = reviewList();
  if (!list.length) return;
  const status = $("review-batch-status");
  status.textContent = `Отмечаю ${list.length}…`;
  let done = 0;
  for (const card of list) {
    try {
      const resp = await fetch("/api/library/review/state", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content_hash: card.content_hash,
          path: card.path,
          state,
        }),
      });
      if (!resp.ok) continue;
      card.state = state;
      card.applied_at = null;
      done += 1;
    } catch {
      /* одна неудача не отменяет остальные — счётчик скажет правду */
    }
  }
  recountReviewStates();
  status.textContent =
    `Отмечено «${REVIEW_STATE_LABEL[state]}»: ${done} из ${list.length}. ` +
    "Файлы не двигались — для этого есть кнопка ниже.";
  renderReviewSummary();
  if (reviewGrid && !reviewState.single) reviewGrid.refresh();
  if (reviewState.single) renderReviewSingle();
}

async function applyReviewDrops() {
  const status = $("review-apply-status");
  const out = $("review-apply-result");
  out.className = "verify-result";
  out.textContent = "";
  const dir = reviewQuarantineDir();
  if (!dir) {
    status.textContent = "Укажите папку карантина.";
    return;
  }
  if (!$("review-confirm").checked) {
    status.textContent =
      "Нужен явный второй шаг: поставьте галочку подтверждения. Без неё " +
      "ни один файл не двинется и папка карантина даже не создастся.";
    return;
  }
  status.textContent = "Перемещаю…";
  let data;
  try {
    const resp = await fetch("/api/library/review/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        quarantine_dir: dir,
        confirm: true,
        folder: $("review-apply-album").checked ? reviewState.folder : null,
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    data = await resp.json();
  } catch (err) {
    status.textContent = `Не получилось: ${err.message}`;
    return;
  }

  status.textContent =
    `Перемещено в карантин: ${data.applied} из ${data.queued}. ` +
    "Файлы не удалены — вернуть их можно на экране журнала.";
  if (data.failed && data.failed.length) {
    const ul = document.createElement("ul");
    data.failed.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = `${item.original} — ${item.error}`;
      ul.appendChild(li);
    });
    out.appendChild(ul);
  }
  $("review-confirm").checked = false;
  await loadReviewAlbum(reviewState.folder);
}

function handleReviewKey(e) {
  const list = reviewList();
  if (!list.length) return;
  const cols = reviewState.single ? 1 : (reviewGrid ? reviewGrid.columns || 1 : 1);

  const moveTo = (idx) => {
    reviewState.index = Math.max(0, Math.min(list.length - 1, idx));
    if (reviewGrid && !reviewState.single) reviewGrid.setActive(reviewState.index);
    if (reviewState.single) renderReviewSingle();
  };

  switch (e.key) {
    case "ArrowRight":
      e.preventDefault();
      moveTo(reviewState.index + 1);
      return;
    case "ArrowLeft":
      e.preventDefault();
      moveTo(reviewState.index - 1);
      return;
    case "ArrowDown":
      e.preventDefault();
      moveTo(reviewState.index + cols);
      return;
    case "ArrowUp":
      e.preventDefault();
      moveTo(reviewState.index - cols);
      return;
    case "g":
    case "G":
      e.preventDefault();
      toggleReviewView(!reviewState.single);
      return;
    case "u":
    case "U":
      e.preventDefault();
      clearReviewCardState(reviewCard());
      return;
    default:
      break;
  }

  const state = REVIEW_STATE_KEY[e.key];
  if (state) {
    e.preventDefault();
    setReviewCardState(reviewCard(), state);
  }
}

document.addEventListener("keydown", (e) => {
  if (isTypingTarget(e.target)) return;
  if (!reviewIsEngaged()) return;
  handleReviewKey(e);
});

$("review-load-btn").addEventListener("click", loadReviewAlbums);
$("review-album").addEventListener("change", (e) => loadReviewAlbum(e.target.value));
$("review-view-btn").addEventListener("click", () => toggleReviewView(!reviewState.single));
document.querySelectorAll(".type-tab[data-rtab]").forEach((btn) => {
  btn.addEventListener("click", () => setReviewTab(btn.dataset.rtab));
});
document.querySelectorAll("#review-states .btn-decide[data-state]").forEach((btn) => {
  btn.addEventListener("click", () => setReviewCardState(reviewCard(), btn.dataset.state));
});
$("review-undo-btn").addEventListener("click", () => clearReviewCardState(reviewCard()));
document.querySelectorAll(".review-batch button[data-batch]").forEach((btn) => {
  btn.addEventListener("click", () => batchReview(btn.dataset.batch));
});
$("review-apply-btn").addEventListener("click", applyReviewDrops);

let currentScanId = null;
let currentReport = null;
let pollTimer = null;

function selectedMode() {
  const picked = document.querySelector('input[name="scan-mode"]:checked');
  return picked ? picked.value : "quick";
}

const $ = (id) => document.getElementById(id);

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
// The report itself still arrives as one JSON response (pilot finding
// P2.9: no pagination, 10.4 MB / 8814 groups on the real disk this was
// built against — see claude/design-decisions.md for the numbers behind
// that call and the gzip middleware added in app.py to shrink the actual
// transfer). What this file is responsible for is not re-litigating that
// call — it is making sure the DOM cost of *displaying* 8814 groups is
// independent of their count, because that is the part that used to
// freeze the page: the old renderReport() below built one full DOM
// subtree (including an <img> tag) per group, eagerly, for every group in
// the report, all three blocks at once.

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
function createVirtualGrid({ viewport, sizer, pool, tileWidth, tileHeight, gap, overscan, renderTile }) {
  let items = [];
  let columns = 1;
  let slots = [];
  let rafPending = false;

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
    });
  }

  function scheduleRender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(render);
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
      viewport.scrollTop = 0;
      computeColumns();
      ensurePool();
      render();
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

// Builds one tile's DOM structure once; renderGridTile() below only ever
// rewrites its content, never recreates it, since it is reused from the
// pool.
function buildTileSkeleton() {
  const thumb = document.createElement("div");
  thumb.className = "tile-thumb";
  const countBadge = document.createElement("div");
  countBadge.className = "tile-count-badge";
  const info = document.createElement("div");
  info.className = "tile-info";
  const sizeEl = document.createElement("div");
  sizeEl.className = "tile-size";
  const keeperEl = document.createElement("div");
  keeperEl.className = "tile-keeper";
  info.appendChild(sizeEl);
  info.appendChild(keeperEl);
  return { thumb, countBadge, info, sizeEl, keeperEl };
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
    node.appendChild(refs.info);
    node._refs = refs;
    node.addEventListener("click", () => showGroupDetail(node.dataset.hash));
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        showGroupDetail(node.dataset.hash);
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
  refs.keeperEl.textContent = group.keeper_display_path
    ? shortenPath(group.keeper_display_path)
    : "—";
  refs.keeperEl.title = group.keeper_display_path || "";

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

function setActiveTab(tab) {
  activeTab = tab;
  document.querySelectorAll(".type-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  const items = buckets[tab];
  $("grid-empty").hidden = items.length > 0;
  $("grid-viewport").hidden = items.length === 0;
  hideGroupDetail();
  initGrid().setItems(items);
}

function showGroupDetail(hash) {
  const group = groupsByHash.get(hash);
  if (!group) return;
  const panel = $("group-detail");
  panel.hidden = false;

  const keeperPath = group.keeper_display_path;
  $("detail-title").textContent =
    `${group.records.length} копии, по ${fmtBytes(group.size)} — освободится ${fmtBytes(group.wasted_bytes)}`;

  const list = $("detail-records");
  list.innerHTML = "";
  group.records.forEach((r) => {
    const li = document.createElement("li");
    li.className = "detail-record" + (r.display_path === keeperPath ? " keeper-badge" : "");
    const archiveNote = r.is_archive_member ? " (внутри архива)" : "";
    li.textContent = r.display_path + archiveNote + (r.display_path === keeperPath ? " — оставить" : "");
    list.appendChild(li);
  });

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

  wireVerify(group);
  panel.scrollIntoView({ block: "nearest" });
}

function hideGroupDetail() {
  $("group-detail").hidden = true;
}

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

  document.querySelectorAll(".type-tab").forEach((btn) => {
    const tab = btn.dataset.tab;
    btn.querySelector(".cnt").textContent = fmtNumber(buckets[tab].length);
  });

  // "Обычные файлы" is the natural first tab, but a real personal photo
  // library (the one this was tested against: D:\Photos plus a Google
  // Photos takeout) can be almost entirely media -- 8812 of 8814 groups on
  // that report, 0 in "plain". Landing on an empty tab reads as "no
  // duplicates found", which is the opposite of true, so the default
  // follows the data: the first tab in the usual order that actually has
  // something in it.
  const tabOrder = ["plain", "media", "archive"];
  const defaultTab = tabOrder.find((t) => buckets[t].length > 0) || "plain";
  setActiveTab(defaultTab);
}

document.querySelectorAll(".type-tab").forEach((btn) => {
  btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
});

$("detail-close-btn").addEventListener("click", hideGroupDetail);

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

refreshIndexStats();

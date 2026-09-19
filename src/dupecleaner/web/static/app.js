let currentScanId = null;
let currentReport = null;
let pollTimer = null;

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
    body: JSON.stringify({ paths, include_archives: $("include-archives").checked }),
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

// --- results ----------------------------------------------------------------

async function loadResult() {
  const resp = await fetch(`/api/scan/${currentScanId}/result`);
  if (!resp.ok) return;
  currentReport = await resp.json();
  renderReport(currentReport);
}

function recordEl(record, isKeeper, groupHash) {
  const div = document.createElement("div");
  div.className = "dup-record";
  // HEIC/HEIF added alongside pillow-heif (pilot finding P2.8) — the
  // server can now decode them, so the grid should actually ask for them.
  const isImage = /\.(jpg|jpeg|png|gif|bmp|webp|heic|heif)$/i.test(record.display_path);
  if (isImage && !record.is_archive_member) {
    const img = document.createElement("img");
    // `hash` is the group's content hash — the thumbnail cache key
    // (thumbnails.py). Sending it lets the server skip a path lookup and
    // serve straight from cache.
    const hashParam = groupHash ? `&hash=${encodeURIComponent(groupHash)}` : "";
    img.src = `/api/thumbnail?path=${encodeURIComponent(record.real_path)}${hashParam}`;
    img.loading = "lazy";
    div.appendChild(img);
  }
  const label = document.createElement("div");
  label.textContent = record.display_path + (isKeeper ? " (оставить)" : "");
  if (isKeeper) label.className = "keeper-badge";
  div.appendChild(label);
  return div;
}

function groupEl(group, checkable) {
  const wrap = document.createElement("div");
  wrap.className = "dup-group";

  if (checkable) {
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "group-check";
    checkbox.dataset.hash = group.content_hash;
    checkbox.checked = true;
    wrap.appendChild(checkbox);
  }

  const title = document.createElement("strong");
  title.textContent = ` ${group.records.length} копии, по ${fmtBytes(group.size)} — освободится ${fmtBytes(group.wasted_bytes)}`;
  wrap.appendChild(title);

  const recordsDiv = document.createElement("div");
  recordsDiv.className = "dup-records";
  const keeperPath = group.records
    .slice()
    .sort((a, b) => a.display_path.length - b.display_path.length)[0].display_path;
  group.records.forEach((r) => recordsDiv.appendChild(recordEl(r, r.display_path === keeperPath, group.content_hash)));
  wrap.appendChild(recordsDiv);

  return wrap;
}

function renderReport(report) {
  $("results").hidden = false;
  $("summary").textContent =
    `Групп дублей: ${fmtNumber(report.groups.length)}. ` +
    `Потенциально можно освободить: ${fmtBytes(report.total_wasted_bytes)}.`;

  const plain = $("plain-groups");
  const media = $("media-groups");
  const archiveOnly = $("archive-groups");
  plain.innerHTML = media.innerHTML = archiveOnly.innerHTML = "";

  report.groups.forEach((g) => {
    if (g.only_archive_members) archiveOnly.appendChild(groupEl(g, false));
    else if (g.is_media) media.appendChild(groupEl(g, true));
    else plain.appendChild(groupEl(g, true));
  });
}

$("quarantine-btn").addEventListener("click", async () => {
  const status = $("quarantine-status");
  const quarantineDir = $("quarantine-dir").value.trim();
  if (!currentScanId || !currentReport) { status.textContent = "Сначала выполните скан."; return; }
  if (!quarantineDir) { status.textContent = "Укажите папку карантина."; return; }

  const checked = Array.from(document.querySelectorAll(".group-check:checked")).map((c) => c.dataset.hash);
  const hasMediaChecked = currentReport.groups.some(
    (g) => g.is_media && !g.only_archive_members && checked.includes(g.content_hash)
  );
  if (hasMediaChecked) {
    status.textContent = "Медиа-группы подтверждаются отдельно, после ручного просмотра.";
    return;
  }

  status.textContent = "Перемещение в карантин...";
  const resp = await fetch(`/api/scan/${currentScanId}/quarantine`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ quarantine_dir: quarantineDir, group_hashes: checked, confirm_media: false }),
  });
  if (!resp.ok) { status.textContent = `Ошибка: ${await resp.text()}`; return; }

  const result = await resp.json();
  status.textContent = `Перемещено файлов: ${result.moved.length}. Манифест: ${quarantineDir}\\manifest.json`;
});

refreshIndexStats();

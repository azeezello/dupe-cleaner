let currentScanId = null;
let currentReport = null;

function fmtBytes(n) {
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(1)} ${units[i]}`;
}

function recordEl(record, isKeeper) {
  const div = document.createElement("div");
  div.className = "dup-record";
  const isImage = /\.(jpg|jpeg|png|gif|bmp|webp)$/i.test(record.display_path);
  if (isImage && !record.is_archive_member) {
    const img = document.createElement("img");
    img.src = `/api/thumbnail?path=${encodeURIComponent(record.real_path)}`;
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
  title.textContent = ` ${group.records.length} копии, по ${fmtBytes(group.size)} каждая — освободится ${fmtBytes(group.wasted_bytes)}`;
  wrap.appendChild(title);

  const recordsDiv = document.createElement("div");
  recordsDiv.className = "dup-records";
  const keeperPath = group.records
    .slice()
    .sort((a, b) => a.display_path.length - b.display_path.length)[0].display_path;
  group.records.forEach((r) => recordsDiv.appendChild(recordEl(r, r.display_path === keeperPath)));
  wrap.appendChild(recordsDiv);

  return wrap;
}

document.getElementById("scan-btn").addEventListener("click", async () => {
  const paths = document.getElementById("paths").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const includeArchives = document.getElementById("include-archives").checked;
  const status = document.getElementById("scan-status");
  if (paths.length === 0) {
    status.textContent = "Укажите хотя бы одну папку.";
    return;
  }
  status.textContent = "Сканирование... это может занять время для больших дисков.";

  const resp = await fetch("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, include_archives: includeArchives }),
  });
  if (!resp.ok) {
    status.textContent = `Ошибка: ${await resp.text()}`;
    return;
  }
  currentReport = await resp.json();
  currentScanId = currentReport.scan_id;
  status.textContent = `Готово. Просмотрено файлов: ${currentReport.total_files_seen}.`;
  renderReport(currentReport);
});

function renderReport(report) {
  document.getElementById("results").hidden = false;
  document.getElementById("summary").textContent =
    `Групп дублей: ${report.groups.length}. Потенциально можно освободить: ${fmtBytes(report.total_wasted_bytes)}.` +
    (report.warnings.length ? ` Предупреждений: ${report.warnings.length}.` : "");

  const plain = document.getElementById("plain-groups");
  const media = document.getElementById("media-groups");
  const archiveOnly = document.getElementById("archive-groups");
  plain.innerHTML = "";
  media.innerHTML = "";
  archiveOnly.innerHTML = "";

  report.groups.forEach((g) => {
    if (g.only_archive_members) {
      archiveOnly.appendChild(groupEl(g, false));
    } else if (g.is_media) {
      media.appendChild(groupEl(g, true));
    } else {
      plain.appendChild(groupEl(g, true));
    }
  });
}

document.getElementById("quarantine-btn").addEventListener("click", async () => {
  const status = document.getElementById("quarantine-status");
  const quarantineDir = document.getElementById("quarantine-dir").value.trim();
  if (!currentScanId) { status.textContent = "Сначала выполните скан."; return; }
  if (!quarantineDir) { status.textContent = "Укажите папку карантина."; return; }

  const checked = Array.from(document.querySelectorAll(".group-check:checked")).map((c) => c.dataset.hash);
  const hasMediaChecked = currentReport.groups.some(
    (g) => g.is_media && !g.only_archive_members && checked.includes(g.content_hash)
  );
  if (hasMediaChecked) {
    status.textContent = "Отметьте медиа-группы отдельно после ручной проверки (см. предупреждение выше).";
    return;
  }

  status.textContent = "Перемещение в карантин...";
  const resp = await fetch(`/api/scan/${currentScanId}/quarantine`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ quarantine_dir: quarantineDir, group_hashes: checked, confirm_media: false }),
  });
  if (!resp.ok) {
    status.textContent = `Ошибка: ${await resp.text()}`;
    return;
  }
  const result = await resp.json();
  status.textContent = `Перемещено файлов: ${result.moved.length}. Манифест сохранён в ${quarantineDir}\\manifest.json.`;
});

// Album discovery for the browser. Mirrors discover_album_queue and
// IGNORED_SCAN_DIRS in gui.py. Only folder intake lives here; no analysis.

const IGNORED_SCAN_DIRS = new Set([
  "_spectrograms",
  "__pycache__",
  "system volume information",
  "@eadir",
  "#recycle",
]);

export function isSkippedFolder(name) {
  const lower = name.toLowerCase();
  return lower.startsWith(".") || lower.startsWith("$") || IGNORED_SCAN_DIRS.has(lower);
}

// entries: [{file, relPath}] where relPath is "Root/Album/01.flac".
// The first path segment is the dropped or selected folder and is never
// filtered, as the desktop walk never filters its root.
export function buildAlbums(entries, dropName) {
  const albums = new Map();
  for (const { file, relPath } of entries) {
    const parts = relPath.split("/").filter(Boolean);
    if (!parts.length || !parts[parts.length - 1].toLowerCase().endsWith(".flac")) {
      continue;
    }
    const dirs = parts.slice(0, -1);
    if (dirs.slice(1).some(isSkippedFolder)) continue;
    const key = dirs.length ? dirs.join("/") : dropName;
    let album = albums.get(key);
    if (!album) {
      album = { key, label: key.split("/").pop(), tracks: [] };
      albums.set(key, album);
    }
    album.tracks.push({ file, relPath, vpath: `/albums/${key}/${file.name}` });
  }
  return [...albums.values()];
}

// fileList: FileList from <input webkitdirectory> or multiple file input.
export function entriesFromInput(fileList) {
  return Array.from(fileList, (file) => ({
    file,
    relPath: file.webkitRelativePath || file.name,
  }));
}

// dataTransferItems: DataTransferItemList from a drop event. The entries are
// read synchronously before the first await, as the browser requires.
export async function entriesFromDrop(dataTransferItems) {
  const roots = Array.from(dataTransferItems, (item) =>
    item.kind === "file" ? item.webkitGetAsEntry() : null,
  ).filter(Boolean);
  const out = [];
  for (const root of roots) await walkEntry(root, "", out);
  return out;
}

async function walkEntry(node, parent, out) {
  const relPath = parent ? `${parent}/${node.name}` : node.name;
  if (node.isFile) {
    if (!node.name.toLowerCase().endsWith(".flac")) return;
    const file = await new Promise((resolve, reject) => node.file(resolve, reject));
    out.push({ file, relPath });
    return;
  }
  if (parent && isSkippedFolder(node.name)) return;
  const reader = node.createReader();
  for (const child of await readAllEntries(reader)) {
    await walkEntry(child, relPath, out);
  }
}

// readEntries returns batches; an empty batch marks the end.
async function readAllEntries(reader) {
  const all = [];
  for (;;) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) return all;
    all.push(...batch);
  }
}

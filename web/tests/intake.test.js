import { test } from "node:test";
import assert from "node:assert/strict";
import { buildAlbums } from "../intake.js";

const entry = (relPath) => ({
  file: { name: relPath.split("/").pop(), size: 1, lastModified: 0 },
  relPath,
});

test("skips ignored and dot/$ folders", () => {
  const albums = buildAlbums(
    [
      entry("A/Keep/1.flac"),
      entry("A/@eaDir/Album/1.flac"),
      entry("A/.hidden/1.flac"),
      entry("A/$RECYCLE.BIN/1.flac"),
      entry("A/_spectrograms/1.flac"),
      entry("A/@EADIR/Album/2.flac"),
      entry("A/System Volume Information/1.flac"),
    ],
    "Dropped files",
  );
  assert.deepEqual(albums.map((a) => a.key), ["A/Keep"]);
});

test("non-flac files dropped", () => {
  const albums = buildAlbums(
    [entry("A/cover.jpg"), entry("A/x.FLAC")],
    "Dropped files",
  );
  assert.equal(albums.length, 1);
  assert.deepEqual(albums[0].tracks.map((t) => t.relPath), ["A/x.FLAC"]);
});

test("one album per folder with direct flac", () => {
  const albums = buildAlbums(
    [
      entry("Artist/Album1/1.flac"),
      entry("Artist/Album2/1.flac"),
      entry("Artist/notes.txt"),
    ],
    "Dropped files",
  );
  assert.deepEqual(albums.map((a) => a.key), ["Artist/Album1", "Artist/Album2"]);
  assert.deepEqual(albums.map((a) => a.label), ["Album1", "Album2"]);
});

test("same filename in two albums gets distinct vpaths", () => {
  const albums = buildAlbums(
    [entry("Artist/Album1/1.flac"), entry("Artist/Album2/1.flac")],
    "Dropped files",
  );
  const vpaths = albums.flatMap((a) => a.tracks.map((t) => t.vpath));
  assert.deepEqual(vpaths, [
    "/albums/Artist/Album1/1.flac",
    "/albums/Artist/Album2/1.flac",
  ]);
  assert.equal(new Set(vpaths).size, 2);
});

test("loose files form one album named after the drop", () => {
  const albums = buildAlbums([entry("1.flac")], "Dropped files");
  assert.equal(albums.length, 1);
  assert.equal(albums[0].key, "Dropped files");
  assert.equal(albums[0].label, "Dropped files");
  assert.equal(albums[0].tracks[0].vpath, "/albums/Dropped files/1.flac");
});

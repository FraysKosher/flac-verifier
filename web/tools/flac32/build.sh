#!/usr/bin/env bash
# Rebuild web/vendor/flac32.{mjs,wasm} from libFLAC 1.5.0 with emscripten.
# Requires: emsdk at $EMSDK_DIR (default ../../tools/emsdk beside the repo), cmake, curl.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
web="$(cd "$here/../.." && pwd)"
tools="${FLAC32_TOOLS:-$web/../../tools}"
EMSDK_DIR="${EMSDK_DIR:-$tools/emsdk}"
EMSDK_VERSION=7.0.0
LIBFLAC_VERSION=1.5.0
LIBFLAC_SHA256=f2c1c76592a82ffff8413ba3c4a1299b6c7ab06c734dee03fd88630485c2b920
LIBFLAC_URL=https://ftp.osuosl.org/pub/xiph/releases/flac/flac-$LIBFLAC_VERSION.tar.xz

[ -d "$EMSDK_DIR" ] || { echo "emsdk not found at $EMSDK_DIR" >&2; exit 1; }
(cd "$EMSDK_DIR" && ./emsdk install "$EMSDK_VERSION" && ./emsdk activate "$EMSDK_VERSION") >/dev/null
source "$EMSDK_DIR/emsdk_env.sh" >/dev/null
echo "emcc: $(emcc --version | head -1)"
emcc --version | head -1 | grep -q " $EMSDK_VERSION " || { echo "emcc is not $EMSDK_VERSION" >&2; exit 1; }
mkdir -p "$tools"
tar_path="$tools/flac-$LIBFLAC_VERSION.tar.xz"
[ -f "$tar_path" ] || curl -fsSL -o "$tar_path" "$LIBFLAC_URL"
echo "$LIBFLAC_SHA256  $tar_path" | sha256sum -c -
src="$tools/flac-$LIBFLAC_VERSION"
[ -d "$src" ] || tar -C "$tools" -xf "$tar_path"
b="$tools/flac-build"
rm -rf "$b"
emcmake cmake -S "$src" -B "$b" -DCMAKE_BUILD_TYPE=Release -DBUILD_PROGRAMS=OFF \
  -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DBUILD_DOCS=OFF -DWITH_OGG=OFF \
  -DINSTALL_MANPAGES=OFF -DBUILD_CXXLIBS=OFF -DBUILD_SHARED_LIBS=OFF \
  -DCMAKE_C_FLAGS=-O3 -DWITH_STACK_PROTECTOR=OFF -DWITH_AVX=OFF
cmake --build "$b" --target FLAC -j"$(nproc)"
lib="$(find "$b" -name 'libFLAC.a' | head -1)"
mkdir -p "$web/vendor"
emcc -O3 "$here/flac32.c" "$lib" -I"$src/include" -o "$web/vendor/flac32.mjs" \
  -sMODULARIZE=1 -sEXPORT_ES6=1 -sENVIRONMENT=web,worker,node -sALLOW_MEMORY_GROWTH=1 \
  -sEXPORT_NAME=createFlac32 -sFILESYSTEM=0 -sINITIAL_MEMORY=16MB \
  -sEXPORTED_FUNCTIONS=_flac32_decode,_flac32_ok,_flac32_oom,_flac32_channels,_flac32_sample_rate,_flac32_bits,_flac32_frames,_flac32_data,_flac32_free,_malloc,_free \
  -sEXPORTED_RUNTIME_METHODS=HEAPU8,HEAP32

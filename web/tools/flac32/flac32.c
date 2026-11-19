/* Decode an in-memory FLAC stream to interleaved int32 using libFLAC. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <FLAC/stream_decoder.h>

typedef struct {
  const uint8_t *in;
  size_t in_len, in_pos;
  uint32_t channels, sample_rate, bits;
  uint64_t total_frames;
  int32_t *data;
  size_t used, cap; /* in samples */
  int failed;
} Handle;

static FLAC__StreamDecoderReadStatus on_read(const FLAC__StreamDecoder *d, FLAC__byte *buf, size_t *bytes, void *c) {
  Handle *h = c;
  size_t n = h->in_len - h->in_pos;
  (void)d;
  if (n == 0) { *bytes = 0; return FLAC__STREAM_DECODER_READ_STATUS_END_OF_STREAM; }
  if (n > *bytes) n = *bytes;
  memcpy(buf, h->in + h->in_pos, n);
  h->in_pos += n;
  *bytes = n;
  return FLAC__STREAM_DECODER_READ_STATUS_CONTINUE;
}

static FLAC__StreamDecoderWriteStatus on_write(const FLAC__StreamDecoder *d, const FLAC__Frame *f, const FLAC__int32 *const buf[], void *c) {
  Handle *h = c;
  size_t need = h->used + (size_t)f->header.blocksize * f->header.channels;
  uint32_t i, ch;
  (void)d;
  if (need > h->cap) {
    size_t cap = h->cap ? h->cap : 1u << 16;
    while (cap < need) cap *= 2;
    int32_t *p = realloc(h->data, cap * sizeof(int32_t));
    if (!p) { h->failed = 1; return FLAC__STREAM_DECODER_WRITE_STATUS_ABORT; }
    h->data = p; h->cap = cap;
  }
  for (i = 0; i < f->header.blocksize; i++)
    for (ch = 0; ch < f->header.channels; ch++)
      h->data[h->used++] = buf[ch][i];
  return FLAC__STREAM_DECODER_WRITE_STATUS_CONTINUE;
}

static void on_meta(const FLAC__StreamDecoder *d, const FLAC__StreamMetadata *m, void *c) {
  Handle *h = c;
  (void)d;
  if (m->type == FLAC__METADATA_TYPE_STREAMINFO) {
    h->channels = m->data.stream_info.channels;
    h->sample_rate = m->data.stream_info.sample_rate;
    h->bits = m->data.stream_info.bits_per_sample;
    h->total_frames = m->data.stream_info.total_samples;
  }
}

static void on_error(const FLAC__StreamDecoder *d, FLAC__StreamDecoderErrorStatus s, void *c) {
  (void)d; (void)s;
  ((Handle *)c)->failed = 1;
}

/* in must stay valid until flac32_free. Returns NULL on allocation failure;
   check flac32_ok for decode success. */
Handle *flac32_decode(const uint8_t *in, size_t len) {
  Handle *h = calloc(1, sizeof *h);
  if (!h) return NULL;
  h->in = in; h->in_len = len;
  FLAC__StreamDecoder *d = FLAC__stream_decoder_new();
  if (!d) { h->failed = 1; return h; }
  FLAC__stream_decoder_set_md5_checking(d, 1);
  if (FLAC__stream_decoder_init_stream(d, on_read, NULL, NULL, NULL, NULL, on_write, on_meta, on_error, h)
      != FLAC__STREAM_DECODER_INIT_STATUS_OK) h->failed = 1;
  else {
    if (!FLAC__stream_decoder_process_until_end_of_stream(d)) h->failed = 1;
    if (!FLAC__stream_decoder_finish(d)) h->failed = 1; /* false on MD5 mismatch */
  }
  FLAC__stream_decoder_delete(d);
  if (h->channels && h->total_frames && h->used != h->total_frames * h->channels) h->failed = 1;
  return h;
}
int flac32_ok(Handle *h) { return !h->failed && h->channels > 0; }
uint32_t flac32_channels(Handle *h) { return h->channels; }
uint32_t flac32_sample_rate(Handle *h) { return h->sample_rate; }
uint32_t flac32_bits(Handle *h) { return h->bits; }
double flac32_frames(Handle *h) { return h->channels ? (double)(h->used / h->channels) : 0; }
int32_t *flac32_data(Handle *h) { return h->data; }
void flac32_free(Handle *h) { if (h) { free(h->data); free(h); } }

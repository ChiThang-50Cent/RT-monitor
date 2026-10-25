#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pipewire/pipewire.h>
#include <spa/param/video/raw-utils.h>
#include <spa/param/buffers.h>
#include <spa/buffer/meta.h>

#define EDGE 384u
#define PIXELS (EDGE * EDGE * 4u)
#define META_SIZE (sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + PIXELS)

struct capture {
    struct pw_main_loop *main;
    struct pw_stream *stream;
    struct spa_source *output;
    int ready_fd, failed;
    bool running, visible, dirty, have_shape, image_hidden, closing;
    int32_t x, y, hx, hy;
    uint32_t width, height;
    uint64_t shape, sent_shape, packet_shape;
    uint8_t pixels[PIXELS], packet[1024 + PIXELS];
    size_t length, written;
};

static void fail(struct capture *c, const char *reason)
{
    fprintf(stderr, "surface-cursor-capture: %s\n", reason);
    c->failed = 1;
    pw_main_loop_quit(c->main);
}

static void output(void *data, int fd, uint32_t mask)
{
    struct capture *c = data;
    (void)fd;
    if (mask & (SPA_IO_ERR | SPA_IO_HUP)) {
        fail(c, "output closed");
        return;
    }
    for (;;) {
        if (c->written == c->length) {
            c->sent_shape = c->packet_shape;
            c->length = c->written = 0;
            if (!c->dirty || !c->running)
                break;
            c->dirty = false;
            bool bitmap = c->visible && c->shape != c->sent_shape;
            int n;
            if (!c->visible)
                n = snprintf((char *)c->packet, 1024, "{\"visible\":false,\"bytes\":0}\n");
            else if (bitmap)
                n = snprintf((char *)c->packet, 1024,
                    "{\"visible\":true,\"x\":%d,\"y\":%d,\"shape\":[%u,%u,%d,%d],\"bytes\":%u}\n",
                    c->x, c->y, c->width, c->height, c->hx, c->hy,
                    c->width * c->height * 4u);
            else
                n = snprintf((char *)c->packet, 1024,
                    "{\"visible\":true,\"x\":%d,\"y\":%d,\"bytes\":0}\n", c->x, c->y);
            c->length = (size_t)n;
            c->packet_shape = c->sent_shape;
            if (bitmap) {
                size_t bytes = c->width * c->height * 4u;
                memcpy(c->packet + c->length, c->pixels, bytes);
                c->length += bytes;
                c->packet_shape = c->shape;
            }
        }
        ssize_t n = write(STDOUT_FILENO, c->packet + c->written, c->length - c->written);
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            break;
        if (n <= 0) {
            fail(c, "output write failed");
            return;
        }
        c->written += (size_t)n;
    }
    pw_loop_update_io(pw_main_loop_get_loop(c->main), c->output,
                      c->length ? SPA_IO_OUT : 0);
}

static bool metadata(struct capture *c, const struct spa_meta *meta)
{
    if (!meta->data || meta->size < sizeof(struct spa_meta_cursor))
        return false;
    const uint8_t *base = meta->data;
    struct spa_meta_cursor cursor;
    memcpy(&cursor, base, sizeof(cursor));
    if (!cursor.id) {
        if (c->visible) {
            c->visible = false;
            c->dirty = true;
        }
        return true;
    }
    if (cursor.position.x < -32768 || cursor.position.x > 32767 ||
        cursor.position.y < -32768 || cursor.position.y > 32767)
        return false;
    if (cursor.bitmap_offset) {
        size_t start = cursor.bitmap_offset;
        if (start < sizeof(cursor) || start > meta->size ||
            meta->size - start < sizeof(struct spa_meta_bitmap))
            return false;
        struct spa_meta_bitmap bm;
        memcpy(&bm, base + start, sizeof(bm));
        if (bm.format != 0) {
            if (!bm.offset) {
                c->visible = false;
                c->image_hidden = true;
                c->dirty = true;
                return true;
            }
            if ((bm.format != SPA_VIDEO_FORMAT_BGRA && bm.format != SPA_VIDEO_FORMAT_RGBA) ||
                !bm.size.width || !bm.size.height || bm.size.width > EDGE || bm.size.height > EDGE ||
                cursor.hotspot.x < 0 || cursor.hotspot.y < 0 ||
                (uint32_t)cursor.hotspot.x >= bm.size.width || (uint32_t)cursor.hotspot.y >= bm.size.height ||
                bm.offset < sizeof(bm))
                return false;
            size_t row = bm.size.width * 4u;
            int64_t stride = bm.stride;
            uint64_t pitch = stride < 0 ? (uint64_t)-stride : (uint64_t)stride;
            if (pitch < row || bm.offset > meta->size - start)
                return false;
            size_t available = meta->size - start - bm.offset;
            uint64_t span = pitch * (bm.size.height - 1u);
            if (row > available ||
                (stride >= 0 && span > available - row) ||
                (stride < 0 && span > bm.offset - sizeof(bm)))
                return false;
            const uint8_t *pixels = base + start + bm.offset;
            bool changed = !c->have_shape || c->width != bm.size.width || c->height != bm.size.height ||
                           c->hx != cursor.hotspot.x || c->hy != cursor.hotspot.y;
            for (uint32_t y = 0; y < bm.size.height; ++y) {
                const uint8_t *src = pixels + (int64_t)y * stride;
                for (size_t x = 0; x < row; ++x) {
                    size_t component = x % 4u;
                    size_t source = bm.format == SPA_VIDEO_FORMAT_RGBA && (component == 0 || component == 2)
                                  ? x + 2u - 2u * component : x;
                    if (c->pixels[y * row + x] != src[source])
                        changed = true;
                }
            }
            if (changed) {
                for (uint32_t y = 0; y < bm.size.height; ++y) {
                    const uint8_t *src = pixels + (int64_t)y * stride;
                    uint8_t *dst = c->pixels + y * row;
                    if (bm.format == SPA_VIDEO_FORMAT_BGRA)
                        memcpy(dst, src, row);
                    else
                        for (uint32_t x = 0; x < bm.size.width; ++x) {
                            dst[4*x] = src[4*x+2]; dst[4*x+1] = src[4*x+1];
                            dst[4*x+2] = src[4*x]; dst[4*x+3] = src[4*x+3];
                        }
                }
                c->width = bm.size.width; c->height = bm.size.height;
                c->hx = cursor.hotspot.x; c->hy = cursor.hotspot.y;
                c->have_shape = true;
                ++c->shape;
                c->dirty = true;
            }
            c->image_hidden = false;
        }
    }
    if (c->image_hidden)
        return true;
    if (!c->have_shape)
        return false;
    bool visible = c->have_shape;
    if (c->visible != visible || (visible && (c->x != cursor.position.x || c->y != cursor.position.y)))
        c->dirty = true;
    c->visible = visible;
    c->x = cursor.position.x; c->y = cursor.position.y;
    return true;
}

static void process(void *data)
{
    struct capture *c = data;
    struct pw_buffer *buffer;
    bool valid = true;
    bool observed = false;
    while ((buffer = pw_stream_dequeue_buffer(c->stream))) {
        struct spa_buffer *b = buffer->buffer;
        for (uint32_t i = 0; i < b->n_metas; ++i) {
            if (b->metas[i].type == SPA_META_Cursor) {
                observed = true;
                if (!metadata(c, &b->metas[i]))
                    valid = false;
            }
        }
        pw_stream_queue_buffer(c->stream, buffer);
    }
    if (!valid) {
        fail(c, "invalid cursor metadata or visible cursor without initial bitmap");
        return;
    }
    if (c->ready_fd >= 0 && observed && c->running) {
        if (write(c->ready_fd, "R", 1) != 1) {
            fail(c, "parent readiness pipe closed");
            return;
        }
        close(c->ready_fd);
        c->ready_fd = -1;
        c->dirty = true;
    }
    if (c->ready_fd < 0)
        output(c, STDOUT_FILENO, 0);
}

static void param_changed(void *data, uint32_t id, const struct spa_pod *param)
{
    struct capture *c = data;
    if (id != SPA_PARAM_Format || !param)
        return;
    uint8_t storage[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    const struct spa_pod *params[2];
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, 8),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_Int((1 << SPA_DATA_MemPtr) | (1 << SPA_DATA_MemFd)));
    params[1] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor), SPA_PARAM_META_size, SPA_POD_Int(META_SIZE));
    if (pw_stream_update_params(c->stream, params, 2) < 0)
        fail(c, "metadata negotiation failed");
}

static void state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error)
{
    struct capture *c = data;
    if (c->closing)
        return;
    if (state == PW_STREAM_STATE_ERROR ||
        (state == PW_STREAM_STATE_UNCONNECTED && old != PW_STREAM_STATE_UNCONNECTED)) {
        fail(c, error ? error : "stream disconnected");
    } else if (state == PW_STREAM_STATE_STREAMING && !c->running) {
        c->running = true;
        c->dirty = true;
    }
}

static void parent_input(void *data, int fd, uint32_t mask)
{
    struct capture *c = data;
    char byte;
    if ((mask & (SPA_IO_ERR | SPA_IO_HUP)) || read(fd, &byte, 1) <= 0)
        pw_main_loop_quit(c->main);
}

static void stop(void *data, int signal_number)
{
    (void)signal_number;
    pw_main_loop_quit(((struct capture *)data)->main);
}

int main(int argc, char **argv)
{
    char *end;
    if (argc != 3)
        return fprintf(stderr, "Usage: %s NODE READY_FD\n", argv[0]), 1;
    errno = 0;
    unsigned long node = strtoul(argv[1], &end, 10);
    if (errno || !*argv[1] || *end || node >= UINT32_MAX)
        return 1;
    long ready = strtol(argv[2], &end, 10);
    if (errno || !*argv[2] || *end || ready < 3 || ready > INT32_MAX)
        return 1;
    static struct capture c;
    c.ready_fd = (int)ready;
    signal(SIGPIPE, SIG_IGN);
    if (fcntl(STDOUT_FILENO, F_SETFL, O_NONBLOCK) < 0)
        return 1;
    pw_init(NULL, NULL);
    c.main = pw_main_loop_new(NULL);
    if (!c.main)
        return 1;
    struct pw_loop *loop = pw_main_loop_get_loop(c.main);
    c.output = pw_loop_add_io(loop, STDOUT_FILENO, 0, false, output, &c);
    struct spa_source *parent = pw_loop_add_io(loop, STDIN_FILENO, SPA_IO_IN | SPA_IO_HUP, false, parent_input, &c);
    struct spa_source *term = pw_loop_add_signal(loop, SIGTERM, stop, &c);
    struct spa_source *interrupt = pw_loop_add_signal(loop, SIGINT, stop, &c);
    static const struct pw_stream_events events = {
        PW_VERSION_STREAM_EVENTS, .state_changed = state_changed,
        .param_changed = param_changed, .process = process,
    };
    c.stream = pw_stream_new_simple(loop, "surface-cursor-capture",
        pw_properties_new(PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY, "Capture",
                          PW_KEY_MEDIA_ROLE, "Screen", NULL), &events, &c);
    if (!c.output || !parent || !term || !interrupt || !c.stream) {
        c.failed = 1;
    } else {
        uint8_t storage[1024];
        struct spa_pod_builder b = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
        struct spa_video_info_raw info = {
            .format = SPA_VIDEO_FORMAT_BGRx, .size = SPA_RECTANGLE(1366, 768),
            .framerate = SPA_FRACTION(0, 1), .max_framerate = SPA_FRACTION(60, 1),
        };
        const struct spa_pod *param = spa_format_video_raw_build(&b, SPA_PARAM_EnumFormat, &info);
        if (pw_stream_connect(c.stream, PW_DIRECTION_INPUT, (uint32_t)node,
                              PW_STREAM_FLAG_AUTOCONNECT, &param, 1) < 0)
            c.failed = 1;
        else
            pw_main_loop_run(c.main);
    }
    if (c.ready_fd >= 0)
        close(c.ready_fd);
    c.closing = true;
    if (c.stream)
        pw_stream_destroy(c.stream);
    pw_main_loop_destroy(c.main);
    pw_deinit();
    return c.failed;
}

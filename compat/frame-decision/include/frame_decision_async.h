#ifndef FRAME_DECISION_ASYNC_H
#define FRAME_DECISION_ASYNC_H

#include "frame_decision_protocol_v3.h"

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FD_V3_CLIENT_ABI_VERSION 2u
#define FD_V3_DEFAULT_SOCKET_PATH \
    "/run/frame-decision/frame-decision-v3.sock"

typedef struct fd_v3_client fd_v3_client_t;

typedef struct {
    uint32_t abi_version;
    char socket_path[FD_SOCKET_PATH_SIZE];
    uint32_t connect_timeout_ms;
    uint32_t io_timeout_ms;
} fd_v3_client_config_t;

typedef struct {
    uint64_t request_id;
    uint64_t client_instance_id;
    uint64_t generation;
    uint64_t sequence;
    uint32_t channel_id;
} fd_v3_input_key_t;

typedef struct {
    fd_v3_input_key_t key;
    int64_t pts_us;
    uint64_t submit_monotonic_ns;
    uint64_t deadline_monotonic_ns;
    int dma_fd;
    uint64_t object_size;
    uint64_t format_modifier;
    uint32_t drm_fourcc;
    uint32_t width;
    uint32_t height;
    uint32_t plane_count;
    uint32_t offsets[2];
    uint32_t pitches[2];
} fd_v3_dmabuf_frame_t;

typedef enum {
    FD_V3_EVENT_NONE = 0,
    FD_V3_EVENT_INPUT_CONSUMED = 1,
    FD_V3_EVENT_RESULT = 2,
    FD_V3_EVENT_INPUT_STATE = 3
} fd_v3_event_type_t;

typedef struct {
    fd_v3_event_type_t type;
    fd_status_t status;
    uint64_t server_epoch;
    fd_v3_input_key_t key;
    fd_v3_input_state_t input_state;
    uint64_t consumed_monotonic_ns;
    uint64_t submit_monotonic_ns;
    uint64_t deadline_monotonic_ns;
    uint64_t completed_monotonic_ns;
    uint32_t width;
    uint32_t height;
    fd_result_t result;
} fd_v3_event_t;

void fd_v3_client_config_init(fd_v3_client_config_t *config);
int fd_v3_client_create(const fd_v3_client_config_t *config,
                        fd_v3_client_t **client,
                        char *error, size_t error_size);

/*
 * Configures the immutable target policy replayed after every HELLO_ACK.
 * Call this once after create and before the first connect. IDs must match
 * [a-z0-9][a-z0-9._-]{0,30}; unknown IDs are rejected by the daemon.
 */
int fd_v3_client_set_targets(fd_v3_client_t *client, uint32_t channel_id,
                             const char *const *target_ids,
                             uint32_t target_count);

/*
 * Except for interrupt, all client operations are serialized on one filter
 * I/O worker and never called from the media thread. Concurrent submit calls
 * are rejected. epoch_changed is set after reconnect to a new daemon epoch.
 * Target replay failure does not fail connect: the HELLO-established socket
 * and epoch remain usable so QUERY_INPUT can safely resolve an input made
 * uncertain by the preceding connection. A subsequent targeted submit
 * returns the replay error and never falls back to the legacy submit type.
 */
int fd_v3_client_connect(fd_v3_client_t *client, uint64_t *server_epoch,
                         int *epoch_changed);

/*
 * dma_fd is borrowed only for sendmsg(). The caller must retain the AVFrame
 * which owns the storage until a matching INPUT_CONSUMED/INPUT_STATE consumed
 * event arrives, or until reconnect proves that the daemon epoch changed.
 * A client permits only one submitted-but-not-consumed input at a time.
 */
int fd_v3_client_submit_dmabuf(fd_v3_client_t *client,
                               const fd_v3_dmabuf_frame_t *frame);

/* timeout_ms == 0 polls; UINT32_MAX uses the configured I/O timeout. */
int fd_v3_client_receive(fd_v3_client_t *client, fd_v3_event_t *event,
                         uint32_t timeout_ms);
int fd_v3_client_query_input(fd_v3_client_t *client,
                             const fd_v3_input_key_t *key);

/*
 * Safe to call from a shutdown thread; wakes connect, HELLO handshake and
 * receive waits. Interrupt is terminal for this client; destroy it after the
 * I/O worker exits and create a new client if processing must resume.
 */
void fd_v3_client_interrupt(fd_v3_client_t *client);

int fd_v3_client_has_uncertain_input(const fd_v3_client_t *client,
                                     fd_v3_input_key_t *key);
uint64_t fd_v3_client_server_epoch(const fd_v3_client_t *client);
uint64_t fd_v3_client_instance_id(const fd_v3_client_t *client);
void fd_v3_event_release(fd_v3_event_t *event);
void fd_v3_client_destroy(fd_v3_client_t *client);

#ifdef __cplusplus
}
#endif
#endif

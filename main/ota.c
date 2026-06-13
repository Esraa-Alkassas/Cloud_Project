#include <string.h>
#include <stdlib.h>
#include <stdbool.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_app_format.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "detools.h"
#include "cJSON.h"
#include "mbedtls/aes.h"
#include "wifi.h"
#include "ota.h"
#include "metrics.h"

static const char *TAG = "OTA_DELTA";

#define HTTP_PREFETCH_SIZE 10240   /* one HTTP-client buffer's worth per fill */

struct patch_state_t {
    const esp_partition_t   *old_partition;
    const esp_partition_t   *new_partition;
    esp_ota_handle_t         ota_handle;
    esp_http_client_handle_t http_client;
    int    old_read_offset;
    int    total_bytes_written;
    int    patch_bytes_read;
    int    content_length;
    int    last_pct;
    mbedtls_aes_context aes_ctx;
    size_t              nc_off;
    unsigned char       nonce_counter[16];
    unsigned char       stream_block[16];
    /* Pre-fetch buffer: read HTTP in HTTP_PREFETCH_SIZE chunks, serve detools 512 B at a time */
    uint8_t *prefetch_buf;
    size_t   prefetch_pos;
    size_t   prefetch_len;
    /* Stage 2: per-callback accumulating timers (~1 µs/call overhead from esp_timer_get_time pairs) */
    int64_t acc_http_us;
    int64_t acc_decrypt_us;
    int64_t acc_from_read_us;
    int64_t acc_flash_write_us;
    int64_t acc_seek_us;
    int64_t bytes_patch_in;
    int64_t bytes_flash_out;
    int64_t bytes_from_read;
    int32_t calls_read;
    int32_t calls_write;
    int32_t calls_from;
};

static int read_old_cb(void *arg_p, uint8_t *buf_p, size_t size)
{
    struct patch_state_t *s = (struct patch_state_t *)arg_p;
    int64_t t0 = metrics_now_us();
    int ret = (esp_partition_read(s->old_partition, s->old_read_offset, buf_p, size) == ESP_OK) ? 0 : -1;
    s->acc_from_read_us += metrics_now_us() - t0;
    if (ret == 0) {
        s->old_read_offset += size;
        s->bytes_from_read += size;
        s->calls_from++;
    }
    return ret;
}

static int seek_old_cb(void *arg_p, int offset)
{
    struct patch_state_t *s = (struct patch_state_t *)arg_p;
    int64_t t0 = metrics_now_us();
    s->old_read_offset = offset;
    s->acc_seek_us += metrics_now_us() - t0;
    return 0;
}

static int read_patch_cb(void *arg_p, uint8_t *buf_p, size_t size)
{
    struct patch_state_t *s = (struct patch_state_t *)arg_p;
    size_t total = 0;
    int64_t t0 = metrics_now_us();

    while (total < size) {
        /* Serve from the pre-fetch buffer first */
        if (s->prefetch_pos < s->prefetch_len) {
            size_t avail = s->prefetch_len - s->prefetch_pos;
            size_t take  = avail < (size - total) ? avail : (size - total);
            memcpy(buf_p + total, s->prefetch_buf + s->prefetch_pos, take);
            s->prefetch_pos += take;
            total           += take;
            continue;
        }

        /* Buffer drained: refill from HTTP in one large read */
        s->prefetch_pos = 0;
        s->prefetch_len = 0;
        int zero_retries = 0;
        while (s->prefetch_len == 0) {
            int r = esp_http_client_read(s->http_client,
                                         (char *)s->prefetch_buf,
                                         HTTP_PREFETCH_SIZE);
            if (r < 0) {
                s->acc_http_us += metrics_now_us() - t0;
                ESP_LOGE(TAG, "HTTP read error %d (patch so far: %d B)", r, s->patch_bytes_read);
                return -1;
            }
            if (r == 0) {
                /* r=0 can occur transiently between TLS records; retry up to ~1 s */
                if (++zero_retries > 100) {
                    s->acc_http_us += metrics_now_us() - t0;
                    ESP_LOGE(TAG, "HTTP read stalled after %d B", s->patch_bytes_read);
                    return -1;
                }
                vTaskDelay(pdMS_TO_TICKS(10));
            } else {
                s->prefetch_len = (size_t)r;
                zero_retries    = 0;
            }
        }
    }

    s->acc_http_us += metrics_now_us() - t0;
    t0 = metrics_now_us();
    mbedtls_aes_crypt_ctr(&s->aes_ctx, size, &s->nc_off,
                           s->nonce_counter, s->stream_block, buf_p, buf_p);
    s->acc_decrypt_us += metrics_now_us() - t0;
    s->patch_bytes_read += size;
    s->bytes_patch_in   += size;
    s->calls_read++;
    if (s->content_length > 0) {
        int pct = (s->patch_bytes_read * 100) / s->content_length;
        if (pct != s->last_pct && pct % 10 == 0) {
            ESP_LOGI(TAG, "Delta Patch Download: %d%%", pct);
            s->last_pct = pct;
        }
    }
    return 0;
}

static int write_new_cb(void *arg_p, const uint8_t *buf_p, size_t size)
{
    struct patch_state_t *s = (struct patch_state_t *)arg_p;
    int64_t t0 = metrics_now_us();
    int ret = (esp_ota_write(s->ota_handle, buf_p, size) == ESP_OK) ? 0 : -1;
    s->acc_flash_write_us += metrics_now_us() - t0;
    if (ret == 0) {
        s->total_bytes_written += size;
        s->bytes_flash_out     += size;
        s->calls_write++;
    }
    return ret;
}

static int get_rssi(void)
{
    wifi_ap_record_t info = {};
    esp_wifi_sta_get_ap_info(&info);
    return info.rssi;
}

void trigger_delta_ota_update(void)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();

    char fail_count_str[16] = "0";
    get_stored_value("ota_fail_cnt", fail_count_str, sizeof(fail_count_str));
    int fail_count = atoi(fail_count_str);

/* CONFIG_OTA_FORCE_FULL is not defined (just commented) when "n" in Kconfig */
#if CONFIG_OTA_FORCE_FULL
    bool force_full = true;
#else
    bool force_full = (fail_count > 0);
#endif

    char url[640];
    snprintf(url, sizeof(url), "%s?hash=%s%s&device=%s",
             CONFIG_OTA_API_URL, app_desc->version,
             force_full ? "&force_full=1" : "",
             CONFIG_METRICS_DEVICE_ID);

    ESP_LOGI(TAG, "Checking updates (Version: %s, Fails: %d)...", app_desc->version, fail_count);

    /* ---- Phase 1: broker check — time the full round trip ---- */
    int64_t t_check_start = metrics_now_us();

    esp_http_client_config_t cfg = {
        .url = url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 15000,
        .buffer_size = 10240,
        .buffer_size_tx = 4096,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    esp_http_client_set_header(client, "Accept", "application/json");

    if (esp_http_client_open(client, 0) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to connect to update broker");
        esp_http_client_cleanup(client);
        return;
    }

    esp_http_client_fetch_headers(client);
    int status_code = esp_http_client_get_status_code(client);
    if (status_code != 200) {
        ESP_LOGE(TAG, "Broker returned HTTP %d", status_code);
        esp_http_client_cleanup(client);
        return;
    }

    char *res_buf = calloc(1, 4096);
    if (!res_buf) {
        esp_http_client_cleanup(client);
        return;
    }

    int total_read = 0;
    while (total_read < 4095) {
        int r = esp_http_client_read(client, res_buf + total_read, 4095 - total_read);
        if (r <= 0) break;
        total_read += r;
    }
    esp_http_client_cleanup(client);
    int64_t t_check_rtt_ms = (metrics_now_us() - t_check_start) / 1000;

    if (total_read <= 0) {
        ESP_LOGE(TAG, "Empty response from broker");
        free(res_buf);
        return;
    }
    ESP_LOGD(TAG, "Broker Response: %s", res_buf);

    cJSON *json = cJSON_Parse(res_buf);
    free(res_buf);
    if (!json) {
        ESP_LOGE(TAG, "Failed to parse broker JSON");
        return;
    }

    bool update_available = cJSON_IsTrue(cJSON_GetObjectItem(json, "update_available"));
    cJSON *url_item      = cJSON_GetObjectItem(json, "download_url");
    cJSON *is_delta_item = cJSON_GetObjectItem(json, "is_delta");
    cJSON *ver_item      = cJSON_GetObjectItem(json, "latest_version");
    bool   is_delta      = cJSON_IsTrue(is_delta_item);

    int rssi = get_rssi();
    metrics_emit("ota_check",
                 "\"rtt_ms\":%" PRId64 ",\"http\":%d,\"update\":%d,\"is_delta\":%d"
                 ",\"rssi\":%d,\"heap_free\":%" PRIu32,
                 t_check_rtt_ms, status_code, update_available ? 1 : 0, is_delta ? 1 : 0,
                 rssi, esp_get_free_heap_size());

    if (!update_available) {
        ESP_LOGI(TAG, "Firmware is up-to-date.");
        store_value("ota_fail_cnt", "0");
        cJSON_Delete(json);
        return;
    }

    if (!url_item || !url_item->valuestring) {
        ESP_LOGE(TAG, "Missing download URL in response");
        cJSON_Delete(json);
        return;
    }

    char *dl_url  = url_item->valuestring;
    const char *fw_to = (ver_item && ver_item->valuestring) ? ver_item->valuestring : "unknown";
    ESP_LOGI(TAG, "Update Found! Strategy: %s  Target: %s", is_delta ? "Delta" : "Full", fw_to);

    /* ---- Phase 2: update — declare all timing vars before any goto ---- */
    int  apply_res          = -1;
    bool ota_begin_ok       = false;
    bool finalize_ok        = false;
    int64_t t_connect_ms    = 0;
    int64_t t_apply_wall_ms = 0;
    int64_t t_finalize_ms   = 0;
    uint32_t heap_min_during = 0;

    int64_t t_ota_start = metrics_now_us();
    metrics_emit("ota_start",
                 "\"is_delta\":%d,\"fw_to\":\"%s\",\"force_full\":%d"
                 ",\"rssi\":%d,\"heap_min\":%" PRIu32,
                 is_delta ? 1 : 0, fw_to, force_full ? 1 : 0,
                 rssi, esp_get_minimum_free_heap_size());

    struct patch_state_t state = {
        .old_partition = esp_ota_get_running_partition(),
        .new_partition = esp_ota_get_next_update_partition(NULL),
        .last_pct      = -1,
    };

    state.prefetch_buf = malloc(HTTP_PREFETCH_SIZE);
    if (!state.prefetch_buf) {
        ESP_LOGE(TAG, "Failed to allocate HTTP prefetch buffer");
        goto update_done;
    }

    unsigned char key[16] = "1234567890123456";
    unsigned char iv[16]  = "abcdefghijklmnop";
    mbedtls_aes_init(&state.aes_ctx);
    mbedtls_aes_setkey_enc(&state.aes_ctx, key, 128);
    memcpy(state.nonce_counter, iv, 16);
    state.nc_off = 0;

    esp_http_client_config_t s3_cfg = {
        .url = dl_url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .buffer_size = 10240,
        .buffer_size_tx = 4096,
        .timeout_ms = 30000,
    };
    state.http_client = esp_http_client_init(&s3_cfg);
    if (!state.http_client) {
        goto update_done;
    }

    {
        int64_t t_conn_start = metrics_now_us();
        if (esp_http_client_open(state.http_client, 0) != ESP_OK) {
            ESP_LOGE(TAG, "Failed to open S3 download connection");
            goto update_done;
        }
        esp_http_client_fetch_headers(state.http_client);
        t_connect_ms = (metrics_now_us() - t_conn_start) / 1000;
    }

    state.content_length = esp_http_client_get_content_length(state.http_client);
    ESP_LOGI(TAG, "Download size: %d bytes, Partition size: %" PRIu32 " bytes",
             state.content_length, state.new_partition->size);

    if (state.content_length > (int)state.new_partition->size && !is_delta) {
        ESP_LOGE(TAG, "Firmware too large for partition!");
        goto update_done;
    }

    if (esp_ota_begin(state.new_partition, OTA_SIZE_UNKNOWN, &state.ota_handle) != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin failed");
        goto update_done;
    }
    ota_begin_ok = true;
    heap_min_during = esp_get_minimum_free_heap_size();

    {
        int64_t t_apply_wall_start = metrics_now_us();
        if (is_delta) {
            if (state.content_length <= 0) {
                ESP_LOGE(TAG, "Delta update requires Content-Length");
            } else {
                apply_res = detools_apply_patch_callbacks(read_old_cb, seek_old_cb,
                                                          read_patch_cb, state.content_length,
                                                          write_new_cb, &state);
            }
        } else {
            char *buf = malloc(2048);
            apply_res = 0;
            while (1) {
                int64_t t0 = metrics_now_us();
                int r = esp_http_client_read(state.http_client, buf, 2048);
                state.acc_http_us += metrics_now_us() - t0;
                if (r < 0) { apply_res = -1; break; }
                if (r == 0) break;
                state.bytes_patch_in += r;
                t0 = metrics_now_us();
                mbedtls_aes_crypt_ctr(&state.aes_ctx, r, &state.nc_off,
                                      state.nonce_counter, state.stream_block,
                                      (unsigned char *)buf, (unsigned char *)buf);
                state.acc_decrypt_us += metrics_now_us() - t0;
                if (write_new_cb(&state, (uint8_t *)buf, r) != 0) { apply_res = -1; break; }
            }
            free(buf);
        }
        t_apply_wall_ms = (metrics_now_us() - t_apply_wall_start) / 1000;
    }

    if (apply_res >= 0) {
        int64_t t_finalize_start = metrics_now_us();
        if (esp_ota_end(state.ota_handle) == ESP_OK) {
            esp_ota_set_boot_partition(state.new_partition);
            finalize_ok = true;
        }
        t_finalize_ms    = (metrics_now_us() - t_finalize_start) / 1000;
        heap_min_during  = esp_get_minimum_free_heap_size();
        ota_begin_ok     = false; /* handle consumed; don't abort below */
    }

update_done: {
    int64_t t_total_ms = (metrics_now_us() - t_ota_start) / 1000;
    metrics_emit("ota_summary",
                 "\"ok\":%d,\"err\":%d,\"is_delta\":%d"
                 ",\"t_total_ms\":%" PRId64 ",\"t_connect_ms\":%" PRId64
                 ",\"t_apply_wall_ms\":%" PRId64
                 ",\"t_http\":%" PRId64 ",\"t_decrypt\":%" PRId64
                 ",\"t_from_read\":%" PRId64 ",\"t_flash\":%" PRId64 ",\"t_seek\":%" PRId64
                 ",\"t_finalize_ms\":%" PRId64
                 ",\"b_patch\":%" PRId64 ",\"b_flash\":%" PRId64 ",\"b_from\":%" PRId64
                 ",\"calls_r\":%" PRId32 ",\"calls_w\":%" PRId32
                 ",\"rssi\":%d,\"heap_min_during\":%" PRIu32,
                 finalize_ok ? 1 : 0, apply_res, is_delta ? 1 : 0,
                 t_total_ms, t_connect_ms,
                 t_apply_wall_ms,
                 state.acc_http_us / 1000, state.acc_decrypt_us / 1000,
                 state.acc_from_read_us / 1000, state.acc_flash_write_us / 1000,
                 state.acc_seek_us / 1000,
                 t_finalize_ms,
                 state.bytes_patch_in, state.bytes_flash_out, state.bytes_from_read,
                 state.calls_read, state.calls_write,
                 rssi, heap_min_during);

    if (finalize_ok) {
        store_value("ota_fail_cnt", "0");
        ESP_LOGI(TAG, "OTA Success! Rebooting...");
        nvs_handle_t nh;
        if (nvs_open("provision", NVS_READWRITE, &nh) == ESP_OK) {
            nvs_set_u8(nh, "ota_boot_pend", 1);
            nvs_commit(nh);
            nvs_close(nh);
        }
        metrics_emit("ota_reboot", "");
        vTaskDelay(pdMS_TO_TICKS(100)); /* flush UART before reset */
        esp_restart();
    } else {
        ESP_LOGE(TAG, "OTA Apply Failed (%d). Marking failure in NVS.", apply_res);
        char next_fail[16];
        snprintf(next_fail, sizeof(next_fail), "%d", fail_count + 1);
        store_value("ota_fail_cnt", next_fail);
        if (ota_begin_ok) {
            esp_ota_abort(state.ota_handle);
        }
    }

    if (state.http_client) {
        esp_http_client_cleanup(state.http_client);
    }
    free(state.prefetch_buf);
    mbedtls_aes_free(&state.aes_ctx);
    cJSON_Delete(json);
    }
}

void ota_task(void *pv)
{
    vTaskDelay(pdMS_TO_TICKS(10000));
    while (1) {
        trigger_delta_ota_update();
        vTaskDelay(pdMS_TO_TICKS(CONFIG_OTA_POLL_INTERVAL_MS));
    }
}

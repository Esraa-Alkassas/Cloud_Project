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
#include "nvs_flash.h"
#include "nvs.h"
#include "detools.h"
#include "cJSON.h"
#include "mbedtls/aes.h"
#include "wifi.h"
#include "ota.h"
#include "metrics.h"

static const char *TAG = "OTA_DELTA";

struct patch_state_t {
    const esp_partition_t *old_partition;
    const esp_partition_t *new_partition;
    esp_ota_handle_t        ota_handle;
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
};

static int read_old_cb(void *arg_p, uint8_t *buf_p, size_t size)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    if (esp_partition_read(state->old_partition, state->old_read_offset, buf_p, size) != ESP_OK)
        return -1;
    state->old_read_offset += size;
    return 0;
}

static int seek_old_cb(void *arg_p, int offset)
{
    ((struct patch_state_t *)arg_p)->old_read_offset = offset;
    return 0;
}

static int read_patch_cb(void *arg_p, uint8_t *buf_p, size_t size)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    size_t total = 0;
    while (total < size) {
        int r = esp_http_client_read(state->http_client, (char *)(buf_p + total), size - total);
        if (r <= 0)
            return -1;
        total += r;
    }
    mbedtls_aes_crypt_ctr(&state->aes_ctx, size, &state->nc_off,
                           state->nonce_counter, state->stream_block, buf_p, buf_p);
    state->patch_bytes_read += size;
    if (state->content_length > 0) {
        int pct = (state->patch_bytes_read * 100) / state->content_length;
        if (pct != state->last_pct && pct % 10 == 0) {
            ESP_LOGI(TAG, "Delta Patch Download: %d%%", pct);
            state->last_pct = pct;
        }
    }
    return 0;
}

static int write_new_cb(void *arg_p, const uint8_t *buf_p, size_t size)
{
    struct patch_state_t *state = (struct patch_state_t *)arg_p;
    if (esp_ota_write(state->ota_handle, buf_p, size) != ESP_OK)
        return -1;
    state->total_bytes_written += size;
    return 0;
}

void trigger_delta_ota_update(void)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();

    char fail_count_str[16] = "0";
    get_stored_value("ota_fail_cnt", fail_count_str, sizeof(fail_count_str));
    int fail_count = atoi(fail_count_str);

    char url[640];
    snprintf(url, sizeof(url), "%s?hash=%s%s&device=%s",
             CONFIG_OTA_API_URL, app_desc->version,
             (fail_count > 0) ? "&force_full=1" : "",
             CONFIG_METRICS_DEVICE_ID);

    ESP_LOGI(TAG, "Checking updates (Version: %s, Fails: %d)...", app_desc->version, fail_count);

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
        if (r <= 0)
            break;
        total_read += r;
    }
    esp_http_client_cleanup(client);

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

    if (cJSON_IsTrue(cJSON_GetObjectItem(json, "update_available"))) {
        cJSON *url_item      = cJSON_GetObjectItem(json, "download_url");
        cJSON *is_delta_item = cJSON_GetObjectItem(json, "is_delta");

        if (!url_item || !url_item->valuestring) {
            ESP_LOGE(TAG, "Missing download URL in response");
            cJSON_Delete(json);
            return;
        }

        char *dl_url  = url_item->valuestring;
        bool  is_delta = cJSON_IsTrue(is_delta_item);
        ESP_LOGI(TAG, "Update Found! Strategy: %s", is_delta ? "Delta" : "Full");

        struct patch_state_t state = {
            .old_partition  = esp_ota_get_running_partition(),
            .new_partition  = esp_ota_get_next_update_partition(NULL),
            .last_pct       = -1,
            .patch_bytes_read = 0,
        };

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

        if (esp_http_client_open(state.http_client, 0) == ESP_OK) {
            esp_http_client_fetch_headers(state.http_client);
            state.content_length = esp_http_client_get_content_length(state.http_client);

            ESP_LOGI(TAG, "Download size: %d bytes, Partition size: %" PRIu32 " bytes",
                     state.content_length, state.new_partition->size);

            if (state.content_length > (int)state.new_partition->size && !is_delta) {
                ESP_LOGE(TAG, "Firmware too large for partition!");
                esp_http_client_cleanup(state.http_client);
                cJSON_Delete(json);
                return;
            }

            if (esp_ota_begin(state.new_partition, OTA_SIZE_UNKNOWN, &state.ota_handle) == ESP_OK) {
                int res = -1;
                if (is_delta) {
                    if (state.content_length <= 0) {
                        ESP_LOGE(TAG, "Delta update requires Content-Length");
                    } else {
                        res = detools_apply_patch_callbacks(read_old_cb, seek_old_cb,
                                                            read_patch_cb, state.content_length,
                                                            write_new_cb, &state);
                    }
                } else {
                    char *buf = malloc(2048);
                    res = 0;
                    while (1) {
                        int r = esp_http_client_read(state.http_client, buf, 2048);
                        if (r < 0) { res = -1; break; }
                        if (r == 0) break;
                        mbedtls_aes_crypt_ctr(&state.aes_ctx, r, &state.nc_off,
                                              state.nonce_counter, state.stream_block,
                                              (unsigned char *)buf, (unsigned char *)buf);
                        if (write_new_cb(&state, (uint8_t *)buf, r) != 0) { res = -1; break; }
                    }
                    free(buf);
                }

                if (res >= 0 && esp_ota_end(state.ota_handle) == ESP_OK) {
                    store_value("ota_fail_cnt", "0");
                    esp_ota_set_boot_partition(state.new_partition);
                    ESP_LOGI(TAG, "OTA Success! Rebooting...");

                    /* Mark that the next boot follows an OTA — read by metrics_init */
                    nvs_handle_t nh;
                    if (nvs_open("provision", NVS_READWRITE, &nh) == ESP_OK) {
                        nvs_set_u8(nh, "ota_boot_pend", 1);
                        nvs_commit(nh);
                        nvs_close(nh);
                    }

                    vTaskDelay(pdMS_TO_TICKS(2000));
                    esp_restart();
                } else {
                    ESP_LOGE(TAG, "OTA Apply Failed (%d). Marking failure in NVS.", res);
                    char next_fail[16];
                    snprintf(next_fail, sizeof(next_fail), "%d", fail_count + 1);
                    store_value("ota_fail_cnt", next_fail);
                    esp_ota_abort(state.ota_handle);
                }
            }
        }
        esp_http_client_cleanup(state.http_client);
        mbedtls_aes_free(&state.aes_ctx);
    } else {
        ESP_LOGI(TAG, "Firmware is up-to-date.");
        store_value("ota_fail_cnt", "0");
    }

    cJSON_Delete(json);
}

void ota_task(void *pv)
{
    vTaskDelay(pdMS_TO_TICKS(10000));
    while (1) {
        trigger_delta_ota_update();
        vTaskDelay(pdMS_TO_TICKS(CONFIG_OTA_POLL_INTERVAL_MS));
    }
}

#include <string.h>
#include <stdlib.h>
#include <stdbool.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_app_format.h"
#include "esp_partition.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_wifi.h"
#include "driver/gpio.h"
#include "detools.h"
#include "cJSON.h"
#include "mbedtls/aes.h"
#include "mqtt_client.h"

static const char *TAG = "OTA_DELTA";

#define API_GATEWAY_URL "https://kavm965brg.execute-api.us-east-1.amazonaws.com/check"
#define TB_MQTT_URL "mqtt://mqtt.eu.thingsboard.cloud"
#define LED_PIN_RED GPIO_NUM_21
#define LED_PIN_GREEN GPIO_NUM_23
#define LED_PIN_BLUE GPIO_NUM_22

static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0
esp_mqtt_client_handle_t mqtt_client = NULL;
static int s_retry_num = 0;

// OTA Global State
static bool s_update_available = false;
static bool s_ota_trigger_received = false;
static char s_pending_dl_url[1024] = {0};
static bool s_pending_is_delta = false;
static int s_pending_size = 0;
static char s_latest_ver_str[64] = {0};

esp_err_t get_stored_value(const char *key, char *out_val, size_t max_len)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("provision", NVS_READONLY, &handle);
    if (err != ESP_OK)
        return err;
    err = nvs_get_str(handle, key, out_val, &max_len);
    nvs_close(handle);
    return err;
}

esp_err_t store_value(const char *key, const char *val)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("provision", NVS_READWRITE, &handle);
    if (err != ESP_OK)
        return err;
    err = nvs_set_str(handle, key, val);
    if (err == ESP_OK)
        nvs_commit(handle);
    nvs_close(handle);
    return err;
}

/* WiFi event handler */
static void event_handler(void *arg, esp_event_base_t event_base,
                          int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START)
    {
        esp_wifi_connect();
    }
    else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED)
    {
        wifi_event_sta_disconnected_t *event = (wifi_event_sta_disconnected_t *)event_data;
        ESP_LOGW(TAG, "WiFi Disconnected (Reason: %d). Retry %d/%d", event->reason, s_retry_num, CONFIG_ESP_MAXIMUM_RETRY);

        if (s_retry_num < CONFIG_ESP_MAXIMUM_RETRY)
        {
            esp_wifi_connect();
            s_retry_num++;
        }
        else
        {
            ESP_LOGE(TAG, "WiFi connection failed after max retries.");
        }
    }
    else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP)
    {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP:" IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        if (wifi_event_group)
        {
            xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
        }

        // Persistence: Save working credentials to NVS
        wifi_config_t conf;
        if (esp_wifi_get_config(WIFI_IF_STA, &conf) == ESP_OK)
        {
            store_value("wifi_ssid", (char *)conf.sta.ssid);
            store_value("wifi_pass", (char *)conf.sta.password);
        }
    }
}

void wifi_init_sta(void)
{
    wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &event_handler,
                                                        NULL,
                                                        &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_GOT_IP,
                                                        &event_handler,
                                                        NULL,
                                                        &instance_got_ip));

    wifi_config_t wifi_config = {
        .sta = {
#if CONFIG_ESP_WIFI_AUTH_OPEN
            .threshold.authmode = WIFI_AUTH_OPEN,
#elif CONFIG_ESP_WIFI_AUTH_WEP
            .threshold.authmode = WIFI_AUTH_WEP,
#elif CONFIG_ESP_WIFI_AUTH_WPA_PSK
            .threshold.authmode = WIFI_AUTH_WPA_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WPA2_PSK
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WPA_WPA2_PSK
            .threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WPA3_PSK
            .threshold.authmode = WIFI_AUTH_WPA3_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WPA2_WPA3_PSK
            .threshold.authmode = WIFI_AUTH_WPA2_WPA3_PSK,
#elif CONFIG_ESP_WIFI_AUTH_WAPI_PSK
            .threshold.authmode = WIFI_AUTH_WAPI_PSK,
#endif
#if CONFIG_ESP_WPA3_SAE_PWE_HUNT_AND_PECK
            .sae_pwe_h2e = WPA3_SAE_PWE_HUNT_AND_PECK,
#elif CONFIG_ESP_WPA3_SAE_PWE_HASH_TO_ELEMENT
            .sae_pwe_h2e = WPA3_SAE_PWE_HASH_TO_ELEMENT,
#elif CONFIG_ESP_WPA3_SAE_PWE_BOTH
            .sae_pwe_h2e = WPA3_SAE_PWE_BOTH,
#endif
        },
    };

    // Try to load credentials from NVS, fallback to Kconfig
    char nvs_ssid[32] = {0};
    char nvs_pass[64] = {0};
    if (get_stored_value("wifi_ssid", nvs_ssid, sizeof(nvs_ssid)) == ESP_OK &&
        get_stored_value("wifi_pass", nvs_pass, sizeof(nvs_pass)) == ESP_OK)
    {
        ESP_LOGI(TAG, "Using WiFi credentials from NVS");
        strncpy((char *)wifi_config.sta.ssid, nvs_ssid, sizeof(wifi_config.sta.ssid));
        strncpy((char *)wifi_config.sta.password, nvs_pass, sizeof(wifi_config.sta.password));
    }
    else
    {
        ESP_LOGI(TAG, "Using WiFi credentials from Kconfig");
        strncpy((char *)wifi_config.sta.ssid, CONFIG_ESP_WIFI_SSID, sizeof(wifi_config.sta.ssid));
        strncpy((char *)wifi_config.sta.password, CONFIG_ESP_WIFI_PASSWORD, sizeof(wifi_config.sta.password));
    }
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "wifi_init_sta finished.");
}

struct patch_state_t
{
    const esp_partition_t *old_partition;
    const esp_partition_t *new_partition;
    esp_ota_handle_t ota_handle;
    esp_http_client_handle_t http_client;
    int old_read_offset;
    int total_bytes_written;
    int patch_bytes_read; // NEW: track download progress
    int content_length;
    int last_pct;
    mbedtls_aes_context aes_ctx;
    size_t nc_off;
    unsigned char nonce_counter[16];
    unsigned char stream_block[16];
};

// ==========================================
// UTILS
// ==========================================

void send_tb_log(const char *msg)
{
    if (mqtt_client == NULL)
        return;
    char payload[256];
    snprintf(payload, sizeof(payload), "{\"tb_log\":\"%s\"}", msg);
    esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", payload, 0, 1, 0);
    ESP_LOGI("TB_LOG", "%s", msg);
}

void send_led_telemetry(int r, int g, int b)
{
    if (mqtt_client == NULL)
        return;
    char payload[128];
    snprintf(payload, sizeof(payload), "{\"red_led\":%d, \"green_led\":%d, \"blue_led\":%d}", r, g, b);
    esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", payload, 0, 1, 0);
}

// ==========================================
// TASKS
// ==========================================
void led_blink_task(void *pvParameter)
{
    gpio_reset_pin(LED_PIN_RED);
    gpio_reset_pin(LED_PIN_GREEN);
    gpio_reset_pin(LED_PIN_BLUE);
    gpio_set_direction(LED_PIN_RED, GPIO_MODE_OUTPUT);
    gpio_set_direction(LED_PIN_GREEN, GPIO_MODE_OUTPUT);
    gpio_set_direction(LED_PIN_BLUE, GPIO_MODE_OUTPUT);
    while (1)
    {
        gpio_set_level(LED_PIN_RED, 1);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE, 1);
        send_led_telemetry(1, 1, 1);
        vTaskDelay(pdMS_TO_TICKS(500));
        gpio_set_level(LED_PIN_RED, 0);
        gpio_set_level(LED_PIN_GREEN, 0);
        gpio_set_level(LED_PIN_BLUE, 0);
        send_led_telemetry(0, 0, 0);
        vTaskDelay(pdMS_TO_TICKS(1000));
        gpio_set_level(LED_PIN_RED, 1);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE, 1);
        send_led_telemetry(1, 1, 1);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void version_print_task(void *pv)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();
    while (1)
    {
        ESP_LOGI(TAG, "Active Version: %s", app_desc->version);
        if (mqtt_client)
        {
            char p[256];
            snprintf(p, sizeof(p), "{\"fw_version\":\"%s\",\"update_available\":%s,\"strategy\":\"%s\",\"size\":%d}", 
                     app_desc->version, 
                     s_update_available ? "true" : "false",
                     s_update_available ? (s_pending_is_delta ? "Delta" : "Full") : "N/A",
                     s_pending_size);
            esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", p, 0, 1, 0);
        }
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}

// ==========================================
// MQTT
// ==========================================
static void mqtt_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;
    switch ((esp_mqtt_event_id_t)event_id)
    {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT Connected to ThingsBoard");
        esp_mqtt_client_subscribe(mqtt_client, "v1/devices/me/rpc/request/+", 1);
        send_tb_log("Connected to ThingsBoard MQTT Broker");
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT Disconnected");
        break;
    case MQTT_EVENT_DATA:
        ESP_LOGI(TAG, "MQTT DATA Received. Topic: %.*s", event->topic_len, event->topic);
        if (strncmp(event->topic, "v1/devices/me/rpc/request/", 26) == 0)
        {
            cJSON *root = cJSON_ParseWithLength(event->data, event->data_len);
            if (root)
            {
                cJSON *method = cJSON_GetObjectItem(root, "method");
                if (method && strcmp(method->valuestring, "start_ota") == 0)
                {
                    if (s_update_available)
                    {
                        send_tb_log("RPC Received: Starting OTA update...");
                        s_ota_trigger_received = true;
                    }
                    else
                    {
                        send_tb_log("RPC Received: No update available to start.");
                    }
                }
                cJSON_Delete(root);
            }
        }
        break;
    default:
        break;
    }
}

static void mqtt_app_start(void)
{
    char token[64] = {0};
    if (get_stored_value("mqtt_token", token, sizeof(token)) != ESP_OK)
    {
        strncpy(token, CONFIG_THINGSBOARD_MQTT_ACCESS_TOKEN, sizeof(token) - 1);
        store_value("mqtt_token", token);
    }
    esp_mqtt_client_config_t mqtt_cfg = {.broker.address.uri = TB_MQTT_URL, .credentials.username = token};
    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(mqtt_client);
}

// ==========================================
// OTA & DETOOLS
// ==========================================
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
    while (total < size)
    {
        int r = esp_http_client_read(state->http_client, (char *)(buf_p + total), size - total);
        if (r <= 0)
            return -1;
        total += r;
    }

    // Decrypt the payload on-the-fly
    mbedtls_aes_crypt_ctr(&state->aes_ctx, size, &state->nc_off, state->nonce_counter, state->stream_block, buf_p, buf_p);

    // Progress based on DOWNLOAD bytes
    state->patch_bytes_read += size;
    if (state->content_length > 0)
    {
        int pct = (state->patch_bytes_read * 100) / state->content_length;
        if (pct != state->last_pct && pct % 10 == 0)
        {
            char log_msg[64];
            snprintf(log_msg, sizeof(log_msg), "Download Progress: %d%%", pct);
            send_tb_log(log_msg);
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

    // Reset update state if not triggered
    if (!s_ota_trigger_received) {
        s_update_available = false;
        s_pending_size = 0;
    }

    // Failure tracking for Full OTA fallback
    char fail_count_str[16] = "0";
    get_stored_value("ota_fail_cnt", fail_count_str, sizeof(fail_count_str));
    int fail_count = atoi(fail_count_str);

    char url[640];
    snprintf(url, sizeof(url), "%s?hash=%s%s", API_GATEWAY_URL, app_desc->version, (fail_count > 0) ? "&force_full=1" : "");

    ESP_LOGI(TAG, "Checking updates (Version: %s, Fails: %d)...", app_desc->version, fail_count);

    esp_http_client_config_t cfg = {
        .url = url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 15000,
        .buffer_size = 10240, 
        .buffer_size_tx = 4096};
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    esp_http_client_set_header(client, "Accept", "application/json");
    if (esp_http_client_open(client, 0) != ESP_OK)
    {
        ESP_LOGE(TAG, "Failed to connect to update broker");
        esp_http_client_cleanup(client);
        return;
    }

    esp_http_client_fetch_headers(client);
    int status_code = esp_http_client_get_status_code(client);
    if (status_code != 200)
    {
        ESP_LOGE(TAG, "Broker returned HTTP %d", status_code);
        esp_http_client_cleanup(client);
        return;
    }

    char *res_buf = calloc(1, 4096);
    if (!res_buf)
    {
        esp_http_client_cleanup(client);
        return;
    }

    int total_read = 0;
    while (total_read < 4095)
    {
        int r = esp_http_client_read(client, res_buf + total_read, 4095 - total_read);
        if (r <= 0)
            break;
        total_read += r;
    }
    esp_http_client_cleanup(client);

    if (total_read <= 0)
    {
        ESP_LOGE(TAG, "Empty response from broker");
        free(res_buf);
        return;
    }

    cJSON *json = cJSON_Parse(res_buf);
    free(res_buf);
    if (!json)
    {
        ESP_LOGE(TAG, "Failed to parse broker JSON");
        return;
    }

    if (cJSON_IsTrue(cJSON_GetObjectItem(json, "update_available")))
    {
        cJSON *url_item = cJSON_GetObjectItem(json, "download_url");
        cJSON *is_delta_item = cJSON_GetObjectItem(json, "is_delta");
        cJSON *latest_ver_item = cJSON_GetObjectItem(json, "latest_version");

        if (!url_item || !url_item->valuestring)
        {
            ESP_LOGE(TAG, "Missing download URL in response");
            cJSON_Delete(json);
            return;
        }

        s_update_available = true;
        s_pending_is_delta = cJSON_IsTrue(is_delta_item);
        strncpy(s_pending_dl_url, url_item->valuestring, sizeof(s_pending_dl_url) - 1);
        if (latest_ver_item && latest_ver_item->valuestring) {
            strncpy(s_latest_ver_str, latest_ver_item->valuestring, sizeof(s_latest_ver_str) - 1);
        }

        // Phase 1: Get File Size (Headers Only)
        esp_http_client_config_t size_cfg = {
            .url = s_pending_dl_url,
            .crt_bundle_attach = esp_crt_bundle_attach,
            .timeout_ms = 15000};
        esp_http_client_handle_t size_client = esp_http_client_init(&size_cfg);
        if (esp_http_client_open(size_client, 0) == ESP_OK) {
            esp_http_client_fetch_headers(size_client);
            s_pending_size = esp_http_client_get_content_length(size_client);
            esp_http_client_close(size_client);
        }
        esp_http_client_cleanup(size_client);

        char log_buf[256];
        snprintf(log_buf, sizeof(log_buf), "Update Pending: %s (%d bytes). Strategy: %s. Awaiting User Deployment...", 
                 s_latest_ver_str, s_pending_size, s_pending_is_delta ? "Delta" : "Full");
        send_tb_log(log_buf);

        // Update ThingsBoard immediately with new pending status
        char p[256];
        snprintf(p, sizeof(p), "{\"update_available\":true,\"strategy\":\"%s\",\"size\":%d,\"latest_ver\":\"%s\"}", 
                 s_pending_is_delta ? "Delta" : "Full", s_pending_size, s_latest_ver_str);
        esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", p, 0, 1, 0);

        // Phase 2: Wait for RPC trigger
        while (!s_ota_trigger_received) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }

        // Start Timing
        uint32_t start_time = xTaskGetTickCount();
        send_tb_log("Deploying SW update now...");

        struct patch_state_t state = {
            .old_partition = esp_ota_get_running_partition(),
            .new_partition = esp_ota_get_next_update_partition(NULL),
            .last_pct = -1,
            .patch_bytes_read = 0,
            .content_length = s_pending_size};

        // Initialize AES-CTR
        unsigned char key[16] = "1234567890123456"; 
        unsigned char iv[16] = "abcdefghijklmnop";  
        mbedtls_aes_init(&state.aes_ctx);
        mbedtls_aes_setkey_enc(&state.aes_ctx, key, 128);
        memcpy(state.nonce_counter, iv, 16);
        state.nc_off = 0;

        esp_http_client_config_t s3_cfg = {
            .url = s_pending_dl_url,
            .crt_bundle_attach = esp_crt_bundle_attach,
            .buffer_size = 10240, 
            .buffer_size_tx = 4096,
            .timeout_ms = 30000};
        state.http_client = esp_http_client_init(&s3_cfg);

        if (esp_http_client_open(state.http_client, 0) == ESP_OK)
        {
            esp_http_client_fetch_headers(state.http_client);
            if (esp_ota_begin(state.new_partition, OTA_SIZE_UNKNOWN, &state.ota_handle) == ESP_OK)
            {
                int res = -1;
                if (s_pending_is_delta)
                {
                    res = detools_apply_patch_callbacks(read_old_cb, seek_old_cb, read_patch_cb, state.content_length, write_new_cb, &state);
                }
                else
                {
                    char *buf = malloc(2048);
                    res = 0;
                    while (1)
                    {
                        int r = esp_http_client_read(state.http_client, buf, 2048);
                        if (r < 0) { res = -1; break; }
                        if (r == 0) break;
                        mbedtls_aes_crypt_ctr(&state.aes_ctx, r, &state.nc_off, state.nonce_counter, state.stream_block, (unsigned char *)buf, (unsigned char *)buf);
                        if (write_new_cb(&state, (uint8_t *)buf, r) != 0) { res = -1; break; }
                    }
                    free(buf);
                }

                if (res >= 0 && esp_ota_end(state.ota_handle) == ESP_OK)
                {
                    uint32_t duration_ms = (xTaskGetTickCount() - start_time) * portTICK_PERIOD_MS;
                    char final_p[128];
                    snprintf(final_p, sizeof(final_p), "{\"flash_time_ms\":%" PRIu32 ", \"update_available\":false}", duration_ms);
                    esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", final_p, 0, 1, 0);
                    
                    char log_success[128];
                    snprintf(log_success, sizeof(log_success), "OTA Success in %" PRIu32 " ms! Rebooting to %s...", duration_ms, s_latest_ver_str);
                    send_tb_log(log_success);
                    
                    store_value("ota_fail_cnt", "0");
                    esp_ota_set_boot_partition(state.new_partition);
                    vTaskDelay(pdMS_TO_TICKS(3000));
                    esp_restart();
                }
                else
                {
                    send_tb_log("OTA Apply Failed! Marking for retry...");
                    char next_fail[16];
                    snprintf(next_fail, sizeof(next_fail), "%d", fail_count + 1);
                    store_value("ota_fail_cnt", next_fail);
                    esp_ota_abort(state.ota_handle);
                }
            }
        }
        esp_http_client_cleanup(state.http_client);
        mbedtls_aes_free(&state.aes_ctx);
        s_ota_trigger_received = false; // Reset trigger on failure
    }
    else
    {
        ESP_LOGI(TAG, "Firmware is up-to-date.");
        s_update_available = false;
        store_value("ota_fail_cnt", "0"); 
    }

    cJSON_Delete(json);
}

void ota_task(void *pv)
{
    vTaskDelay(pdMS_TO_TICKS(10000));
    while (1)
    {
        trigger_delta_ota_update();
        vTaskDelay(pdMS_TO_TICKS(31000));
    }
}

void app_main(void)
{
    esp_log_level_set("wifi", ESP_LOG_ERROR);
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND)
    {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    if (esp_ota_get_running_partition()->subtype != ESP_PARTITION_SUBTYPE_APP_FACTORY)
        esp_ota_mark_app_valid_cancel_rollback();

    wifi_init_sta();
    xTaskCreate(&led_blink_task, "led_task", 3072, NULL, 5, NULL);
    xTaskCreate(&version_print_task, "ver_task", 3072, NULL, 5, NULL);
    xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    mqtt_app_start();
    xTaskCreate(&ota_task, "ota_task", 12288, NULL, 5, NULL);
}

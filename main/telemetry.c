#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_app_desc.h"
#include "driver/gpio.h"
#include "wifi.h"
#include "telemetry.h"

/* GPIO_NUM_22/23 are not defined for ESP32S3 but the values are kept
 * to preserve the original hardware pin mapping. */
#define LED_PIN_RED   GPIO_NUM_21
#define LED_PIN_GREEN ((gpio_num_t)23)
#define LED_PIN_BLUE  ((gpio_num_t)22)

static const char *TAG = "TELEMETRY";

#if CONFIG_TELEMETRY_ENABLE

#include "mqtt_client.h"
#define TB_MQTT_URL "mqtt://mqtt.eu.thingsboard.cloud"

esp_mqtt_client_handle_t mqtt_client = NULL;

static void send_led_telemetry(int r, int g, int b)
{
    if (mqtt_client == NULL)
        return;
    char payload[128];
    snprintf(payload, sizeof(payload),
             "{\"red_led\":%d, \"green_led\":%d, \"blue_led\":%d}", r, g, b);
    esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", payload, 0, 1, 0);
}

static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                                int32_t event_id, void *event_data)
{
    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT Connected to ThingsBoard");
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT Disconnected");
        break;
    default:
        break;
    }
}

void mqtt_app_start(void)
{
    char token[64] = {0};
    if (get_stored_value("mqtt_token", token, sizeof(token)) != ESP_OK) {
        strncpy(token, CONFIG_THINGSBOARD_MQTT_ACCESS_TOKEN, sizeof(token) - 1);
        store_value("mqtt_token", token);
    }
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = TB_MQTT_URL,
        .credentials.username = token,
    };
    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(mqtt_client);
}

void led_blink_task(void *pvParameter)
{
    gpio_reset_pin(LED_PIN_RED);
    gpio_reset_pin(LED_PIN_GREEN);
    gpio_reset_pin(LED_PIN_BLUE);
    gpio_set_direction(LED_PIN_RED,   GPIO_MODE_OUTPUT);
    gpio_set_direction(LED_PIN_GREEN, GPIO_MODE_OUTPUT);
    gpio_set_direction(LED_PIN_BLUE,  GPIO_MODE_OUTPUT);
    while (1) {
        gpio_set_level(LED_PIN_RED,   1);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE,  0);
        send_led_telemetry(1, 1, 1);
        vTaskDelay(pdMS_TO_TICKS(500));
        gpio_set_level(LED_PIN_RED,   0);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE,  0);
        send_led_telemetry(0, 0, 0);
        vTaskDelay(pdMS_TO_TICKS(1000));
        gpio_set_level(LED_PIN_RED,   0);
        gpio_set_level(LED_PIN_GREEN, 0);
        gpio_set_level(LED_PIN_BLUE,  1);
        send_led_telemetry(1, 1, 1);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void version_print_task(void *pv)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();
    while (1) {
        ESP_LOGI(TAG, "Active Version: %s", app_desc->version);
        char p[64];
        snprintf(p, sizeof(p), "{\"fw_version\":\"%s\"}", app_desc->version);
        if (mqtt_client)
            esp_mqtt_client_publish(mqtt_client, "v1/devices/me/telemetry", p, 0, 1, 0);
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}

#else /* CONFIG_TELEMETRY_ENABLE not set */

void led_blink_task(void *pvParameter)
{
    gpio_reset_pin(LED_PIN_RED);
    gpio_reset_pin(LED_PIN_GREEN);
    gpio_reset_pin(LED_PIN_BLUE);
    gpio_set_direction(LED_PIN_RED,   GPIO_MODE_OUTPUT);
    gpio_set_direction(LED_PIN_GREEN, GPIO_MODE_OUTPUT);
    gpio_set_direction(LED_PIN_BLUE,  GPIO_MODE_OUTPUT);
    while (1) {
        gpio_set_level(LED_PIN_RED,   1);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE,  0);
        vTaskDelay(pdMS_TO_TICKS(500));
        gpio_set_level(LED_PIN_RED,   0);
        gpio_set_level(LED_PIN_GREEN, 1);
        gpio_set_level(LED_PIN_BLUE,  0);
        vTaskDelay(pdMS_TO_TICKS(1000));
        gpio_set_level(LED_PIN_RED,   0);
        gpio_set_level(LED_PIN_GREEN, 0);
        gpio_set_level(LED_PIN_BLUE,  1);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void version_print_task(void *pv)
{
    const esp_app_desc_t *app_desc = esp_app_get_description();
    while (1) {
        ESP_LOGI(TAG, "Active Version: %s", app_desc->version);
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}

#endif /* CONFIG_TELEMETRY_ENABLE */

#pragma once

#if CONFIG_TELEMETRY_ENABLE
#include "mqtt_client.h"
extern esp_mqtt_client_handle_t mqtt_client;
void mqtt_app_start(void);
#endif

void led_blink_task(void *pvParameter);
void version_print_task(void *pv);

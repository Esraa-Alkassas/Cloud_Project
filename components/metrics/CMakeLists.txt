idf_component_register(
    SRCS "metrics.c"
    INCLUDE_DIRS "."
    REQUIRES esp_timer esp_app_format nvs_flash app_update esp_system
)

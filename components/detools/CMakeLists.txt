# 1. Hide the fetching logic from ESP-IDF's early requirement scanner
if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    include(FetchContent)
    
    FetchContent_Declare(
        detools_src
        GIT_REPOSITORY https://github.com/eerimoq/detools.git
        GIT_TAG        master
        GIT_SUBMODULES ""
    )
    FetchContent_MakeAvailable(detools_src)

    set(detools_sources "${detools_src_SOURCE_DIR}/c/detools.c")
    set(detools_includes "${detools_src_SOURCE_DIR}/c")
endif()

# 2. Register the component
idf_component_register(
    SRCS ${detools_sources}
    INCLUDE_DIRS ${detools_includes}
)

# 3. Disable heavy compression algorithms that break ESP32 compilation
target_compile_definitions(${COMPONENT_LIB} PUBLIC
    DETOOLS_CONFIG_COMPRESSION_LZMA=0
    DETOOLS_CONFIG_COMPRESSION_ZSTD=0
    DETOOLS_CONFIG_COMPRESSION_LZ4=0
    DETOOLS_CONFIG_COMPRESSION_HEATSHRINK=0
)
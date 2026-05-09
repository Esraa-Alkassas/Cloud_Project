# The following five lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

# Tell the build system to re-run CMake whenever version.txt is modified
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "version.txt")

if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/version.txt")
    file(READ "${CMAKE_CURRENT_LIST_DIR}/version.txt" VER)
    string(STRIP "${VER}" VER)
    set(PROJECT_VER "${VER}")
else()
    set(PROJECT_VER "1") 
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(wifi_station)
# Visor agent layer: an OpenXR API layer, built as one self-contained module.
#
#   cmake -S native/layer -B build -DOPENXR_INCLUDE_DIR=<dir with openxr/openxr.h>
#   cmake --build build && cmake --install build --prefix <dest>
#
# `visor sim setup` runs exactly this; see visor/xr/setup.py.
cmake_minimum_required(VERSION 3.16)
project(visor_xr_layer CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

set(OPENXR_INCLUDE_DIR "" CACHE PATH "Directory containing openxr/openxr.h")
find_path(VULKAN_INCLUDE_DIR vulkan/vulkan.h HINTS "$ENV{VULKAN_SDK}/include")
if(NOT OPENXR_INCLUDE_DIR OR NOT EXISTS "${OPENXR_INCLUDE_DIR}/openxr/openxr.h")
  message(FATAL_ERROR "Set OPENXR_INCLUDE_DIR to a directory containing openxr/openxr.h")
endif()
if(NOT VULKAN_INCLUDE_DIR)
  message(FATAL_ERROR "Vulkan headers not found (vulkan/vulkan.h)")
endif()

set(LAYER_NAME XrApiLayer_visor_agent)
add_library(${LAYER_NAME} MODULE
  src/layer.cpp
  src/rig.cpp
  src/capture.cpp
  src/control.cpp
  src/json.cpp)
target_include_directories(${LAYER_NAME} PRIVATE ${OPENXR_INCLUDE_DIR} ${VULKAN_INCLUDE_DIR})
target_compile_options(${LAYER_NAME} PRIVATE -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers)
# The layer is loaded into arbitrary engines that bring their own C++ runtime:
# carry ours privately rather than depend on whichever libstdc++ they load.
target_link_options(${LAYER_NAME} PRIVATE -static-libstdc++ -static-libgcc -Wl,--exclude-libs,ALL -Wl,--no-undefined
  -Wl,-Bsymbolic -Wl,--version-script=${CMAKE_CURRENT_SOURCE_DIR}/exports.map)
set_property(TARGET ${LAYER_NAME} APPEND PROPERTY LINK_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/exports.map)
target_link_libraries(${LAYER_NAME} PRIVATE dl pthread rt)

configure_file(XrApiLayer_visor_agent.json.in ${CMAKE_CURRENT_BINARY_DIR}/XrApiLayer_visor_agent.json @ONLY)
install(TARGETS ${LAYER_NAME} LIBRARY DESTINATION .)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/XrApiLayer_visor_agent.json DESTINATION .)

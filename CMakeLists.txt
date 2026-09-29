cmake_minimum_required(VERSION 3.25)
project(kitty-skins VERSION 0.1.0 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
find_package(PkgConfig REQUIRED)
find_path(GLAZE_INCLUDE_DIR glaze/glaze.hpp REQUIRED)
pkg_check_modules(PNG REQUIRED IMPORTED_TARGET libpng)
pkg_check_modules(HYPR REQUIRED IMPORTED_TARGET
  hyprland
  libdrm
  pixman-1
  pangocairo)

add_library(kitty-skins-core STATIC
  src/core/manifest.cpp
  src/core/store.cpp
  src/core/process.cpp)

target_include_directories(kitty-skins-core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/include
  ${GLAZE_INCLUDE_DIR})

target_link_libraries(kitty-skins-core PUBLIC PkgConfig::PNG)

add_executable(terminal-skin
  src/cli/main.cpp)

target_link_libraries(terminal-skin PRIVATE kitty-skins-core)

add_library(kitty-skins SHARED
  src/plugin/plugin.cpp
  src/plugin/state.cpp
  src/plugin/texture.cpp
  src/plugin/layout.cpp
  src/plugin/decoration.cpp
  src/plugin/pass.cpp)

target_link_libraries(kitty-skins PRIVATE
  kitty-skins-core
  PkgConfig::HYPR
  rt)

target_compile_definitions(kitty-skins PRIVATE VERSION="${PROJECT_VERSION}")
set_target_properties(kitty-skins PROPERTIES PREFIX "lib")

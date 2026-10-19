cmake_minimum_required(VERSION 3.25)
project(swag VERSION 0.1.0 LANGUAGES NONE)

option(SWAG_CONTAINER_ONLY "Configure host-side container commands without a local Qt SDK" OFF)
if(SWAG_CONTAINER_ONLY)
    find_program(SWAG_ENGINE NAMES podman docker REQUIRED)
    find_program(SWAG_PYTHON NAMES python3.12 REQUIRED)
    if(EXISTS "${CMAKE_SOURCE_DIR}/.venv/bin/python")
        set(SWAG_TEST_PYTHON "${CMAKE_SOURCE_DIR}/.venv/bin/python")
    else()
        set(SWAG_TEST_PYTHON "${SWAG_PYTHON}")
    endif()
    add_custom_target(probe-image
        COMMAND "${SWAG_PYTHON}" tools/probe.py build --engine "${SWAG_ENGINE}"
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    foreach(backend qt tiger)
        add_custom_target(probe-${backend}
            COMMAND "${SWAG_PYTHON}" tools/probe.py run --engine "${SWAG_ENGINE}" --backend ${backend}
            WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    endforeach()
    add_custom_target(probe-check
        COMMAND "${SWAG_TEST_PYTHON}" tests/browser/smoke.py --engine "${SWAG_ENGINE}" --backend tiger --transport unix
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    add_dependencies(probe-check probe-image)
    add_custom_target(probe-compare
        COMMAND "${SWAG_TEST_PYTHON}" tests/browser/smoke.py --engine "${SWAG_ENGINE}" --backend both --transport tcp
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    add_dependencies(probe-compare probe-image)
    foreach(action build up down)
        add_custom_target(app-${action}
            COMMAND "${SWAG_PYTHON}" tools/app.py ${action} --engine "${SWAG_ENGINE}"
            WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    endforeach()
    add_custom_target(app-up-mdi
        COMMAND "${SWAG_PYTHON}" tools/app.py up --engine "${SWAG_ENGINE}" --ui-mode mdi
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    add_custom_target(app-mdi-check
        COMMAND "${SWAG_TEST_PYTHON}" tests/browser/mdi.py --engine "${SWAG_ENGINE}"
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    add_custom_target(app-resize-check
        COMMAND "${SWAG_TEST_PYTHON}" tests/browser/resize.py
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    add_custom_target(app-check
        COMMAND "${SWAG_TEST_PYTHON}" tests/browser/workspace.py --engine "${SWAG_ENGINE}"
        COMMAND "${SWAG_TEST_PYTHON}" tests/browser/login_state.py
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    add_custom_target(app-login-check
        COMMAND "${SWAG_TEST_PYTHON}" tests/browser/login_state.py
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" USES_TERMINAL)
    return()
endif()

enable_language(CXX)

option(SWAG_ENFORCE_TOOLCHAIN "Require the qualified GCC 15 toolchain" ON)
if(SWAG_ENFORCE_TOOLCHAIN)
    if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "GNU"
       OR CMAKE_CXX_COMPILER_VERSION VERSION_LESS 15
       OR NOT CMAKE_CXX_COMPILER_VERSION VERSION_LESS 16)
        message(FATAL_ERROR "Use GCC Toolset 15 in the EL8 container (or explicitly disable SWAG_ENFORCE_TOOLCHAIN for a local diagnostic build)")
    endif()
endif()

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)
find_package(Qt5 5.15 REQUIRED COMPONENTS Widgets Network)
add_subdirectory(src/probe)
install(TARGETS swag-probe RUNTIME DESTINATION bin)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/desktop/CMakeLists.txt")
    add_subdirectory(src/desktop)
    install(TARGETS swag-desktop RUNTIME DESTINATION bin)
endif()

cmake_minimum_required(VERSION 3.16)
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    project(vncws VERSION 0.1.0 LANGUAGES CXX)
endif()
set(CMAKE_AUTOMOC ON)

find_package(Qt5 5.15 REQUIRED COMPONENTS Core Gui Network WebSockets)
find_package(Qt5FbSupport REQUIRED)
find_package(Qt5FontDatabaseSupport REQUIRED)
find_package(Qt5ServiceSupport REQUIRED)
find_package(Qt5EventDispatcherSupport REQUIRED)
find_package(PkgConfig REQUIRED)
find_package(ZLIB REQUIRED)
pkg_check_modules(XKBCOMMON REQUIRED IMPORTED_TARGET xkbcommon)

add_library(qvncws MODULE
    main.cpp
    qvnc.cpp qvnc_p.h
    qvncclient.cpp qvncclient.h
    qvncintegration.cpp qvncintegration.h
    qvncscreen.cpp qvncscreen.h
    qvncwebsocketstream.cpp qvncwebsocketstream.h
    qvncstream.h
    qvnclocalstream.cpp qvnclocalstream.h
    qvncunixlistener.cpp qvncunixlistener.h
    vnc.json
)
set_target_properties(qvncws PROPERTIES CXX_STANDARD 17 CXX_STANDARD_REQUIRED ON)
target_link_libraries(qvncws PRIVATE
    Qt5::CorePrivate Qt5::GuiPrivate Qt5::Network Qt5::WebSockets
    Qt5::FbSupportPrivate Qt5::FontDatabaseSupportPrivate
    Qt5::ServiceSupportPrivate Qt5::EventDispatcherSupportPrivate
    PkgConfig::XKBCOMMON
    ZLIB::ZLIB
)
install(TARGETS qvncws LIBRARY DESTINATION plugins/platforms)

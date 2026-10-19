add_executable(swag-desktop main.cpp)
target_compile_features(swag-desktop PRIVATE cxx_std_23)
target_link_libraries(swag-desktop PRIVATE Qt5::Widgets Qt5::Network)

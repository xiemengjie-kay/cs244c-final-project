cmake_minimum_required(VERSION 3.20)
project(linearizing_paxos LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Export compile commands for VSCode IntelliSense
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_library(paxos_lib
    src/paxos.cpp
    src/paxos_tcp_transport.cpp
)

target_include_directories(paxos_lib
    PUBLIC
        ${PROJECT_SOURCE_DIR}/include
)

target_compile_options(paxos_lib
    PRIVATE
        -Wall
        -Wextra
        -Wpedantic
)

# Paxos node executable
add_executable(paxos_node
    src/paxos_node_main.cpp
)

target_link_libraries(paxos_node
    PRIVATE
        paxos_lib
)

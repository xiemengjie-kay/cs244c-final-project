cmake_minimum_required(VERSION 3.20)
project(linearizing_paxos LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(paxos_lib
    src/paxos.cpp
)

target_include_directories(paxos_lib PUBLIC include)

target_compile_options(paxos_lib PRIVATE -Wall -Wextra -Wpedantic)

add_executable(paxos_demo src/main.cpp)
target_link_libraries(paxos_demo PRIVATE paxos_lib)

add_executable(paxos_node
    src/paxos_node_main.cpp
    src/paxos_tcp_transport.cpp
)
target_link_libraries(paxos_node PRIVATE paxos_lib)

enable_testing()
add_executable(paxos_tests tests/test_paxos.cpp)
target_link_libraries(paxos_tests PRIVATE paxos_lib)
add_test(NAME paxos_tests COMMAND paxos_tests)

add_executable(paxos_transport_tests
    tests/test_paxos_transport.cpp
    src/paxos_tcp_transport.cpp
)
target_link_libraries(paxos_transport_tests PRIVATE paxos_lib)
add_test(NAME paxos_transport_tests COMMAND paxos_transport_tests)

add_test(
    NAME paxos_tcp_three_nodes
    COMMAND bash "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_paxos_tcp_three_nodes.sh" $<TARGET_FILE:paxos_node>
)
set_tests_properties(paxos_tcp_three_nodes PROPERTIES TIMEOUT 60)

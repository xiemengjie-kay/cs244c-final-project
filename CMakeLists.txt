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

add_executable(paxos_demo src/main.cpp src/paxos_process.cpp)
target_link_libraries(paxos_demo PRIVATE paxos_lib)

enable_testing()
add_executable(paxos_tests tests/test_paxos.cpp)
target_link_libraries(paxos_tests PRIVATE paxos_lib)
add_test(NAME paxos_tests COMMAND paxos_tests)

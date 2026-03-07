#include "process_mode.hpp"

#include <vector>
#include <string>

int run_process_node_mode(int argc, char** argv) {
  int node_id = -1;
  std::string ports_spec;

  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--id" && i + 1 < argc) {
      node_id = std::stoi(argv[++i]);
    } else if (arg == "--ports" && i + 1 < argc) {
      ports_spec = argv[++i];
    } else if (arg == "--help") {
      return 0;
    }
  }
}

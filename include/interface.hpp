#include "messsage.hpp"
#include "paxos.hpp"
#include "paxos_tcp_transport.hpp"
#include <iostream>
#include <sstream>
#include <unordered_map>


enum class EditType { Insert, Delete };

struct EditOperation {
	EditType type;
	uint32_t position;

	std::string text; // used for insert
	uint32_t length;  // used for delete
};

// insert 0 hello
// insert 5 world
// delete 3 2
bool parse_command(const std::string& cmd, EditOperation& out);

void usage(const char* prog);
std::unordered_map<int, TcpNodeAddress> parse_nodes(const std::string& spec);

void apply_to_document(std::string& doc, const std::string& cmd);
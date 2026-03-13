#include <sstream>
#include <unordered_map>
#include "messsage.hpp"
#include "paxos.hpp"
#include "paxos_tcp_transport.hpp"
#include <iostream>
#include "interface.hpp"


std::unordered_map<int, TcpNodeAddress> parse_nodes(const std::string& spec) {
	// Token formats:
	// - id:port (host defaults to 127.0.0.1)
	// - id:host:port
	std::unordered_map<int, TcpNodeAddress> out;
	std::stringstream ss(spec);
	std::string token;
	while (std::getline(ss, token, ',')) {
		if (token.empty()) {
			continue;
		}

		std::stringstream token_ss(token);
		std::vector<std::string> parts;
		std::string part;
		while (std::getline(token_ss, part, ':')) {
			parts.push_back(part);
		}

		if (parts.size() == 2) {
			const int id = std::stoi(parts[0]);
			const int port = std::stoi(parts[1]);
			if (port <= 0 || port > 65535) {
				throw std::runtime_error("port out of range in --nodes");
			}
			out[id] = TcpNodeAddress{.host = "127.0.0.1",
				.paxos_port = static_cast<std::uint16_t>(port)};
			continue;
		}

		if (parts.size() == 3) {
			const int id = std::stoi(parts[0]);
			const std::string& host = parts[1];
			const int port = std::stoi(parts[2]);
			if (port <= 0 || port > 65535) {
				throw std::runtime_error("port out of range in --nodes");
			}
			out[id] = TcpNodeAddress{
				.host = host, .paxos_port = static_cast<std::uint16_t>(port)};
			continue;
		}

		throw std::runtime_error(
			"bad --nodes token, expected id:port or id:host:port");
	}
	return out;
}

void usage(const char* prog) {
	std::cout << "Usage: " << prog
			  << " --id <node_id> --nodes <id:host:port,...> [--eval-trace]\n\n"
			  << "Examples:\n"
			  << "  " << prog << " --id 1 --nodes 1:15001,2:15002,3:15003\n"
			  << "  " << prog
			  << " --id 3 --nodes "
				 "1:10.0.0.10:15001,2:10.0.0.11:15002,3:10.0.0.12:15003\n\n"
			  << "Stdin commands:\n"
			  << "  /status  /crash  /restore  /quit  or plain command text\n";
}

bool parse_command(const std::string& cmd, EditOperation& out) {
	std::istringstream iss(cmd);
	std::string op;

	if (!(iss >> op))
		return false;

	if (op == "insert") {
		uint32_t pos;
		std::string text;

		if (!(iss >> pos))
			return false;

		std::getline(iss, text); // rest of line

		if (!text.empty() && text[0] == ' ')
			text.erase(0, 1);

		out.type = EditType::Insert;
		out.position = pos;
		out.text = text;
		out.length = 0;

		return true;
	}

	if (op == "delete") {
		uint32_t pos, len;

		if (!(iss >> pos >> len))
			return false;

		out.type = EditType::Delete;
		out.position = pos;
		out.length = len;
		out.text.clear();

		return true;
	}

	return false;
}

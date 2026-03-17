#include "editor_server.hpp"

#include <arpa/inet.h>
#include <iostream>
#include <unistd.h>

// the server here is in the c++ nodes
// the client is any browser

std::string encode(const std::string& text) {
	std::string result = text;
	size_t pos = 0;
	while ((pos = result.find('\n', pos)) != std::string::npos) {
		result.replace(pos, 1, "\\n");
		pos += 2;
	}
	return result;
}

void EditorServer::send_full_document_to_client(
	int client_fd, const std::string& document) {
	std::string msg = "FULL " + encode(document) + "\n"; // <-- note the newline
	std::lock_guard lock(clients_mu);

	ssize_t r = send(client_fd, msg.c_str(), msg.size(), 0);
	if (r <= 0) {
		close(client_fd);
		clients.erase(std::remove(clients.begin(), clients.end(), client_fd),
			clients.end());
	}
}

EditorServer::EditorServer(std::queue<std::string>& queue, std::mutex& mu, std::string& document)
	: input_queue(queue), input_mu(mu), document(document) {}

void EditorServer::start(int port) {
	running = true;
	server_thread = std::thread(&EditorServer::server_loop, this, port);
}

void EditorServer::stop() {
	running = false;
	if (server_thread.joinable()) {
		server_thread.join();
	}
}

void EditorServer::server_loop(int port) {
	int server_fd = socket(AF_INET, SOCK_STREAM, 0);

	sockaddr_in addr{};
	addr.sin_family = AF_INET;
	addr.sin_port = htons(port);
	addr.sin_addr.s_addr = INADDR_ANY;

	bind(server_fd, (sockaddr*)&addr, sizeof(addr));
	listen(server_fd, 8);

	std::cout << "EditorServer listening on port " << port << "\n";

	while (running) {
		int client_fd = accept(server_fd, nullptr, nullptr);

		if (client_fd < 0)
			continue;

		{
			std::lock_guard lock(clients_mu);
			clients.push_back(client_fd);
		}

		// send_full_document_to_client(client_fd, document);
		std::thread(&EditorServer::client_loop, this, client_fd).detach();
	}

	close(server_fd);
}

void EditorServer::client_loop(int client_fd) {
	char buffer[1024];
	std::string pending;

	while (running) {
		ssize_t n = read(client_fd, buffer, sizeof(buffer));
		if (n <= 0)
			break;

		pending.append(buffer, n);

		size_t pos;
		while ((pos = pending.find('\n')) != std::string::npos) {
			std::string line = pending.substr(0, pos);
			pending.erase(0, pos + 1);
			if (line == "FETCH_FULL_DOCUMENT") {
				send_full_document_to_client(client_fd, document);
				continue;
			}
			{
				std::lock_guard lock(input_mu);
				input_queue.push(std::move(line));
			}
		}
	}

	close(client_fd);

	{
		std::lock_guard lock(clients_mu);
		clients.erase(std::remove(clients.begin(), clients.end(), client_fd),
			clients.end());
	}
}

// for this demo we just have one client, which is the connected node.js
void EditorServer::broadcast(const std::string& msg) {
	std::string line = msg + "\n";

	std::lock_guard lock(clients_mu);

	for (auto it = clients.begin(); it != clients.end();) {
		ssize_t r = send(*it, line.c_str(), line.size(), 0);

		if (r <= 0) {
			close(*it);
			it = clients.erase(it);
		} else {
			++it;
		}
	}
}
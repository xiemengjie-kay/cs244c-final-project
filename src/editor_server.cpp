#include "editor_server.hpp"

#include <arpa/inet.h>
#include <iostream>
#include <unistd.h>

// the server here is in the c++ nodes
// the client is any browser
EditorServer::EditorServer(std::queue<std::string>& queue, std::mutex& mu)
	: input_queue(queue), input_mu(mu) {}

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
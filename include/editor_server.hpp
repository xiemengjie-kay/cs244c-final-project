#pragma once

#include <atomic>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>

class EditorServer {
  public:
	EditorServer(std::queue<std::string>& queue, std::mutex& mu);

	void start(int port);
	void stop();

	// send message to all connected clients
	void broadcast(const std::string& msg);

  private:
	void server_loop(int port);
	void client_loop(int client_fd);

	std::queue<std::string>& input_queue;
	std::mutex& input_mu;

	std::vector<int> clients;
	std::mutex clients_mu;

	std::thread server_thread;
	std::atomic<bool> running{false};
};
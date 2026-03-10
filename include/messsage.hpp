#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <variant>
#include <vector>


using NodeId = std::uint32_t;

enum class MessageType {
  CLIENT_REQUEST = 0,
  CLIENT_RESPONSE,
  FORWARD_REQUEST,
  PREPARE,
  PROMISE,
  ACCEPT_REQUEST,
  ACCEPTED,
  COMMIT,
  HEARTBEAT,
  SYNC_REQUEST,
  SYNC_DATA
};

struct Value {
	bool isRead; // Always read everything
	// For write:
	uint32_t row;
	uint32_t column_range[2]; // [inclusive-exclusive)
	std::string val;		  // e.g. "append Alice"
};

struct ClientRequest {
    NodeId client_id;
	uint32_t request_id;
    Value value;
};

struct ClientResponse {
    bool succ;
    bool isWrite;
};

struct ForwardRequest {
	ClientRequest request;
};

struct Prepare {
	uint64_t ballot;
};

struct Promise {
	uint64_t ballot;
	uint64_t accepted_ballot;
	std::optional<Value> accepted_value;
};

struct Accept {
	uint64_t ballot;
	uint64_t slot;
	Value value;
};

struct Accepted {
	uint64_t ballot;
	uint64_t slot;
};

struct Commit {
	uint64_t slot;
	Value value;
};

struct Heartbeat {
	uint64_t ballot;
	uint64_t committed_up_to;
};

using Payload =
    std::variant<ClientRequest, ClientResponse, ForwardRequest, Prepare, Promise, Accept, Accepted, Commit,
                 Heartbeat>;

struct Message {
  MessageType type{MessageType::PREPARE};
  NodeId from{0};
  NodeId to{0};
  std::uint64_t term_or_ballot{0};
  Payload payload;
};



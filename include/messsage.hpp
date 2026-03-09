using NodeId = uint32_t;

struct Message {
	MessageType type;
	NodeId from;
	NodeId to;
	uint64_t term_or_ballot;

	Payload payload;
};

enum class MessageType {
	CLIENT_REQUEST,
	CLIENT_RESPONSE,
	FORWARD_REQUEST,

	PREPARE,
	PROMISE,

	ACCEPT,
	ACCEPTED,

	COMMIT,

	HEARTBEAT
};

struct Value {
	bool isRead; // Always read everything
	// For write:
	uint32_t row;
	uint32_t[2] column_range; // [inclusive-exclusive)
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
};

using Payload = std::variant<ClientRequest, ClientResponse, ForwardRequest,
	Prepare, Promise, Accept, Accepted, Commit>;


const net = require("net");
const WebSocket = require("ws");

const args = process.argv.slice(2);

if (args.length < 2) {
  console.error("Usage: node bridge.js <tcp_port> <ws_port>");
  process.exit(1);
}

const TCP_PORT = parseInt(args[0], 10);
const WS_PORT = parseInt(args[1], 10);

let tcp;

// bridge.js connects PaxosNode in C++ implementation and the browser editor
// TCP between C++ and Node.js: C++ is server, expect messages with \n in and out
// WebSocket between Node.js and browser: Node.js is server, expect messages without \n in and out
function encode(text) {
  return text.replace(/\n/g, "\\n");
}

function connectTCP() {
  tcp = net.createConnection({ host: "127.0.0.1", port: TCP_PORT });

  tcp.on("connect", () => {
    console.log("Connected to EditorServer");
  });

let buffer = "";

  // on receiving data from C++ server, split by newline and send to all clients connected via websocket
  tcp.on("data", (data) => {
    buffer += data.toString();

    let index;

    while ((index = buffer.indexOf("\n")) >= 0) {
        const msg = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);

        if (!msg) continue;

        console.log("TCP -> Browser:", msg);

        // this message that we sends to browser does not end with newline
        // for this demo we just have one client, which is the single browser connected
        wss.clients.forEach((client) => {
        if (client.readyState === WebSocket.OPEN) {
            client.send(encode(msg));
        }
        });
    }
    });

  tcp.on("error", (err) => {
    console.log("TCP error:", err.code);
  });

  tcp.on("close", () => {
    console.log("TCP connection closed. Reconnecting in 1s...");
    setTimeout(connectTCP, 1000);
  });
}

connectTCP();

const wss = new WebSocket.Server({ port: WS_PORT });

console.log("WebSocket server running on ws://localhost:" + WS_PORT);

wss.on("connection", (ws) => {
console.log("Browser connected");

  // append newline to browser message
  // on receiving message from browser, send to C++ server via TCP
  // websocket preserves message boundaries, so we don't need to worry about fragmentation or buffering
  ws.on("message", (msg) => {
    if (tcp && !tcp.destroyed) {
      function encode(text) {
        return text.replace(/\n/g, "\\n");
      }

      tcp.write(encode(msg.toString()) + "\n");
    }
  });
});

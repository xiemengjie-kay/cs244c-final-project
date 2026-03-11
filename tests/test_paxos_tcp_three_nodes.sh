#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <path-to-paxos_node>" >&2
  exit 2
fi

node_bin="$1"
if [[ ! -x "$node_bin" ]]; then
  echo "paxos_node binary is not executable: $node_bin" >&2
  exit 2
fi

tmp_dir="$(mktemp -d)"
pids=()

print_logs() {
  for id in 1 2 3; do
    local log_file="$tmp_dir/node${id}.log"
    echo "===== node${id}.log =====" >&2
    if [[ -f "$log_file" ]]; then
      cat "$log_file" >&2
    else
      echo "<missing>" >&2
    fi
  done
}

fail() {
  echo "test failure: $1" >&2
  print_logs
  exit 1
}

cleanup() {
  for pid in "${pids[@]-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  if [[ ${#pids[@]} -gt 0 ]]; then
    wait "${pids[@]}" 2>/dev/null || true
  fi
  exec 3>&- 4>&- 5>&- || true
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

mkfifo "$tmp_dir/node1.in" "$tmp_dir/node2.in" "$tmp_dir/node3.in"
exec 3<>"$tmp_dir/node1.in"
exec 4<>"$tmp_dir/node2.in"
exec 5<>"$tmp_dir/node3.in"

base_port=$((30000 + (RANDOM % 15000)))
nodes_spec="1:${base_port},2:$((base_port + 1)),3:$((base_port + 2))"

"$node_bin" --id 1 --nodes "$nodes_spec" \
  <"$tmp_dir/node1.in" >"$tmp_dir/node1.log" 2>&1 &
pids+=("$!")
"$node_bin" --id 2 --nodes "$nodes_spec" \
  <"$tmp_dir/node2.in" >"$tmp_dir/node2.log" 2>&1 &
pids+=("$!")
"$node_bin" --id 3 --nodes "$nodes_spec" \
  <"$tmp_dir/node3.in" >"$tmp_dir/node3.log" 2>&1 &
pids+=("$!")

sleep 3

printf '/status\n' >&3
printf '/status\n' >&4
printf '/status\n' >&5

test_command="terminal-network-check-${base_port}"
printf '%s\n' "$test_command" >&3
printf '%s\n' "$test_command" >&4
printf '%s\n' "$test_command" >&5

sleep 4

printf '/status\n' >&3
printf '/status\n' >&4
printf '/status\n' >&5

sleep 1

printf '/quit\n' >&3
printf '/quit\n' >&4
printf '/quit\n' >&5

exec 3>&-
exec 4>&-
exec 5>&-

deadline=$((SECONDS + 12))
while (( SECONDS < deadline )); do
  any_alive=0
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      any_alive=1
      break
    fi
  done
  if [[ "$any_alive" -eq 0 ]]; then
    break
  fi
  sleep 1
done

for pid in "${pids[@]}"; do
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
done
wait "${pids[@]}" 2>/dev/null || true
pids=()

leader_yes=0
for id in 1 2 3; do
  log_file="$tmp_dir/node${id}.log"
  if ! grep -q "node ${id} started on paxos@" "$log_file"; then
    fail "node ${id} did not start correctly"
  fi

  status_line="$(awk '/node '"${id}"' leader=/{line=$0} END{print line}' "$log_file")"
  if [[ -z "$status_line" ]]; then
    fail "node ${id} never printed status"
  fi
  if [[ "$status_line" == *"leader=yes"* ]]; then
    leader_yes=$((leader_yes + 1))
  fi

  if ! grep -E -q "node ${id} committed slot [0-9]+: ${test_command}" "$log_file"; then
    fail "node ${id} did not commit replicated command"
  fi
done

if [[ "$leader_yes" -ne 1 ]]; then
  fail "expected exactly one leader in final status snapshot, saw ${leader_yes}"
fi

echo "paxos_tcp_three_nodes passed"

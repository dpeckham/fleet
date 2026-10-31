# Runs as root in the environment and only reads state. Prints: enabled active process-count
# Counts dev's processes, leaving out the session manager that any login leaves behind.
agent_count() { ps -u dev -o comm= 2>/dev/null | grep -cvE '^(systemd|\(sd-pam\))$' || true; }
enabled=$(systemctl is-enabled fleet-agents 2>/dev/null) || enabled=${enabled:-no}
active=$(systemctl is-active fleet-agents 2>/dev/null) || active=${active:-inactive}
count=$(agent_count)
echo "$enabled $active $count"

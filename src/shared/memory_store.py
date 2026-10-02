import json
from datetime import datetime


class SharedMemoryStore:
    """
    Central memory store. All agents read from and write to this object.
    """
    def __init__(self):
        self.memory = {
            "session_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "created_at": datetime.now().isoformat(),
            "runtime_anomaly_agent": None,
            "drift_detection_agent": None,
            "security_agent": None,
            "rca_agent": None,
            "remediation_agent": None,
            "validation_agent": None,
            "incident": {
                "active": False,
                "severity": "NORMAL",
                "affected_services": [],
                "root_cause": None,
                "remediation_plan": None,
                "status": "MONITORING"
            }
        }

    def write(self, agent_name, data):
        """Store an agent's output."""
        self.memory[agent_name] = {
            "timestamp": datetime.now().isoformat(),
            "data": data
        }
        print(f"[SharedMemory] {agent_name} → written")

    def read(self, agent_name):
        """Read an agent's stored output."""
        entry = self.memory.get(agent_name)
        if entry:
            return entry.get("data")
        return None

    def update_incident(self, **kwargs):
        """Update the current incident state."""
        for key, value in kwargs.items():
            if key in self.memory["incident"]:
                self.memory["incident"][key] = value

    def get_incident(self):
        return self.memory["incident"]

    def get_full_state(self):
        return self.memory

    def print_summary(self):
        """Print a summary of the current state."""
        print("\n" + "="*50)
        print("SHARED MEMORY SUMMARY")
        print("="*50)
        incident = self.memory["incident"]
        print(f"Status:    {incident['status']}")
        print(f"Severity:  {incident['severity']}")
        print(f"Active:    {incident['active']}")
        print(f"Services:  {incident['affected_services']}")
        print(f"Root Cause: {incident['root_cause']}")

        agents = [
            "runtime_anomaly_agent",
            "drift_detection_agent",
            "rca_agent",
            "remediation_agent",
            "validation_agent"
        ]
        print("\nAgent Status:")
        for agent in agents:
            status = "✓ Done" if self.memory.get(agent) else "○ Pending"
            print(f"  {agent}: {status}")
"""
Defense-in-depth command validator.

Operator confirmation is never a complete guarantee that a command is safe:
an operator may approve in haste, or the LLM may generate an unusual command
that looks routine at a glance. This module is therefore a SECOND,
independent safety layer. Even AFTER the operator approves, the command must
still pass this allow-list before it executes.

Design: only specific (verb, resource-type) combinations are permitted, and
the target must always be one of the six known services from the testbed
(Section 5.2). Anything outside that set -- namespace-level deletes, RBAC
changes, exec into pods, arbitrary apply/create -- is rejected even if the
operator has already approved it.
"""

import re

KNOWN_SERVICES = {
    "frontend",
    "cartservice",
    "currencyservice",
    "paymentservice",
    "productcatalogservice",
    "redis-cart",
}

# Permitted (verb, resource_type) pairs. These cover both the four
# remediation actions listed in Fig. 1 of the paper and the diagnostic verbs
# that appeared most often in the recorded runs.
ALLOWED_OPERATIONS = {
    # Diagnostic / read-only (no state change)
    ("describe", "pod"),
    ("get", "pod"),
    ("get", "deployment"),
    ("get", "hpa"),
    ("top", "pod"),
    ("logs", "pod"),
    # Remediation actions (state-changing; listed in Fig. 1 of the paper)
    ("rollout", "restart"),   # kubectl rollout restart deployment/X
    ("scale", "deployment"),  # kubectl scale deployment/X --replicas=N
    ("patch", "configmap"),   # kubectl patch configmap/X
    ("rollout", "undo"),      # kubectl rollout undo deployment/X
}


class CommandRejected(Exception):
    """Raised when a command fails the allow-list check."""
    pass


def validate_command(action: str) -> tuple[bool, str]:
    """
    Validate a kubectl command string:
      1. Is the (verb, resource-type) pair on the allow-list?
      2. Is the target one of KNOWN_SERVICES?

    Returns: (is_safe: bool, reason: str)
    """
    action = action.strip()

    if not action.startswith("kubectl"):
        return False, "Only kubectl commands are permitted (non-kubectl rejected)"

    tokens = action.split()
    if len(tokens) < 2:
        return False, "Command is too short or incomplete"

    verb = tokens[1].lower()

    # Locate the resource type: the first token after the verb that is not a flag
    resource_type = None
    for t in tokens[2:]:
        if not t.startswith("-"):
            # May take the form "pod/xyz" or "deployment/xyz"
            resource_type = t.split("/")[0].lower()
            break

    if resource_type is None:
        return False, "Could not identify a resource type"

    if (verb, resource_type) not in ALLOWED_OPERATIONS:
        return False, (
            f"'{verb} {resource_type}' is not on the allow-list "
            f"(permitted: {sorted(ALLOWED_OPERATIONS)})"
        )

    # Target check: a known service name must appear somewhere in the
    # command (as an app label, resource name, and so on).
    if not any(svc in action for svc in KNOWN_SERVICES):
        return False, "No known service (Section 5.2) referenced in the command"

    # Basic shell-injection guard: semicolons, pipes, backticks and $() are rejected
    if re.search(r"[;&|`$]|\$\(", action):
        return False, "Shell metacharacters are not permitted (safety)"

    return True, "OK"
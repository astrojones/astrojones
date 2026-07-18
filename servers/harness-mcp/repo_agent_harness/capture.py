"""SSOT for the cognee ship node_set used by the claude-mem -> cognee mirror.

The client-side capture/digest pipeline (local sqlite queue + in-process drain) was
removed per the coexistence contract (D5): claude-mem is now the single upstream capture
store, and ``CogneeSync`` mirrors it into cognee. All that survives here is the node_set
label every mirrored session digest ships under — kept in one place so ``cognee_sync`` (the
writer) and ``agent_hooks._recall_section`` (the reader) agree on the same value.
"""

from __future__ import annotations

# The node_set every mirrored session digest is tagged with. Read by
# cognee_sync.remember() (write path) and agent_hooks._recall_section (recall filter).
CAPTURE_NODE_SET = ["session_digest"]

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "ruiware-mcp"))

from ruiware_mcp.server import McpApplication, TOOLS


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path, None))
        if path.endswith("/draft-1"):
            return {"id": "draft-1", "attachments": [{"id": "asset-1", "filename": "drawing.pdf"}]}
        return {"complete": True, "stage": "features"}

    def post(self, path, payload):
        self.calls.append(("POST", path, payload))
        return {"path": path, "payload": payload}


def content(result):
    return json.loads(result["content"][0]["text"])


def test_tools_list_and_initialize_are_mcp_compatible():
    app = McpApplication(FakeClient())
    initialized = app.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert initialized["result"]["capabilities"] == {"tools": {}}
    listed = app.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {tool["name"] for tool in listed["result"]["tools"]} == {tool["name"] for tool in TOOLS}


def test_context_attachment_and_validation_tools_are_read_only():
    client = FakeClient()
    app = McpApplication(client)
    draft = content(app.call_tool("ruiware_get_draft_context", {"draftId": "draft-1"}))
    attachment = content(app.call_tool("ruiware_get_attachment", {"draftId": "draft-1", "attachmentId": "asset-1"}))
    validation = content(app.call_tool("ruiware_get_validation_result", {"draftId": "draft-1", "stage": "features"}))
    assert draft["id"] == "draft-1"
    assert attachment["filename"] == "drawing.pdf"
    assert validation["complete"] is True
    assert all(method == "GET" for method, _, _ in client.calls)


def test_submit_proposal_uses_only_the_explicit_apply_route():
    client = FakeClient()
    app = McpApplication(client)
    proposal = {"id": "proposal-1", "baseRevision": 3, "commands": []}
    response = content(app.call_tool("ruiware_submit_proposal", {"draftId": "draft-1", "proposal": proposal, "selectedCommandIds": []}))
    assert response["path"] == "/template-drafts/draft-1/proposals/apply"
    assert response["payload"]["proposal"] == proposal

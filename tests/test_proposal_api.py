from fastapi.testclient import TestClient

import app.main as main
from app.repository import Repository
from template_core.material import RuiWareMaterialLibrary


def test_structured_proposal_preview_and_apply_use_generic_routes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "repository", Repository(tmp_path / "platform.db", RuiWareMaterialLibrary(tmp_path / "materials.db")))
    client = TestClient(main.app)
    draft = client.post("/api/v1/template-drafts/blank", json={"name": "提案路由测试"}).json()
    proposal = {
        "id": "proposal-length",
        "taskType": "parameterRecognition",
        "baseRevision": draft["revision"],
        "summary": "增加长度参数",
        "confidence": 1,
        "assumptions": [],
        "requiredConfirmations": [],
        "commands": [{
            "id": "cmd-length",
            "type": "upsertParameter",
            "targetId": "length",
            "payload": {"id": "length", "label": "长度", "default": 1000, "minimum": 1, "maximum": 5000},
            "reason": "作为实例驱动参数",
        }],
    }
    preview = client.post(f"/api/v1/template-drafts/{draft['id']}/proposals/preview", json={"proposal": proposal})
    assert preview.status_code == 200
    assert preview.json()["canAccept"] is True
    applied = client.post(f"/api/v1/template-drafts/{draft['id']}/proposals/apply", json={"proposal": proposal})
    assert applied.status_code == 200
    assert applied.json()["revision"] == draft["revision"] + 1
    assert any(item["id"] == "length" for item in applied.json()["parameterDefinitions"])

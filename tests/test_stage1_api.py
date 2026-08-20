import io
import zipfile

from fastapi.testclient import TestClient

import app.main as main
from app.repository import Repository
from template_core.material import RuiWareMaterialLibrary


def test_stage1_api_attachment_completion_and_source_package(tmp_path, monkeypatch) -> None:
    store = Repository(tmp_path / "platform.db", RuiWareMaterialLibrary(tmp_path / "unused.db"))
    attachment_root = tmp_path / "attachments"
    artifact_root = tmp_path / "artifacts"
    attachment_root.mkdir()
    artifact_root.mkdir()
    monkeypatch.setattr(main, "repository", store)
    monkeypatch.setattr(main, "ATTACHMENT_ROOT", attachment_root)
    monkeypatch.setattr(main, "ARTIFACT_ROOT", artifact_root)
    client = TestClient(main.app)

    created_response = client.post("/api/v1/template-drafts/blank", json={"name": "第一阶段测试模板"})
    assert created_response.status_code == 201
    draft = created_response.json()
    draft.update(
        {
            "description": "用于验证模板信息阶段完整功能的测试草稿。",
            "designIntent": "通过明确的制造类别、设计意图和来源资料建立可审计的模板工程输入。",
            "manufacturingClassification": {"originId":"inHouse","primaryProcessId":"cutting","secondaryProcessIds":["bending"],"reviewed":True},
            "geometryPrototypeId": "prototype.plate",
            "owner": "测试工程师",
            "organization": "RuiWare",
            "tags": ["测试", "板材"],
        }
    )
    saved_response = client.put(f"/api/v1/template-drafts/{draft['id']}", json=draft)
    assert saved_response.status_code == 200
    saved = saved_response.json()
    assert saved["revision"] == 2

    upload_response = client.post(
        f"/api/v1/template-drafts/{draft['id']}/attachments?filename=reference.png&kind=referenceImage",
        content=b"\x89PNG\r\n\x1a\nreference-image",
        headers={"Content-Type": "image/png"},
    )
    assert upload_response.status_code == 200
    uploaded = upload_response.json()
    assert uploaded["attachments"][0]["sha256"]
    assert uploaded["revision"] == 3

    metadata_response = client.patch(
        f"/api/v1/template-drafts/{draft['id']}/attachments/{uploaded['attachments'][0]['id']}",
        json={"description":"主视图，总宽90 mm","kind":"referenceImage"},
    )
    assert metadata_response.status_code == 200
    uploaded = metadata_response.json()
    assert uploaded["attachments"][0]["description"] == "主视图，总宽90 mm"
    assert uploaded["revision"] == 4

    validation = client.get(
        f"/api/v1/template-drafts/{draft['id']}/stages/templateInfo/validate"
    ).json()
    assert validation["complete"] is True
    assert validation["progress"] == 100

    completion = client.post(
        f"/api/v1/template-drafts/{draft['id']}/stages/templateInfo/complete"
    ).json()
    assert completion["draft"]["stageStatus"]["templateInfo"] == "complete"
    assert completion["draft"]["revision"] == 5

    package_response = client.get(f"/api/v1/template-drafts/{draft['id']}/source-package")
    assert package_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package_response.content)) as package:
        names = set(package.namelist())
        assert {
            "manifest.json",
            "evidence.json",
            "material-requirements.json",
            "material-validation.json",
            "parameters.json",
            "parameter-dependencies.json",
            "geometry-recipe.json",
            "classification.json",
            "feature-rules.json",
            "variants.json",
            "constraints.json",
            "sketch-solver.json",
            "interfaces.json",
            "outputs.json",
            "admission.json",
        } <= names
        assert any(name.endswith("reference.png") for name in names)

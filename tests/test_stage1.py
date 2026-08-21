import pytest

from app.repository import Repository, RevisionConflictError
from template_core.material import RuiWareMaterialLibrary
from template_core.models import TemplateDraft
from template_core.stage1 import validate_template_info


def complete_draft(code: str = "RW-TPL-1001") -> TemplateDraft:
    return TemplateDraft.model_validate(
        {
            "code": code,
            "name": "通用工业零部件模板",
            "description": "用于验证第一阶段模板信息、来源资料和修订管理。",
            "designIntent": "通过通用制造形态建立可参数化模板，并在后续阶段补充材料、草图与特征。",
            "manufacturingClassification": {"originId":"inHouse","primaryProcessId":"coldRollForming","reviewed":True},
            "geometryPrototypeId": "prototype.openThinWallProfile",
            "tags": ["货架", "型材"],
            "owner": "模板工程组",
            "organization": "RuiWare",
        }
    )


def repository(tmp_path) -> Repository:
    return Repository(tmp_path / "platform.db", RuiWareMaterialLibrary(tmp_path / "unused.db"))


def test_template_info_validation_has_stable_paths() -> None:
    incomplete = complete_draft().model_copy(update={"code": "?", "designIntent": "太短", "manufacturingClassification": complete_draft().manufacturingClassification.model_copy(update={"reviewed":False})})
    result = validate_template_info(incomplete)
    assert result.complete is False
    assert {item.path for item in result.checks if not item.passed} >= {"code", "designIntent", "manufacturingClassification.reviewed"}

    complete = validate_template_info(complete_draft())
    assert complete.complete is True
    assert complete.progress == 100


def test_draft_lifecycle_revisions_duplicate_archive_and_restore(tmp_path) -> None:
    store = repository(tmp_path)
    first = store.save_draft(complete_draft(), reason="create")
    assert first.revision == 1
    assert len(store.list_revisions(first.id)) == 1

    completed = first.model_copy(
        update={"stageStatus": first.stageStatus.model_copy(update={"templateInfo": "complete"})}
    )
    second = store.save_draft(completed, expected_revision=1, reason="complete-template-info")
    assert second.stageStatus.templateInfo == "complete"

    edited = second.model_copy(update={"name": "修改后的模板名称"})
    third = store.save_draft(edited, expected_revision=2, reason="manual-save")
    assert third.revision == 3
    assert third.stageStatus.templateInfo == "in_progress"

    with pytest.raises(RevisionConflictError):
        store.save_draft(edited, expected_revision=2)

    duplicate = store.duplicate_draft(third.id)
    assert duplicate.id != third.id
    assert duplicate.code.startswith("RW-TPL-1001-COPY")
    assert duplicate.revision == 1

    store.archive_draft(third.id)
    assert all(item.id != third.id for item in store.list_drafts())
    restored = store.restore_draft(third.id)
    assert restored.id == third.id

    restored_revision = store.restore_revision(third.id, 1)
    assert restored_revision.revision == 4
    assert restored_revision.name == "通用工业零部件模板"

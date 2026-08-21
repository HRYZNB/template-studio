from app.config import LOCAL_DATABASE, MATERIAL_DATABASE
from app.repository import Repository
from template_core.material import RuiWareMaterialLibrary
from template_core.models import TemplateDraft


repository = Repository(LOCAL_DATABASE, RuiWareMaterialLibrary(MATERIAL_DATABASE))
for current in repository.list_drafts():
    replacement = TemplateDraft(
        id=current.id,
        revision=current.revision,
        code=current.code,
        name=current.name,
        description=current.description,
        designIntent=current.designIntent,
        owner=current.owner,
        organization=current.organization,
        tags=current.tags,
    )
    repository.save_draft(replacement, expected_revision=current.revision, reason="generic-sketch-reset")

print("Existing editable drafts now use the generic parametric sketch model.")

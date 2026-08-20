"""Create or refresh the greenfield vertical-slice demo."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "template-api"))

from app.main import repository  # noqa: E402
from template_core.models import TemplateDraft  # noqa: E402


def main() -> None:
    bindings = repository.list_bindings()
    binding = bindings[0] if bindings else repository.create_binding("1", "reference").model_dump()
    existing = repository.list_drafts()
    matching = next((draft for draft in existing if draft.name == "Ω型立柱纵向切片示例"), None)
    payload = TemplateDraft.model_validate(
        {
            "id": matching.id if matching else None,
            "name": "Ω型立柱纵向切片示例",
            "code": "RW-TPL-OMEGA-DEMO-001",
            "description": "通用带返边开口型材与规则驱动孔列",
            "designIntent": "采用冷弯开口薄壁型材几何配方，孔列数量和位置由制造特征规则生成。",
            "manufacturingClassification": {"originId":"inHouse","primaryProcessId":"coldRollForming","secondaryProcessIds":["punching"],"reviewed":True},
            "geometryPrototypeId": "prototype.openThinWallProfile",
            "materialRequirements": [{"selectionMode":"category","supplyForm":"coil","reviewed":True}],
            "materialValidationSamples": [{
                "id":"material.nominal","role":"nominal","name":"标称样例","bindingId":binding["id"],
                "bindingMode":binding["mode"],"materialCode":binding["snapshot"]["code"],"materialName":binding["snapshot"]["name"],
                "materialThickness":binding["snapshot"].get("thickness"),"variantId":"nominal","requiredForAdmission":True,"reviewed":True,
            }],
            "featureRulesReviewed": True,
            "featureRules": [{"id":"holes.main","name":"交错孔列","featureType":"circularHole","generationMode":"linearArray","countExpression":"4","arguments":{"diameter":12},"argumentExpressions":{"x":"-18 if i % 2 == 0 else 18","z":"200 + i * 300"}}],
            "revision": 1,
        }
    )
    saved = repository.save_draft(payload)
    print(saved.id)


if __name__ == "__main__":
    main()

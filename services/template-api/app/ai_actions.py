"""外部助手提案的结构化命令处理。

这里不调用任何大模型，只负责校验、预览和应用已经结构化的提案命令。
这样可以保证 AI/外部工具的修改必须经过工程师确认后才写入草稿。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from template_core.metamodel import GeometryOperationDefinition, ParameterDefinition
from template_core.models import (
    SemanticSketchConstraint,
    SemanticSketchEntity,
    SemanticSketchRegion,
    TemplateDraft,
)


AITaskType = Literal[
    "parameterRecognition",
    "sketchSettings",
    "sketchDrawing",
    "sketchDesign",
    "geometryRecipe",
    "geometryReview",
]

AICommandType = Literal[
    "setSketchSettings",
    "replaceSketchGeometry",
    "upsertParameter",
    "removeParameter",
    "upsertSketchEntity",
    "removeSketchEntity",
    "upsertSketchConstraint",
    "removeSketchConstraint",
    "upsertSketchRegion",
    "removeSketchRegion",
    "setGeometryRecipe",
    "upsertGeometryOperation",
    "removeGeometryOperation",
]


class AIModelCommand(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    type: AICommandType
    targetId: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class AIModelProposal(BaseModel):
    id: str = Field(default_factory=lambda: f"ai-{uuid.uuid4().hex[:12]}")
    taskType: AITaskType
    baseRevision: int
    summary: str
    confidence: float = Field(default=0.5, ge=0, le=1)
    assumptions: list[str] = Field(default_factory=list)
    requiredConfirmations: list[str] = Field(default_factory=list)
    commands: list[AIModelCommand] = Field(default_factory=list, max_length=200)


class ProposalError(ValueError):
    pass


_TASK_COMMANDS: dict[str, set[str]] = {
    "parameterRecognition": {"upsertParameter", "removeParameter"},
    "sketchSettings": {"setSketchSettings"},
    "sketchDrawing": {
        "replaceSketchGeometry", "upsertSketchEntity", "removeSketchEntity",
        "upsertSketchRegion", "removeSketchRegion",
    },
    "sketchDesign": {
        "upsertParameter", "upsertSketchConstraint", "removeSketchConstraint",
        "upsertSketchRegion", "removeSketchRegion",
    },
    "geometryRecipe": {
        "setGeometryRecipe", "upsertGeometryOperation", "removeGeometryOperation",
    },
    "geometryReview": {
        "setSketchSettings", "replaceSketchGeometry", "upsertParameter", "removeParameter", "upsertSketchEntity",
        "removeSketchEntity", "upsertSketchConstraint", "removeSketchConstraint",
        "upsertSketchRegion", "removeSketchRegion", "setGeometryRecipe",
        "upsertGeometryOperation", "removeGeometryOperation",
    },
}


def _upsert(items: list[Any], value: Any, target_id: str) -> list[Any]:
    return [value if item.id == target_id else item for item in items] if any(item.id == target_id for item in items) else [*items, value]


def _existing(items: list[Any], target_id: str) -> Any | None:
    return next((item for item in items if item.id == target_id), None)


def _merged_payload(items: list[Any], target_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = _existing(items, target_id)
    return {**(current.model_dump() if current else {}), **payload, "id": target_id}


def _numeric_parameter(draft: TemplateDraft, parameter_id: str) -> float | None:
    parameter = _existing(draft.parameterDefinitions, parameter_id)
    value = parameter.default if parameter else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _normalize_entities(
    draft: TemplateDraft, entities: list[SemanticSketchEntity],
) -> list[SemanticSketchEntity]:
    """Map proposal-relative coordinates onto confirmed overall section dimensions."""
    points: list[tuple[float, float]] = []
    for entity in entities:
        points.extend(point for point in (entity.start, entity.end, entity.center) if point is not None)
        points.extend(entity.points)
        if entity.center and entity.radius:
            points.extend([
                (entity.center[0] - entity.radius, entity.center[1] - entity.radius),
                (entity.center[0] + entity.radius, entity.center[1] + entity.radius),
            ])
    if not points:
        raise ProposalError("提案草图没有可用于坐标标定的点。")
    minimum_x, maximum_x = min(x for x, _ in points), max(x for x, _ in points)
    minimum_y, maximum_y = min(y for _, y in points), max(y for _, y in points)
    span_x, span_y = maximum_x - minimum_x, maximum_y - minimum_y
    if span_x <= 1e-9 or span_y <= 1e-9:
        raise ProposalError("提案草图的宽度或高度为 0，无法标定。")
    width = _numeric_parameter(draft, "sectionWidth") or span_x
    height = _numeric_parameter(draft, "sectionHeight") or span_y
    scale_x, scale_y = width / span_x, height / span_y
    center_x, center_y = (minimum_x + maximum_x) / 2, (minimum_y + maximum_y) / 2

    def point(value: tuple[float, float] | None) -> tuple[float, float] | None:
        return None if value is None else ((value[0] - center_x) * scale_x, (value[1] - center_y) * scale_y)

    normalized: list[SemanticSketchEntity] = []
    for entity in entities:
        normalized.append(entity.model_copy(update={
            "start": point(entity.start),
            "end": point(entity.end),
            "center": point(entity.center),
            "points": [point(item) for item in entity.points],
            "radius": entity.radius * min(scale_x, scale_y) if entity.radius else None,
        }))
    return normalized


# 将选中的提案命令应用到草稿副本上；这里只改内存对象，不直接保存数据库。
def apply_proposal(
    draft: TemplateDraft,
    proposal: AIModelProposal,
    selected_command_ids: list[str] | None = None,
) -> tuple[TemplateDraft, list[AIModelCommand]]:
    if proposal.baseRevision != draft.revision:
        raise ProposalError(
            f"提案基于 R{proposal.baseRevision}，当前已是 R{draft.revision}；请基于最新修订重新生成。"
        )
    selected = set(selected_command_ids) if selected_command_ids is not None else None
    commands = [command for command in proposal.commands if selected is None or command.id in selected]
    if selected is not None and selected - {item.id for item in proposal.commands}:
        raise ProposalError("选中的命令不属于当前提案。")
    candidate = draft.model_copy(deep=True)
    sketch_changed = False
    recipe_changed = False

    for command in commands:
        if command.type not in _TASK_COMMANDS[proposal.taskType]:
            raise ProposalError(f"任务 {proposal.taskType} 不允许 {command.type}")
        target_id, payload = command.targetId.strip(), command.payload
        if command.type not in {"setSketchSettings", "replaceSketchGeometry", "setGeometryRecipe"} and not target_id:
            raise ProposalError(f"命令 {command.id} 缺少 targetId")

        if command.type == "setSketchSettings":
            allowed = {"plane", "profileMode", "drivingParameters", "sourceAttachmentId", "importUnit", "importScale"}
            unknown = set(payload) - allowed
            if unknown:
                raise ProposalError(f"草图设置包含不允许字段：{', '.join(sorted(unknown))}")
            candidate.sketch = candidate.sketch.model_copy(update=payload)
            sketch_changed = True
        elif command.type == "replaceSketchGeometry":
            unknown = set(payload) - {"entities", "regions", "coordinateSpace"}
            if unknown:
                raise ProposalError(f"替换草图包含不允许字段：{', '.join(sorted(unknown))}")
            raw_entities = payload.get("entities")
            raw_regions = payload.get("regions", [])
            if not isinstance(raw_entities, list) or not raw_entities:
                raise ProposalError("替换草图必须包含非空 entities 数组。")
            if not isinstance(raw_regions, list):
                raise ProposalError("替换草图的 regions 必须是数组。")
            entities = [SemanticSketchEntity.model_validate(item) for item in raw_entities]
            entity_ids = {item.id for item in entities}
            if len(entity_ids) != len(entities):
                raise ProposalError("替换草图中存在重复图元 ID。")
            coordinate_space = payload.get("coordinateSpace", "normalized")
            if coordinate_space not in {"normalized", "model"}:
                raise ProposalError("coordinateSpace 只能是 normalized 或 model。")
            if coordinate_space == "normalized":
                entities = _normalize_entities(candidate, entities)
            regions = [SemanticSketchRegion.model_validate(item) for item in raw_regions]
            candidate.sketch.entities = entities
            candidate.sketch.regions = regions
            # A topology replacement starts a new sketch design pass. Reusing
            # constraints merely because semantic IDs happen to match can pull
            # the new outline back toward the previous default rectangle.
            candidate.sketch.constraints = []
            sketch_changed = True
        elif command.type == "upsertParameter":
            value = ParameterDefinition.model_validate(_merged_payload(candidate.parameterDefinitions, target_id, payload))
            candidate.parameterDefinitions = _upsert(candidate.parameterDefinitions, value, target_id)
        elif command.type == "removeParameter":
            referenced = target_id in candidate.sketch.drivingParameters or any(
                item.parameterId == target_id or target_id in item.parameterRefs
                for item in [*candidate.sketch.constraints, *candidate.sketch.entities]
            )
            if referenced:
                raise ProposalError(f"参数 {target_id} 仍被草图引用，不能删除。")
            candidate.parameterDefinitions = [item for item in candidate.parameterDefinitions if item.id != target_id]
        elif command.type == "upsertSketchEntity":
            value = SemanticSketchEntity.model_validate(_merged_payload(candidate.sketch.entities, target_id, payload))
            candidate.sketch.entities = _upsert(candidate.sketch.entities, value, target_id)
            sketch_changed = True
        elif command.type == "removeSketchEntity":
            if any(target_id in item.entityRefs for item in candidate.sketch.constraints) or any(
                target_id in item.boundaryRefs for item in candidate.sketch.regions
            ):
                raise ProposalError(f"图元 {target_id} 仍被约束或区域引用，不能删除。")
            candidate.sketch.entities = [item for item in candidate.sketch.entities if item.id != target_id]
            sketch_changed = True
        elif command.type == "upsertSketchConstraint":
            value = SemanticSketchConstraint.model_validate(_merged_payload(candidate.sketch.constraints, target_id, payload))
            candidate.sketch.constraints = _upsert(candidate.sketch.constraints, value, target_id)
            sketch_changed = True
        elif command.type == "removeSketchConstraint":
            candidate.sketch.constraints = [item for item in candidate.sketch.constraints if item.id != target_id]
            sketch_changed = True
        elif command.type == "upsertSketchRegion":
            value = SemanticSketchRegion.model_validate(_merged_payload(candidate.sketch.regions, target_id, payload))
            candidate.sketch.regions = _upsert(candidate.sketch.regions, value, target_id)
            sketch_changed = True
        elif command.type == "removeSketchRegion":
            candidate.sketch.regions = [item for item in candidate.sketch.regions if item.id != target_id]
            sketch_changed = True
        elif command.type == "setGeometryRecipe":
            allowed = {"constructionMode", "sketches", "paths"}
            unknown = set(payload) - allowed
            if unknown:
                raise ProposalError(f"几何配方包含不允许字段：{', '.join(sorted(unknown))}")
            candidate.geometryRecipe = candidate.geometryRecipe.model_copy(update=payload)
            recipe_changed = True
        elif command.type == "upsertGeometryOperation":
            value = GeometryOperationDefinition.model_validate(
                _merged_payload(candidate.geometryRecipe.operations, target_id, payload)
            )
            if value.operator == "solid.import":
                raise ProposalError("solid.import 尚未实现，不能将其加入可执行提案。")
            candidate.geometryRecipe.operations = _upsert(candidate.geometryRecipe.operations, value, target_id)
            recipe_changed = True
        elif command.type == "removeGeometryOperation":
            candidate.geometryRecipe.operations = [
                item for item in candidate.geometryRecipe.operations if item.id != target_id
            ]
            recipe_changed = True

    if sketch_changed:
        candidate.sketch.acquisitionMethod = "manual"
        candidate.sketch.constraintsReviewed = False
        candidate.sketch.conversionReviewed = False
    if recipe_changed:
        candidate.geometryRecipe.reviewed = False
    _validate_references(candidate)
    return TemplateDraft.model_validate(candidate.model_dump()), commands


# 提案应用后做引用完整性检查，防止出现悬空参数、图元、约束或区域引用。
def _validate_references(draft: TemplateDraft) -> None:
    parameter_ids = {item.id for item in draft.parameterDefinitions}
    entity_ids = {item.id for item in draft.sketch.entities}
    for entity in draft.sketch.entities:
        missing = set(entity.parameterRefs) - parameter_ids
        if missing:
            raise ProposalError(f"图元 {entity.id} 引用了未定义参数：{', '.join(sorted(missing))}")
    for constraint in draft.sketch.constraints:
        missing_entities = set(constraint.entityRefs) - entity_ids
        if missing_entities:
            raise ProposalError(f"约束 {constraint.id} 引用了不存在的图元：{', '.join(sorted(missing_entities))}")
        if constraint.parameterId and constraint.parameterId not in parameter_ids:
            raise ProposalError(f"约束 {constraint.id} 引用了未定义参数 {constraint.parameterId}")
    for region in draft.sketch.regions:
        missing = set(region.boundaryRefs) - entity_ids
        if missing:
            raise ProposalError(f"区域 {region.id} 引用了不存在的图元：{', '.join(sorted(missing))}")
    missing_drivers = set(draft.sketch.drivingParameters) - parameter_ids
    if missing_drivers:
        raise ProposalError(f"草图驱动参数未定义：{', '.join(sorted(missing_drivers))}")


# 生成提案预览差异，供前端或 MCP 调用方展示“改了什么”。
def proposal_diff(draft: TemplateDraft, candidate: TemplateDraft, commands: list[AIModelCommand]) -> dict[str, Any]:
    labels = {
        "setSketchSettings": "草图设置", "replaceSketchGeometry": "草图拓扑", "upsertParameter": "参数", "removeParameter": "参数",
        "upsertSketchEntity": "图元", "removeSketchEntity": "图元",
        "upsertSketchConstraint": "约束/尺寸", "removeSketchConstraint": "约束/尺寸",
        "upsertSketchRegion": "区域", "removeSketchRegion": "区域",
        "setGeometryRecipe": "几何配方", "upsertGeometryOperation": "几何算子",
        "removeGeometryOperation": "几何算子",
    }
    return {
        "commands": [
            {**command.model_dump(), "category": labels[command.type]}
            for command in commands
        ],
        "before": {
            "parameters": len(draft.parameterDefinitions), "entities": len(draft.sketch.entities),
            "constraints": len(draft.sketch.constraints), "regions": len(draft.sketch.regions),
            "operations": len(draft.geometryRecipe.operations),
        },
        "after": {
            "parameters": len(candidate.parameterDefinitions), "entities": len(candidate.sketch.entities),
            "constraints": len(candidate.sketch.constraints), "regions": len(candidate.sketch.regions),
            "operations": len(candidate.geometryRecipe.operations),
        },
    }

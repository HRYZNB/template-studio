from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ARTIFACT_ROOT, ATTACHMENT_ROOT, MATERIAL_DATABASE, LOCAL_DATABASE, PLATFORM_ROOT

LIB_ROOT = PLATFORM_ROOT / "libs" / "python"
WORKER_ROOT = PLATFORM_ROOT / "services" / "cad-worker"
for path in (LIB_ROOT, WORKER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from template_core.lowering import lower_to_plan  # noqa: E402
from template_core.material import RuiWareMaterialLibrary, effective_thickness_domain, material_requirement_mismatches  # noqa: E402
from template_core.models import (  # noqa: E402
    CompileRequest, CompileResult, PublishedVersion, SourceAttachment,
    StageName, StageValidation, TemplateDraft,
)
from template_core.metamodel import AIProposal, MaterialRequirement, Scalar, TemplateEvaluation  # noqa: E402
from template_core.rules import evaluate_template  # noqa: E402
from template_core.registries import TEMPLATE_AUTHORING_REGISTRY  # noqa: E402
from template_core.stages import STAGE_ORDER, validate_stage  # noqa: E402
from template_core.sketch_solver import solve_semantic_sketch  # noqa: E402

from .repository import DuplicateCodeError, Repository, RevisionConflictError  # noqa: E402
from .ai_actions import AIModelProposal, ProposalError, apply_proposal, proposal_diff  # noqa: E402


class BindingRequest(BaseModel):
    sourceRecordId: str
    mode: Literal["reference", "copy"]


class NewDraftRequest(BaseModel):
    name: str = "未命名零部件模板"


class StageActionResult(BaseModel):
    draft: TemplateDraft
    validation: StageValidation


class PublishResult(BaseModel):
    draft: TemplateDraft
    version: PublishedVersion
    validation: StageValidation


class ProposalPreviewRequest(BaseModel):
    proposal: AIModelProposal
    selectedCommandIds: list[str] | None = None


class ProposalApplyRequest(ProposalPreviewRequest):
    pass


class EvaluationRequest(BaseModel):
    overrides: dict[str, Scalar] = Field(default_factory=dict)
    material: dict[str, object] = Field(default_factory=dict)
    product: dict[str, object] = Field(default_factory=dict)
    component: dict[str, object] = Field(default_factory=dict)
    projectZone: dict[str, object] = Field(default_factory=dict)


class MaterialSearchRequest(BaseModel):
    search: str = Field(default="", max_length=80)
    limit: int = Field(default=100, ge=1, le=500)
    requirement: MaterialRequirement | None = None


class SketchSolveRequest(BaseModel):
    draft: TemplateDraft
    overrides: dict[str, float] = Field(default_factory=dict)


material_library = RuiWareMaterialLibrary(MATERIAL_DATABASE)
repository = Repository(LOCAL_DATABASE, material_library)

app = FastAPI(
    title="RuiWare Monolithic Part Template API",
    version="1.0.0",
    description="Monolithic industrial part template authoring, deterministic CAD validation and immutable publishing.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/artifacts", StaticFiles(directory=ARTIFACT_ROOT), name="artifacts")
app.mount("/uploads", StaticFiles(directory=ATTACHMENT_ROOT), name="uploads")


@app.get("/api/v1/registries/template-authoring")
def template_authoring_registry():
    return TEMPLATE_AUTHORING_REGISTRY


@app.post("/api/v1/sketches/solve")
def solve_sketch(request: SketchSolveRequest):
    return solve_semantic_sketch(request.draft, request.overrides)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _next_template_code() -> str:
    index = 1
    while not repository.code_is_unique(f"RW-TPL-{index:04d}"):
        index += 1
    return f"RW-TPL-{index:04d}"


def _save(draft: TemplateDraft, *, reason: str) -> TemplateDraft:
    try:
        return repository.save_draft(draft, expected_revision=draft.revision if draft.id else None, reason=reason)
    except RevisionConflictError as error:
        raise HTTPException(status_code=409, detail=f"草稿已被其他操作更新：{error}") from error
    except DuplicateCodeError as error:
        raise HTTPException(status_code=409, detail=f"模板编码已存在：{error}") from error


def _draft(draft_id: str) -> TemplateDraft:
    try:
        return repository.get_draft(draft_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="模板草稿不存在") from error


def _material_sample_contexts(draft: TemplateDraft) -> list[dict]:
    requirement = draft.materialRequirements[0] if draft.materialRequirements else None
    contexts: list[dict] = []
    for sample in draft.materialValidationSamples:
        try:
            material, provenance = repository.resolve_binding(sample.bindingId)
            contexts.append({"sampleId": sample.id, "material": material, "provenance": provenance, "mismatches": material_requirement_mismatches(requirement, material) if requirement else []})
        except KeyError:
            continue
    return contexts


def _nominal_material_context(draft: TemplateDraft) -> dict | None:
    contexts = _material_sample_contexts(draft)
    by_id = {item["sampleId"]: item for item in contexts}
    nominal = next((item for item in draft.materialValidationSamples if item.role == "nominal"), None)
    fallback = next((item for item in draft.materialValidationSamples if item.requiredForAdmission), None)
    selected = nominal or fallback
    return by_id.get(selected.id) if selected else None


def _stage_context(draft: TemplateDraft) -> tuple[list[dict], CompileResult | None, str | None]:
    material_samples = _material_sample_contexts(draft)
    expected_hash = None
    nominal = _nominal_material_context(draft)
    if nominal:
        expected_hash = lower_to_plan(draft, {"record": nominal["material"], "provenance": nominal["provenance"]}).inputHash
    latest = repository.latest_compile(draft.id) if draft.id else None
    return material_samples, latest, expected_hash


def _validate(stage: StageName, draft: TemplateDraft) -> StageValidation:
    material_samples, latest, expected_hash = _stage_context(draft)
    return validate_stage(
        stage, draft,
        code_unique=repository.code_is_unique(draft.code, draft.id),
        material_samples=material_samples, compile_result=latest, expected_hash=expected_hash,
    )


@app.get("/api/v1/health")
def health() -> dict[str, object]:
    return {
        "status": "ok", "version": app.version,
        "materialDatabase": str(MATERIAL_DATABASE),
        "materialDatabaseAvailable": MATERIAL_DATABASE.exists(),
        "cadWorker": "process-isolated-opencascade",
    }


@app.get("/api/v1/material-sources")
def material_sources() -> list[dict[str, object]]:
    return [{
        "id": material_library.source_id, "name": "RuiWare 已有材料库",
        "kind": "sqlite-readonly", "available": MATERIAL_DATABASE.exists(),
        "capabilities": ["reference", "copy"],
    }]


@app.get("/api/v1/materials")
def materials(search: str = Query(default="", max_length=80), limit: int = 100, draft_id: str | None = None):
    try:
        rows = material_library.list(search=search, limit=limit)
        if not draft_id:
            return rows
        draft = _draft(draft_id)
        requirement = draft.materialRequirements[0] if draft.materialRequirements else None
        return [{**row, "requirementMatch": {"compatible": not (reasons := material_requirement_mismatches(requirement, row)), "reasons": reasons}} for row in rows]
    except (sqlite3.Error, OSError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/v1/materials/search")
def search_materials(request: MaterialSearchRequest):
    """Match against the unsaved requirement currently being edited in the UI."""
    try:
        rows = material_library.list(search=request.search, limit=request.limit)
        if request.requirement is None:
            return rows
        return [{**row, "requirementMatch": {"compatible": not (reasons := material_requirement_mismatches(request.requirement, row)), "reasons": reasons}} for row in rows]
    except (sqlite3.Error, OSError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/v1/material-bindings")
def material_bindings():
    return repository.list_bindings()


@app.post("/api/v1/material-bindings", status_code=201)
def create_material_binding(request: BindingRequest):
    try:
        return repository.create_binding(request.sourceRecordId, request.mode)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="材料记录不存在") from error


@app.get("/api/v1/material-bindings/{binding_id}/resolved")
def resolve_material_binding(binding_id: str):
    try:
        material, provenance = repository.resolve_binding(binding_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="材料绑定不存在") from error
    return {"material": material, "provenance": provenance}


@app.post("/api/v1/template-drafts/blank", response_model=TemplateDraft, status_code=201)
def create_blank_template_draft(request: NewDraftRequest):
    draft = TemplateDraft(
        code=_next_template_code(), name=request.name.strip() or "未命名零部件模板",
    )
    return repository.save_draft(draft, reason="create")


@app.get("/api/v1/template-drafts", response_model=list[TemplateDraft])
def list_template_drafts(includeArchived: bool = False):
    return repository.list_drafts(include_archived=includeArchived)


@app.post("/api/v1/template-drafts", response_model=TemplateDraft, status_code=201)
def create_template_draft(draft: TemplateDraft):
    if draft.id and repository.get_draft_optional(draft.id, include_archived=True):
        raise HTTPException(status_code=409, detail="草稿 ID 已存在")
    return _save(draft.model_copy(update={"id": None}), reason="create")


@app.get("/api/v1/template-drafts/{draft_id}", response_model=TemplateDraft)
def get_template_draft(draft_id: str):
    return _draft(draft_id)


@app.put("/api/v1/template-drafts/{draft_id}", response_model=TemplateDraft)
def update_template_draft(draft_id: str, draft: TemplateDraft):
    if draft.id not in (None, draft_id):
        raise HTTPException(status_code=409, detail="路径 ID 与草稿 ID 不一致")
    _draft(draft_id)
    candidate = draft.model_copy(update={"id": draft_id})
    solve = solve_semantic_sketch(candidate)
    candidate = _sync_sketch_seed_coordinates(candidate, solve)
    return _save(candidate, reason="manual-save")


@app.post("/api/v1/template-drafts/{draft_id}/duplicate", response_model=TemplateDraft, status_code=201)
def duplicate_template_draft(draft_id: str):
    try:
        return repository.duplicate_draft(draft_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="模板草稿不存在") from error


@app.delete("/api/v1/template-drafts/{draft_id}", status_code=204)
def archive_template_draft(draft_id: str):
    try:
        repository.archive_draft(draft_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="模板草稿不存在") from error


@app.post("/api/v1/template-drafts/{draft_id}/restore", response_model=TemplateDraft)
def restore_template_draft(draft_id: str):
    try:
        return repository.restore_draft(draft_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="归档草稿不存在") from error
    except DuplicateCodeError as error:
        raise HTTPException(status_code=409, detail=f"恢复失败，编码已被占用：{error}") from error


@app.get("/api/v1/template-drafts/{draft_id}/revisions")
def list_template_revisions(draft_id: str):
    try:
        return repository.list_revisions(draft_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="模板草稿不存在") from error


@app.post("/api/v1/template-drafts/{draft_id}/revisions/{revision}/restore", response_model=TemplateDraft)
def restore_template_revision(draft_id: str, revision: int):
    try:
        return repository.restore_revision(draft_id, revision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="草稿或修订不存在") from error


@app.get("/api/v1/template-drafts/{draft_id}/stages/{stage}/validate", response_model=StageValidation)
def validate_template_stage(draft_id: str, stage: StageName):
    return _validate(stage, _draft(draft_id))


@app.post("/api/v1/template-drafts/{draft_id}/stages/{stage}/complete", response_model=StageActionResult)
def complete_template_stage(draft_id: str, stage: StageName):
    draft = _draft(draft_id)
    index = STAGE_ORDER.index(stage)
    if index > 0 and getattr(draft.stageStatus, STAGE_ORDER[index - 1]) != "complete":
        raise HTTPException(status_code=409, detail=f"请先完成阶段：{STAGE_ORDER[index - 1]}")
    validation = _validate(stage, draft)
    if not validation.complete:
        return StageActionResult(draft=draft, validation=validation)
    stage_status = draft.stageStatus.model_copy(update={stage: "complete"})
    completed = _save(draft.model_copy(update={"stageStatus": stage_status}), reason=f"complete-{stage}")
    return StageActionResult(draft=completed, validation=validation)


ALLOWED_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".dxf", ".dwg", ".step", ".stp", ".txt", ".csv"}


class AttachmentUpdateRequest(BaseModel):
    description: str = Field(default="", max_length=1000)
    kind: Literal["referenceImage", "drawing", "specification", "other"] | None = None


@app.post("/api/v1/template-drafts/{draft_id}/attachments", response_model=TemplateDraft)
async def upload_template_attachment(
    draft_id: str, request: Request,
    filename: str = Query(min_length=1, max_length=180),
    kind: Literal["referenceImage", "drawing", "specification", "other"] = "other",
):
    draft = _draft(draft_id)
    safe_name = Path(filename).name
    if Path(safe_name).suffix.lower() not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=415, detail="不支持该附件格式")
    content = await request.body()
    if not content:
        raise HTTPException(status_code=422, detail="附件不能为空")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="单个附件不能超过 20 MB")
    digest = hashlib.sha256(content).hexdigest()
    directory = ATTACHMENT_ROOT / digest
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_name
    if not target.exists():
        target.write_bytes(content)
    attachment = SourceAttachment(
        id=f"asset-{uuid.uuid4().hex[:12]}", filename=safe_name,
        mediaType=request.headers.get("content-type", "application/octet-stream").split(";")[0],
        kind=kind,
        size=len(content), sha256=digest,
        url=f"/uploads/{digest}/{safe_name}", createdAt=_now(),
    )
    attachments = [item for item in draft.attachments if item.sha256 != digest] + [attachment]
    return _save(draft.model_copy(update={"attachments": attachments}), reason="add-attachment")


@app.patch("/api/v1/template-drafts/{draft_id}/attachments/{attachment_id}", response_model=TemplateDraft)
def update_template_attachment(draft_id: str, attachment_id: str, request: AttachmentUpdateRequest):
    draft = _draft(draft_id)
    found = False
    attachments: list[SourceAttachment] = []
    for item in draft.attachments:
        if item.id != attachment_id:
            attachments.append(item)
            continue
        found = True
        attachments.append(item.model_copy(update={
            "description": request.description.strip(),
            "kind": request.kind or item.kind,
        }))
    if not found:
        raise HTTPException(status_code=404, detail="附件不存在")
    return _save(draft.model_copy(update={"attachments": attachments}), reason="update-attachment-metadata")


@app.delete("/api/v1/template-drafts/{draft_id}/attachments/{attachment_id}", response_model=TemplateDraft)
def remove_template_attachment(draft_id: str, attachment_id: str):
    draft = _draft(draft_id)
    attachments = [item for item in draft.attachments if item.id != attachment_id]
    if len(attachments) == len(draft.attachments):
        raise HTTPException(status_code=404, detail="附件不存在")
    return _save(draft.model_copy(update={"attachments": attachments}), reason="remove-attachment")


def _write_source_package(draft: TemplateDraft) -> Path:
    package_directory = ARTIFACT_ROOT / "packages"
    package_directory.mkdir(parents=True, exist_ok=True)
    target = package_directory / f"{draft.code or draft.id}-r{draft.revision}.rwpart"
    latest = repository.latest_compile(draft.id) if draft.id else None
    documents = {
        "manifest.json": draft.model_dump(exclude={"parameterDefinitions", "variants", "sketch", "blank", "admission", "materialRequirements", "materialValidationSamples", "geometryRecipe", "featureRules", "interfaces", "evidence", "aiProposals"}),
        "classification.json": {"templateKind": draft.templateKind, "manufacturing": draft.manufacturingClassification.model_dump(), "geometryPrototypeId": draft.geometryPrototypeId, "registryVersion": TEMPLATE_AUTHORING_REGISTRY.version},
        "evidence.json": {"items": [item.model_dump() for item in draft.evidence], "aiProposals": [item.model_dump() for item in draft.aiProposals]},
        "material-requirements.json": {"requirements": [item.model_dump() for item in draft.materialRequirements], "effectiveThicknessDomains": {item.id: effective_thickness_domain(item) for item in draft.materialRequirements}, "blank": draft.blank.model_dump()},
        "material-validation.json": {"samples": [item.model_dump() for item in draft.materialValidationSamples], "resolved": _material_sample_contexts(draft)},
        "parameters.json": {"definitions": [item.model_dump() for item in draft.parameterDefinitions]},
        "parameter-dependencies.json": {"sources": {item.id: item.sourceDefinition.model_dump() if item.sourceDefinition else None for item in draft.parameterDefinitions}},
        "geometry-recipe.json": draft.geometryRecipe.model_dump(),
        "feature-rules.json": {"reviewed": draft.featureRulesReviewed, "rules": [item.model_dump() for item in draft.featureRules]},
        "variants.json": {"variants": [item.model_dump() for item in draft.variants]},
        "constraints.json": draft.sketch.model_dump(),
        "sketch-solver.json": solve_semantic_sketch(draft),
        "interfaces.json": {"coordinateSystem": draft.coordinateSystem, "interfaces": [item.model_dump() for item in draft.interfaces]},
        "outputs.json": latest.model_dump() if latest else {"outputs": []},
        "admission.json": {"policy": draft.admission.model_dump(), "stageStatus": draft.stageStatus.model_dump()},
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, payload in documents.items():
            package.writestr(name, json.dumps(payload, ensure_ascii=False, indent=2))
        for item in draft.attachments:
            source = ATTACHMENT_ROOT / item.sha256 / item.filename
            if source.exists():
                package.write(source, f"assets/{item.id}/{item.filename}")
    return target


@app.get("/api/v1/template-drafts/{draft_id}/source-package")
def download_source_package(draft_id: str):
    target = _write_source_package(_draft(draft_id))
    return FileResponse(target, media_type="application/octet-stream", filename=target.name)


def _run_worker(plan) -> CompileResult:
    work_directory = ARTIFACT_ROOT / "_jobs" / f"job-{uuid.uuid4().hex[:12]}"
    work_directory.mkdir(parents=True, exist_ok=True)
    plan_path, result_path = work_directory / "plan.json", work_directory / "result.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    environment = os.environ.copy()
    paths = [str(LIB_ROOT), str(WORKER_ROOT)]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    process = subprocess.run(
        [sys.executable, "-m", "cad_worker.cli", "--plan", str(plan_path), "--output", str(ARTIFACT_ROOT), "--result", str(result_path)],
        cwd=PLATFORM_ROOT, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    if not result_path.exists():
        return CompileResult(success=False, inputHash=plan.inputHash, diagnostics=[{
            "severity": "error", "code": "WORKER_PROCESS_FAILED", "path": "worker",
            "message": process.stderr[-2000:] or "CAD Worker 未返回结果。",
        }])
    return CompileResult.model_validate_json(result_path.read_text(encoding="utf-8"))


@app.post("/api/v1/template-drafts/{draft_id}/compile", response_model=CompileResult)
def compile_template_draft(draft_id: str):
    draft = _draft(draft_id)
    required = STAGE_ORDER[:5]
    missing = [stage for stage in required if getattr(draft.stageStatus, stage) != "complete"]
    if missing:
        raise HTTPException(status_code=409, detail=f"请先完成前置阶段：{', '.join(missing)}")
    nominal = _nominal_material_context(draft)
    if nominal is None:
        raise HTTPException(status_code=422, detail="尚未配置可解析的标称材料验证样例")
    result = _run_worker(lower_to_plan(draft, {"record": nominal["material"], "provenance": nominal["provenance"]}))
    repository.record_compile(draft.id, result.model_dump())
    return result


@app.post("/api/v1/template-drafts/{draft_id}/evaluate", response_model=TemplateEvaluation)
def evaluate_template_draft(draft_id: str, request: EvaluationRequest):
    draft = _draft(draft_id)
    context = {
        "material": request.material, "product": request.product, "component": request.component,
        "projectZone": request.projectZone,
    }
    return evaluate_template(
        draft.parameterDefinitions, draft.featureRules, request.overrides, context,
        semantic_faces=draft.geometryRecipe.semanticFaces,
        interfaces=draft.interfaces,
    )


@app.get("/api/v1/template-drafts/{draft_id}/compile-runs/latest", response_model=CompileResult | None)
def latest_compile_run(draft_id: str):
    _draft(draft_id)
    return repository.latest_compile(draft_id)


@app.post("/api/v1/compile-preview", response_model=CompileResult)
def compile_preview(request: CompileRequest):
    return _run_worker(lower_to_plan(request.draft, request.materialSnapshot))


@app.get("/api/v1/template-drafts/{draft_id}/versions", response_model=list[PublishedVersion])
def list_published_versions(draft_id: str):
    _draft(draft_id)
    return repository.list_versions(draft_id)


def _proposal_candidate(draft: TemplateDraft, request: ProposalPreviewRequest) -> tuple[TemplateDraft, list[Any]]:
    try:
        return apply_proposal(draft, request.proposal, request.selectedCommandIds)
    except ProposalError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"提案命令数据不符合模板元模型：{error}") from error


def _sync_sketch_seed_coordinates(
    candidate: TemplateDraft, solve: dict[str, Any],
) -> TemplateDraft:
    """Persist the nominal solved geometry as the next deterministic seed.

    Parameter constraints make the solver output authoritative. Keeping stale
    input coordinates makes the object inspector disagree with the canvas and
    causes later edits to start from a different shape than the user reviewed.
    """
    if not solve.get("valid"):
        return candidate
    nominal = next(
        (item for item in solve.get("cases", []) if item.get("case") == "nominal"),
        None,
    )
    if not nominal or not nominal.get("valid"):
        return candidate
    primitives = {
        item.get("id"): item
        for item in nominal.get("primitives", [])
        if isinstance(item, dict) and item.get("id")
    }
    entities = []
    for entity in candidate.sketch.entities:
        primitive = primitives.get(entity.id)
        if not primitive:
            entities.append(entity)
            continue

        def point(name: str):
            value = primitive.get(name)
            if not isinstance(value, dict):
                return None
            x, y = value.get("x"), value.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return None
            return (float(x), float(y))

        update: dict[str, Any] = {}
        if entity.geometryType == "point":
            update["start"] = point("start")
        elif entity.geometryType == "line":
            update.update(start=point("start"), end=point("end"))
        elif entity.geometryType in {"circle", "arc"}:
            update.update(center=point("center"), radius=primitive.get("radius"))
            if entity.geometryType == "arc":
                update.update(
                    start=point("start"), end=point("end"),
                    startAngle=primitive.get("startAngle"),
                    endAngle=primitive.get("endAngle"),
                )
        entities.append(entity.model_copy(update=update))
    candidate.sketch.entities = entities
    return TemplateDraft.model_validate(candidate.model_dump())


@app.post("/api/v1/template-drafts/{draft_id}/proposals/preview")
def preview_proposal(draft_id: str, request: ProposalPreviewRequest):
    draft = _draft(draft_id)
    candidate, commands = _proposal_candidate(draft, request)
    solve = solve_semantic_sketch(candidate)
    candidate = _sync_sketch_seed_coordinates(candidate, solve)
    validation = _validate("baseSketch", candidate)
    return {
        "proposal": request.proposal.model_dump(),
        "candidate": candidate.model_dump(),
        "diff": proposal_diff(draft, candidate, commands),
        "solve": solve,
        "validation": validation.model_dump(),
        "canAccept": bool(commands) and bool(solve.get("valid")),
    }


@app.post("/api/v1/template-drafts/{draft_id}/proposals/apply", response_model=TemplateDraft)
def apply_template_proposal(draft_id: str, request: ProposalApplyRequest):
    draft = _draft(draft_id)
    candidate, commands = _proposal_candidate(draft, request)
    if not commands:
        raise HTTPException(status_code=422, detail="未选择任何提案命令。")
    solve = solve_semantic_sketch(candidate)
    if not solve.get("valid"):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "草图未通过几何求解，不能应用提案。请先修改提案或取消有冲突的命令。",
                "diagnostics": solve.get("diagnostics", []),
            },
        )
    candidate = _sync_sketch_seed_coordinates(candidate, solve)
    audit = {
        "id": request.proposal.id,
        "stage": "baseSketch",
        "summary": request.proposal.summary,
        "confidence": request.proposal.confidence,
        "operations": [
            {
                "action": "replace" if command.type.startswith("set") or command.type.startswith("upsert") else "remove",
                "path": f"{command.type}/{command.targetId}",
                "value": command.payload,
            }
            for command in commands
        ],
        "affectedObjects": [command.targetId for command in commands if command.targetId],
        "risks": request.proposal.assumptions,
        "requiredConfirmations": request.proposal.requiredConfirmations,
        "status": "accepted",
    }
    candidate.aiProposals = [item for item in candidate.aiProposals if item.id != request.proposal.id]
    candidate.aiProposals.append(AIProposal.model_validate(audit))
    return _save(candidate, reason=f"proposal-apply-{request.proposal.taskType}")


@app.post("/api/v1/template-drafts/{draft_id}/publish", response_model=PublishResult)
def publish_template(draft_id: str):
    draft = _draft(draft_id)
    if draft.lifecycleStatus == "published" and draft.stageStatus.admission == "complete":
        raise HTTPException(status_code=409, detail="当前修订已发布；请先修改模板并重新完成受影响阶段。")
    validation = _validate("admission", draft)
    if not validation.complete:
        raise HTTPException(status_code=422, detail=validation.model_dump())
    latest = repository.latest_compile(draft_id)
    if latest is None:
        raise HTTPException(status_code=422, detail="缺少CAD编译记录")
    status = draft.stageStatus.model_copy(update={"admission": "complete"})
    released = _save(draft.model_copy(update={"stageStatus": status, "lifecycleStatus": "published"}), reason="publish")
    package = _write_source_package(released)
    version = repository.publish(released, latest, f"/artifacts/packages/{package.name}")
    return PublishResult(draft=released, version=version, validation=validation)

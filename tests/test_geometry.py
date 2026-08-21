import math

import pytest

from cad_worker.geometry import execute_plan
from template_core.lowering import lower_to_plan

from test_lowering import draft
from template_core.models import TemplateDraft
from template_core.sketch_solver import solve_semantic_sketch


def test_open_cascade_generates_valid_brep_and_preview(tmp_path) -> None:
    plan = lower_to_plan(draft(2), {"record": {"code": "Q345", "grade": "Q345"}})
    result = execute_plan(plan, tmp_path)
    assert result.success, result.diagnostics
    assert result.metrics is not None
    assert result.metrics.valid
    assert result.metrics.solidCount == 1
    assert {item.kind for item in result.artifacts} == {"step", "stl", "plan", "semanticMap", "diagnostics"}
    job_root = tmp_path / result.inputHash[:16]
    assert (job_root / "model.step").stat().st_size > 0
    assert (job_root / "preview.stl").stat().st_size > 0


def test_section_dimensions_change_authoritative_brep(tmp_path) -> None:
    small = TemplateDraft(name="small profile")
    large = TemplateDraft(name="large profile")
    next(item for item in small.parameterDefinitions if item.id == "sectionWidth").default = 80
    next(item for item in large.parameterDefinitions if item.id == "sectionWidth").default = 160
    small_result = execute_plan(lower_to_plan(small, {"record": {"code": "Q345"}}), tmp_path / "small")
    large_result = execute_plan(lower_to_plan(large, {"record": {"code": "Q345"}}), tmp_path / "large")
    assert small_result.success, small_result.diagnostics
    assert large_result.success, large_result.diagnostics
    assert small_result.inputHash != large_result.inputHash
    assert small_result.metrics is not None and large_result.metrics is not None
    assert abs(small_result.metrics.volume - large_result.metrics.volume) > 1


def test_multi_region_circle_hole_compiles_as_one_stable_solid(tmp_path) -> None:
    value = TemplateDraft(name="annular profile")
    value.sketch = value.sketch.model_validate({
        "profileMode":"multiRegion",
        "entities":[
            {"id":"circle.outer","role":"section.outer","geometryType":"circle","center":(0,0),"radius":50},
            {"id":"circle.inner","role":"section.inner","geometryType":"circle","center":(0,0),"radius":30}
        ],
        "constraints":[
            {"id":"outer.fixed","constraintType":"fixed","entityRefs":["circle.outer"]},
            {"id":"inner.fixed","constraintType":"fixed","entityRefs":["circle.inner"]},
            {"id":"circles.concentric","constraintType":"concentric","entityRefs":["circle.outer","circle.inner"]}
        ],
        "regions":[
            {"id":"region.outer","boundaryRefs":["circle.outer"],"operation":"add"},
            {"id":"region.inner","boundaryRefs":["circle.inner"],"operation":"subtract"}
        ]
    })
    plan = lower_to_plan(value, {"record":{"code":"Q345"}})
    result = execute_plan(plan, tmp_path)
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1
    assert result.metrics.volume == pytest.approx(math.pi * (50**2 - 30**2) * 1000, rel=1e-5)


def test_centerline_thinwall_compiles_as_one_stable_solid(tmp_path) -> None:
    value = TemplateDraft(name="centerline thin-wall profile")
    value.sketch = value.sketch.model_validate({
        "profileMode": "centerlineThinWall",
        "drivingParameters": ["thickness"],
        "entities": [
            {"id":"wall.left","role":"section.centerline.left","geometryType":"line","start":(-50,30),"end":(-50,-30),"parameterRefs":["thickness"]},
            {"id":"wall.base","role":"section.centerline.base","geometryType":"line","start":(-50,-30),"end":(50,-30),"parameterRefs":["thickness"]},
            {"id":"wall.right","role":"section.centerline.right","geometryType":"line","start":(50,-30),"end":(50,30),"parameterRefs":["thickness"]},
        ],
        "constraints": [
            {"id":"path.connected","constraintType":"coincident","entityRefs":["wall.left","wall.base","wall.right"]},
            {"id":"path.fixed","constraintType":"fixed","entityRefs":["wall.left","wall.base","wall.right"]},
        ],
        "regions": [],
    })
    value.geometryRecipe.operations[0].operator = "sketch.centerline_thinwall_extrude"
    value.geometryRecipe.operations[0].argumentExpressions = {"length":"length", "thickness":"thickness"}
    result = execute_plan(lower_to_plan(value, {"record":{"code":"Q345"}}), tmp_path)
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1
    assert result.metrics.volume > 0


@pytest.mark.parametrize("plane", ["XZ", "YZ"])
def test_centerline_thinwall_respects_sketch_plane(tmp_path, plane) -> None:
    value = TemplateDraft(name=f"thin-wall {plane}")
    value.sketch.profileMode = "centerlineThinWall"
    value.sketch.plane = plane
    value.sketch.regions = []
    value.geometryRecipe.operations[0].operator = "sketch.centerline_thinwall_extrude"
    value.geometryRecipe.operations[0].argumentExpressions = {"length":"length", "thickness":"thickness"}
    result = execute_plan(lower_to_plan(value, {"record":{"code":"Q345"}}), tmp_path / plane)
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1


def _compile_operator(tmp_path, operator: str, arguments: dict[str, object]):
    value = TemplateDraft(name=f"{operator} test")
    operation = value.geometryRecipe.operations[0]
    operation.operator = operator
    operation.arguments = {key: item for key, item in arguments.items() if isinstance(item, (str, int, float, bool))}
    operation.argumentExpressions = {}
    return execute_plan(lower_to_plan(value, {"record": {"code": "Q345"}}), tmp_path)


@pytest.mark.parametrize("plane", ["XY", "XZ", "YZ"])
def test_revolve_compiles_closed_profile_on_each_reference_plane(tmp_path, plane) -> None:
    value = TemplateDraft(name=f"{plane} revolve")
    value.sketch.plane = plane
    value.geometryRecipe.operations[0].operator = "solid.revolve"
    value.geometryRecipe.operations[0].argumentExpressions = {}
    value.geometryRecipe.operations[0].arguments = {
        "axisOriginU": -75,
        "axisOriginV": 0,
        "axisDirectionU": 0,
        "axisDirectionV": 1,
        "angleDegrees": 360,
    }
    result = execute_plan(lower_to_plan(value, {"record":{"code":"Q345"}}), tmp_path)
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1
    assert result.metrics.volume > 0


def test_sweep_compiles_profile_along_straight_path(tmp_path) -> None:
    value = TemplateDraft(name="parameterized sweep")
    value.geometryRecipe.operations[0].operator = "solid.sweep"
    value.geometryRecipe.operations[0].argumentExpressions = {}
    value.geometryRecipe.operations[0].arguments = {"pathPoints": "0:0:0;0:0:length * 0.25"}
    result = execute_plan(lower_to_plan(value, {"record":{"code":"Q345"}}), tmp_path)
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1
    assert result.metrics.volume == pytest.approx(100 * 50 * 250, rel=1e-4)


def test_sweep_supports_right_corner_polyline_and_hollow_regions(tmp_path) -> None:
    value = TemplateDraft(name="right-corner hollow sweep")
    value.sketch = value.sketch.model_validate({
        "profileMode":"multiRegion",
        "entities":[
            {"id":"circle.outer","role":"section.outer","geometryType":"circle","center":(0,0),"radius":20},
            {"id":"circle.inner","role":"section.inner","geometryType":"circle","center":(0,0),"radius":15},
        ],
        "constraints":[
            {"id":"outer.fixed","constraintType":"fixed","entityRefs":["circle.outer"]},
            {"id":"inner.fixed","constraintType":"fixed","entityRefs":["circle.inner"]},
            {"id":"circles.concentric","constraintType":"concentric","entityRefs":["circle.outer","circle.inner"]},
        ],
        "regions":[
            {"id":"region.outer","boundaryRefs":["circle.outer"],"operation":"add"},
            {"id":"region.inner","boundaryRefs":["circle.inner"],"operation":"subtract"},
        ],
    })
    value.geometryRecipe.operations[0].operator = "solid.sweep"
    value.geometryRecipe.operations[0].argumentExpressions = {}
    value.geometryRecipe.operations[0].arguments = {"pathPoints":"0:0:0;0:0:100;100:0:100"}
    result = execute_plan(lower_to_plan(value, {"record":{"code":"Q345"}}), tmp_path)
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1
    assert result.metrics.volume > 0


@pytest.mark.parametrize("plane", ["XY", "XZ", "YZ"])
def test_loft_compiles_scaled_sections_on_each_reference_plane(tmp_path, plane) -> None:
    value = TemplateDraft(name="parameterized loft")
    value.sketch.plane = plane
    value.geometryRecipe.operations[0].operator = "solid.loft"
    value.geometryRecipe.operations[0].argumentExpressions = {}
    value.geometryRecipe.operations[0].arguments = {"stations": "0:1;length * 0.15:0.6;length * 0.3:1.25"}
    result = execute_plan(lower_to_plan(value, {"record":{"code":"Q345"}}), tmp_path)
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1
    assert result.metrics.volume > 0


def test_loft_preserves_multi_region_hollow_topology(tmp_path) -> None:
    value = TemplateDraft(name="hollow loft")
    value.sketch = value.sketch.model_validate({
        "profileMode":"multiRegion",
        "entities":[
            {"id":"circle.outer","role":"section.outer","geometryType":"circle","center":(0,0),"radius":50},
            {"id":"circle.inner","role":"section.inner","geometryType":"circle","center":(0,0),"radius":30},
        ],
        "constraints":[
            {"id":"outer.fixed","constraintType":"fixed","entityRefs":["circle.outer"]},
            {"id":"inner.fixed","constraintType":"fixed","entityRefs":["circle.inner"]},
            {"id":"circles.concentric","constraintType":"concentric","entityRefs":["circle.outer","circle.inner"]},
        ],
        "regions":[
            {"id":"region.outer","boundaryRefs":["circle.outer"],"operation":"add"},
            {"id":"region.inner","boundaryRefs":["circle.inner"],"operation":"subtract"},
        ],
    })
    value.geometryRecipe.operations[0].operator = "solid.loft"
    value.geometryRecipe.operations[0].argumentExpressions = {}
    value.geometryRecipe.operations[0].arguments = {"stations":"0:1;200:0.6"}
    result = execute_plan(lower_to_plan(value, {"record":{"code":"Q345"}}), tmp_path)
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1
    assert 0 < result.metrics.volume < math.pi * 50**2 * 200


def test_sheet_bend_compiles_single_continuous_solid(tmp_path) -> None:
    result = _compile_operator(tmp_path, "sheet.bend", {
        "length": 300,
        "width": 80,
        "thickness": 2,
        "bendPosition": 180,
        "bendAngleDegrees": 90,
        "insideRadius": 3,
        "kFactor": 0.42,
    })
    assert result.success, result.diagnostics
    assert result.metrics is not None and result.metrics.solidCount == 1
    assert result.metrics.volume == pytest.approx(300 * 80 * 2, rel=0.02)


def test_constraint_contract_rejects_wrong_selection_count_and_type() -> None:
    value = TemplateDraft(name="invalid constraint selection")
    value.sketch.constraints.append(
        value.sketch.constraints[0].model_validate({
            "id":"invalid.perpendicular",
            "constraintType":"perpendicular",
            "entityRefs":["edge.bottom"],
        })
    )
    value.sketch.constraints.append(
        value.sketch.constraints[0].model_validate({
            "id":"invalid.radius",
            "constraintType":"radius",
            "entityRefs":["edge.bottom"],
            "value":10,
        })
    )
    solution = solve_semantic_sketch(value)
    codes = {item["code"] for item in solution["diagnostics"]}
    assert "SKETCH_CONSTRAINT_SELECTION_INVALID" in codes
    assert "SKETCH_CONSTRAINT_ENTITY_TYPE_INVALID" in codes

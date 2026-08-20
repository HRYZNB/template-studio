"""模板语义的安全表达式求值和规则展开。

规则引擎负责解析参数、制造特征和零部件接口，同时拒绝不安全或
不确定的表达式，保证同一输入得到同一结果。
"""

from __future__ import annotations

import ast
import math
from collections import defaultdict, deque
from typing import Any, Mapping

from .metamodel import (
    EvaluationDiagnostic,
    SemanticFaceDefinition,
    FeatureRule,
    ParameterDefinition,
    PartInterface,
    ResolvedFeature,
    ResolvedInterface,
    Scalar,
    TemplateEvaluation,
)


class RuleEvaluationError(ValueError):
    pass


_FUNCTIONS = {
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "max": max,
    "min": min,
    "round": round,
    "sqrt": math.sqrt,
    "clamp": lambda value, minimum, maximum: max(minimum, min(maximum, value)),
}


class _SafeEvaluator:
    """受限表达式解释器，只允许工程规则需要的安全语法。"""

    def __init__(self, context: Mapping[str, Any]):
        self.context = context

    def evaluate(self, expression: str) -> Scalar:
        if len(expression) > 1_000:
            raise RuleEvaluationError("expression exceeds 1000 characters")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as error:
            raise RuleEvaluationError(f"invalid expression syntax: {error.msg}") from error
        if sum(1 for _ in ast.walk(tree)) > 200:
            raise RuleEvaluationError("expression is too complex")
        value = self._node(tree.body)
        if not isinstance(value, (int, float, bool, str)):
            raise RuleEvaluationError(f"expression returned unsupported type: {type(value).__name__}")
        if isinstance(value, float) and not math.isfinite(value):
            raise RuleEvaluationError("expression returned a non-finite number")
        return value

    def _node(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool, str)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.context:
                return self.context[node.id]
            raise RuleEvaluationError(f"unknown name: {node.id}")
        if isinstance(node, ast.Attribute):
            value = self._node(node.value)
            if node.attr.startswith("_") or not isinstance(value, Mapping) or node.attr not in value:
                raise RuleEvaluationError(f"unknown reference: {node.attr}")
            return value[node.attr]
        if isinstance(node, ast.BinOp):
            left, right = self._node(node.left), self._node(node.right)
            operations = {
                ast.Add: lambda: left + right,
                ast.Sub: lambda: left - right,
                ast.Mult: lambda: left * right,
                ast.Div: lambda: left / right,
                ast.FloorDiv: lambda: left // right,
                ast.Mod: lambda: left % right,
                ast.Pow: lambda: left ** right,
            }
            operation = operations.get(type(node.op))
            if operation is None:
                raise RuleEvaluationError(f"operator not allowed: {type(node.op).__name__}")
            try:
                result = operation()
            except (ArithmeticError, TypeError) as error:
                raise RuleEvaluationError(str(error)) from error
            if type(node.op) is ast.Pow and isinstance(result, (int, float)) and abs(result) > 1e12:
                raise RuleEvaluationError("power result exceeds safety limit")
            return result
        if isinstance(node, ast.UnaryOp):
            value = self._node(node.operand)
            if isinstance(node.op, ast.USub): return -value
            if isinstance(node.op, ast.UAdd): return +value
            if isinstance(node.op, ast.Not): return not value
            raise RuleEvaluationError(f"unary operator not allowed: {type(node.op).__name__}")
        if isinstance(node, ast.BoolOp):
            values = [bool(self._node(item)) for item in node.values]
            if isinstance(node.op, ast.And): return all(values)
            if isinstance(node.op, ast.Or): return any(values)
            raise RuleEvaluationError("boolean operator not allowed")
        if isinstance(node, ast.Compare):
            left = self._node(node.left)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._node(comparator)
                comparisons = {
                    ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
                    ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right,
                    ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right,
                }
                if type(operator) not in comparisons:
                    raise RuleEvaluationError(f"comparison not allowed: {type(operator).__name__}")
                try:
                    passed = comparisons[type(operator)]()
                except TypeError as error:
                    raise RuleEvaluationError(str(error)) from error
                if not passed: return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self._node(node.body if self._node(node.test) else node.orelse)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS or node.keywords:
                raise RuleEvaluationError("only approved positional functions are allowed")
            return _FUNCTIONS[node.func.id](*(self._node(argument) for argument in node.args))
        raise RuleEvaluationError(f"syntax not allowed: {type(node).__name__}")


# 求值单个表达式；外部只应通过这个入口使用安全表达式能力。
def evaluate_expression(expression: str, context: Mapping[str, Any]) -> Scalar:
    return _SafeEvaluator(context).evaluate(expression)


# 提取表达式中引用的名称，用于建立参数依赖图。
def expression_names(expression: str) -> set[str]:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise RuleEvaluationError(f"invalid expression syntax: {error.msg}") from error
    return {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in _FUNCTIONS and node.id not in {"True", "False"}
    }


def _parameter_dependencies(definition: ParameterDefinition, parameter_ids: set[str]) -> set[str]:
    source = definition.sourceDefinition
    if source is None:
        return set()
    dependencies = set(source.dependencies)
    if source.expression:
        dependencies |= expression_names(source.expression) & parameter_ids
    if source.type == "lookup" and source.reference:
        try:
            dependencies |= expression_names(source.reference) & parameter_ids
        except RuleEvaluationError:
            pass
    return dependencies


# 对参数定义做拓扑排序，保证公式参数先拿到依赖值。
def parameter_evaluation_order(definitions: list[ParameterDefinition]) -> list[str]:
    by_id = {item.id: item for item in definitions}
    if len(by_id) != len(definitions):
        raise RuleEvaluationError("parameter IDs must be unique")
    ids = set(by_id)
    dependencies = {item.id: _parameter_dependencies(item, ids) for item in definitions}
    for parameter_id, required in dependencies.items():
        unknown = required - ids
        if unknown:
            raise RuleEvaluationError(f"parameter {parameter_id} depends on unknown parameters: {', '.join(sorted(unknown))}")
    outgoing: dict[str, set[str]] = defaultdict(set)
    indegree = {parameter_id: len(required) for parameter_id, required in dependencies.items()}
    for parameter_id, required in dependencies.items():
        for dependency in required:
            outgoing[dependency].add(parameter_id)
    queue = deque(sorted(parameter_id for parameter_id, count in indegree.items() if count == 0))
    order: list[str] = []
    while queue:
        current = queue.popleft()
        order.append(current)
        for target in sorted(outgoing[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(order) != len(definitions):
        cyclic = sorted(parameter_id for parameter_id, count in indegree.items() if count > 0)
        raise RuleEvaluationError(f"cyclic parameter dependency: {', '.join(cyclic)}")
    return order


def _reference(path: str | None, context: Mapping[str, Any]) -> Scalar | None:
    if not path:
        return None
    current: Any = context
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current if isinstance(current, (int, float, bool, str)) else None


def _coerce(definition: ParameterDefinition, value: Scalar) -> Scalar:
    try:
        if definition.valueType == "number": value = float(value)
        elif definition.valueType == "integer": value = int(value)
        elif definition.valueType == "boolean": value = bool(value)
        elif definition.valueType in {"string", "enum"}: value = str(value)
    except (TypeError, ValueError) as error:
        raise RuleEvaluationError(f"parameter {definition.id} has invalid {definition.valueType} value") from error
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if definition.minimum is not None and value < definition.minimum:
            raise RuleEvaluationError(f"parameter {definition.id} is below minimum {definition.minimum}")
        if definition.maximum is not None and value > definition.maximum:
            raise RuleEvaluationError(f"parameter {definition.id} exceeds maximum {definition.maximum}")
    if definition.allowedValues and value not in definition.allowedValues:
        raise RuleEvaluationError(f"parameter {definition.id} is not an allowed value")
    return value


# 按参数来源、默认值、覆盖值和上下文解析最终参数值。
def resolve_parameters(
    definitions: list[ParameterDefinition],
    overrides: Mapping[str, Scalar] | None = None,
    external_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Scalar], list[str], list[EvaluationDiagnostic]]:
    overrides = overrides or {}
    external = dict(external_context or {})
    diagnostics: list[EvaluationDiagnostic] = []
    values: dict[str, Scalar] = {}
    try:
        order = parameter_evaluation_order(definitions)
    except RuleEvaluationError as error:
        return values, [], [EvaluationDiagnostic(severity="error", code="PARAMETER_DEPENDENCY_INVALID", path="parameterDefinitions", message=str(error))]
    by_id = {item.id: item for item in definitions}
    unknown_overrides = set(overrides) - set(by_id)
    if unknown_overrides:
        diagnostics.append(EvaluationDiagnostic(severity="error", code="UNKNOWN_PARAMETER_OVERRIDE", path="overrides", message=f"unknown overrides: {', '.join(sorted(unknown_overrides))}"))
    for parameter_id in order:
        definition = by_id[parameter_id]
        source = definition.sourceDefinition
        context = {**external, **values}
        try:
            if parameter_id in overrides:
                value = overrides[parameter_id]
            elif source is None or source.type == "userInput":
                value = definition.default
            elif source.type == "constant":
                value = source.fallback if source.fallback is not None else definition.default
            elif source.type == "formula":
                if not source.expression:
                    raise RuleEvaluationError("formula source requires expression")
                value = evaluate_expression(source.expression, context)
            elif source.type == "lookup":
                if not source.reference:
                    raise RuleEvaluationError("lookup source requires reference")
                key = str(evaluate_expression(source.reference, context))
                value = source.lookupTable.get(key, source.fallback)
                if value is None:
                    raise RuleEvaluationError(f"lookup key not found: {key}")
            else:
                value = _reference(source.reference, external)
                if value is None:
                    value = source.fallback
                if value is None:
                    value = definition.default
            values[parameter_id] = _coerce(definition, value)
        except RuleEvaluationError as error:
            diagnostics.append(EvaluationDiagnostic(severity="error", code="PARAMETER_EVALUATION_FAILED", path=f"parameterDefinitions.{parameter_id}", message=str(error)))
    return values, order, diagnostics


_DEFAULT_SEMANTIC_FACES = [SemanticFaceDefinition(id="part.face.front", label="前侧面", hostFrame="negativeY")]


# 将动态特征规则展开为确定数量的孔、槽、切口等静态特征。
def resolve_feature_rules(
    rules: list[FeatureRule], context: Mapping[str, Any], semantic_faces: list[SemanticFaceDefinition] | None = None,
) -> tuple[list[ResolvedFeature], list[EvaluationDiagnostic]]:
    features: list[ResolvedFeature] = []
    diagnostics: list[EvaluationDiagnostic] = []
    faces = {item.id: item for item in (semantic_faces or _DEFAULT_SEMANTIC_FACES)}
    for rule in sorted((item for item in rules if item.enabled), key=lambda item: item.id):
        path = f"featureRules.{rule.id}"
        try:
            if not bool(evaluate_expression(rule.conditionExpression, context)):
                continue
            placement = rule.placement
            if placement.mode == "single":
                count = 1
            elif placement.mode != "maxPitch":
                raw_count = evaluate_expression(rule.countExpression, context)
                if isinstance(raw_count, bool) or not isinstance(raw_count, (int, float)) or int(raw_count) != raw_count:
                    raise RuleEvaluationError("count expression must return an integer")
                count = int(raw_count)
            if not rule.faceBindings:
                raise RuleEvaluationError("feature rule requires at least one semantic face binding")
            dimensions: dict[str, Any] = {}
            for dimension in rule.profileDimensions:
                value = context.get(dimension.parameterId)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RuleEvaluationError(f"profile dimension {dimension.id} must reference a numeric parameter")
                dimensions[dimension.id] = value
            for binding in sorted(rule.faceBindings, key=lambda item: item.semanticFaceId):
                face = faces.get(binding.semanticFaceId)
                if face is None:
                    raise RuleEvaluationError(f"semantic face not found: {binding.semanticFaceId}")
                face_context = {**context, **dimensions}
                axis_start = 0.0
                usable_span = 0.0
                if placement.mode in {"linearArray", "equalSpan", "maxPitch"}:
                    axis_start_expression = face.uStartExpression if placement.axis == "u" else face.vStartExpression
                    axis_span_expression = face.uSpanExpression if placement.axis == "u" else face.vSpanExpression
                    start = evaluate_expression(placement.startMarginExpression, face_context)
                    end = evaluate_expression(placement.endMarginExpression, face_context)
                    axis_start_value = evaluate_expression(axis_start_expression, face_context)
                    axis_span_value = evaluate_expression(axis_span_expression, face_context)
                    values = (start, end, axis_start_value, axis_span_value)
                    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
                        raise RuleEvaluationError("placement margins and semantic-face bounds must be numeric")
                    if start < 0 or end < 0 or axis_span_value <= 0:
                        raise RuleEvaluationError("placement margins must be non-negative and face span must be greater than zero")
                    axis_start = float(axis_start_value + start)
                    usable_span = float(axis_span_value - start - end)
                    if placement.mode in {"equalSpan", "maxPitch"} and usable_span < 0:
                        raise RuleEvaluationError("start and end margins exceed the semantic-face span")
                    if placement.mode == "maxPitch":
                        maximum_pitch = evaluate_expression(placement.maximumPitchExpression, face_context)
                        if isinstance(maximum_pitch, bool) or not isinstance(maximum_pitch, (int, float)) or maximum_pitch <= 0:
                            raise RuleEvaluationError("maximum-pitch placement requires a positive numeric maximum pitch")
                        count = max(2, math.ceil(usable_span / maximum_pitch) + 1)
                if count < 0 or count > rule.maximumCount:
                    raise RuleEvaluationError(f"resolved count {count} is outside 0..{rule.maximumCount}")
                for index in range(count):
                    item_context = {**context, **dimensions, rule.indexVariable: index, "count": count}
                    arguments = dict(rule.arguments)
                    for name, expression in sorted(rule.argumentExpressions.items()):
                        arguments[name] = evaluate_expression(expression, item_context)
                    coordinate_key = "x" if placement.axis == "u" else "z"
                    base_coordinate = arguments.get(coordinate_key, 0)
                    if isinstance(base_coordinate, bool) or not isinstance(base_coordinate, (int, float)):
                        raise RuleEvaluationError(f"placement coordinate {coordinate_key} must be numeric")
                    offset = 0.0
                    automatic_position = placement.mode in {"linearArray", "equalSpan", "maxPitch"}
                    if placement.mode in {"linearArray", "symmetric"}:
                        pitch = evaluate_expression(placement.pitchExpression, item_context)
                        if isinstance(pitch, bool) or not isinstance(pitch, (int, float)):
                            raise RuleEvaluationError("placement pitch must be numeric")
                        offset = index * pitch if placement.mode == "linearArray" else (index - (count - 1) / 2) * pitch
                    elif placement.mode in {"equalSpan", "maxPitch"}:
                        offset = 0.0 if count <= 1 else index * usable_span / (count - 1)
                    coordinate = axis_start + offset if automatic_position else float(base_coordinate + offset)
                    arguments[coordinate_key] = float(coordinate)
                    vertices: list[tuple[float, float]] = []
                    if rule.featureType == "polygonalCutout":
                        if len(rule.polygonVertices) < 3:
                            raise RuleEvaluationError("polygonal cutout requires at least three vertices")
                        for vertex in rule.polygonVertices:
                            u = evaluate_expression(vertex.uExpression, item_context)
                            v = evaluate_expression(vertex.vExpression, item_context)
                            if isinstance(u, bool) or isinstance(v, bool) or not isinstance(u, (int, float)) or not isinstance(v, (int, float)):
                                raise RuleEvaluationError("polygon vertex expressions must return numbers")
                            if not math.isfinite(u) or not math.isfinite(v):
                                raise RuleEvaluationError("polygon vertex coordinates must be finite")
                            translation = coordinate if automatic_position else offset
                            vertices.append((float(u + translation), float(v)) if placement.axis == "u" else (float(u), float(v + translation)))
                        _validate_polygon(vertices)
                    features.append(ResolvedFeature(
                        id=f"{rule.id}.{binding.semanticFaceId.replace('.', '_')}.{index + 1:03d}", featureType=rule.featureType,
                        arguments=arguments, semanticFaceId=face.id, hostFace=face.hostFrame,
                        polygonVertices=vertices, sourceRuleId=rule.id, index=index,
                    ))
        except RuleEvaluationError as error:
            diagnostics.append(EvaluationDiagnostic(severity="error", code="FEATURE_RULE_EVALUATION_FAILED", path=path, message=str(error)))
    ids = [item.id for item in features]
    if len(ids) != len(set(ids)):
        diagnostics.append(EvaluationDiagnostic(severity="error", code="RESOLVED_FEATURE_ID_DUPLICATE", path="featureRules", message="resolved feature IDs are not unique"))
    return features, diagnostics


def _validate_polygon(vertices: list[tuple[float, float]]) -> None:
    """Reject degenerate/self-intersecting straight-edge profiles before CAD lowering."""
    if any(math.dist(a, b) <= 1e-8 for a, b in zip(vertices, vertices[1:] + vertices[:1])):
        raise RuleEvaluationError("polygon contains a zero-length edge")
    area2 = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(vertices, vertices[1:] + vertices[:1]))
    if abs(area2) <= 1e-8:
        raise RuleEvaluationError("polygon area must be greater than zero")

    def orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    edge_count = len(vertices)
    for i in range(edge_count):
        a, b = vertices[i], vertices[(i + 1) % edge_count]
        for j in range(i + 1, edge_count):
            if j in {i, (i + 1) % edge_count} or (i == 0 and j == edge_count - 1):
                continue
            c, d = vertices[j], vertices[(j + 1) % edge_count]
            if orientation(a, b, c) * orientation(a, b, d) <= 0 and orientation(c, d, a) * orientation(c, d, b) <= 0:
                raise RuleEvaluationError("polygon edges must not self-intersect or overlap")


# 模板试算总入口：解析参数、展开特征、解析接口并返回诊断。
def evaluate_template(
    definitions: list[ParameterDefinition],
    rules: list[FeatureRule],
    overrides: Mapping[str, Scalar] | None = None,
    external_context: Mapping[str, Any] | None = None,
    semantic_faces: list[SemanticFaceDefinition] | None = None,
    interfaces: list[PartInterface] | None = None,
) -> TemplateEvaluation:
    values, order, diagnostics = resolve_parameters(definitions, overrides, external_context)
    features: list[ResolvedFeature] = []
    if not any(item.severity == "error" for item in diagnostics):
        features, feature_diagnostics = resolve_feature_rules(rules, {**dict(external_context or {}), **values}, semantic_faces)
        diagnostics.extend(feature_diagnostics)
    resolved_interfaces, interface_diagnostics = resolve_part_interfaces(interfaces or [], rules, features)
    diagnostics.extend(interface_diagnostics)
    return TemplateEvaluation(
        values=values,
        evaluationOrder=order,
        features=features,
        resolvedInterfaces=resolved_interfaces,
        diagnostics=diagnostics,
    )


# 根据静态定义或已解析特征生成对外接口结果。
def resolve_part_interfaces(
    interfaces: list[PartInterface],
    rules: list[FeatureRule],
    features: list[ResolvedFeature],
) -> tuple[list[ResolvedInterface], list[EvaluationDiagnostic]]:
    """Expand a feature-derived declaration into one stable occurrence per resolved feature."""

    resolved: list[ResolvedInterface] = []
    diagnostics: list[EvaluationDiagnostic] = []
    rule_ids = {rule.id for rule in rules}

    for interface in interfaces:
        if interface.declarationMode == "staticGeometry":
            resolved.append(ResolvedInterface(
                id=interface.id,
                sourceInterfaceId=interface.id,
                declarationMode="staticGeometry",
                interfaceType=interface.interfaceType,
                geometryRefs=interface.geometryRefs,
                parameterRefs=interface.parameterRefs,
            ))
            continue

        rule_id = interface.sourceFeatureRuleId
        if not rule_id or rule_id not in rule_ids:
            diagnostics.append(EvaluationDiagnostic(
                severity="error",
                code="INTERFACE_RULE_EVALUATION_FAILED",
                path=f"interfaces.{interface.id}.sourceFeatureRuleId",
                message=f"Feature-derived interface {interface.id} references an unavailable feature rule.",
            ))
            continue

        for feature in (item for item in features if item.sourceRuleId == rule_id):
            suffix = feature.id.removeprefix(f"{rule_id}.")
            resolved.append(ResolvedInterface(
                id=f"{interface.id}.{suffix}",
                sourceInterfaceId=interface.id,
                declarationMode="featureDerived",
                interfaceType=interface.interfaceType,
                geometryRefs=[feature.semanticFaceId],
                parameterRefs=interface.parameterRefs,
                sourceFeatureRuleId=rule_id,
                sourceFeatureId=feature.id,
            ))

    seen: set[str] = set()
    for item in resolved:
        if item.id in seen:
            diagnostics.append(EvaluationDiagnostic(
                severity="error",
                code="INTERFACE_ID_DUPLICATE",
                path=f"interfaces.{item.sourceInterfaceId}",
                message=f"Resolved interface id {item.id} is duplicated.",
            ))
        seen.add(item.id)
    return resolved, diagnostics

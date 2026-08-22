# 当前实现架构

当前模块定位为“单体零部件模板生成器”，不是完整模板工程平台。它只发布最终为一个连续实体的零部件模板。

```text
studio-web
  └─ Template API
       ├─ 独立平台 SQLite（草稿、绑定、编译记录）
       ├─ 只读 RuiWareMaterialLibrary（现有 ruiware.db，仅作为材料数据源）
       └─ 子进程协议
            └─ CAD Worker
                 ├─ 参数/约束预检
                 ├─ 集合 Lowering
                 ├─ OpenCascade B-Rep
                 └─ STEP / STL / 计划 / 语义映射 / 诊断
```

关键边界：

- 单体模板生成器不依赖其他模板生成器的运行逻辑；
- RuiWare 材料库只作为业务数据源，只读访问；
- Web API 不持有 OpenCascade 对象；
- CAD Worker 只接受不可变静态计划；
- UI、API、测试和后续 AI 都只能调用同一条 Lowering/CAD 链路；
- 孔等集合在进入几何内核前已展开，算子无需理解“2/4/6 孔”特例；
- 公共语义接口使用稳定业务 ID，不公开 Face/Edge 顺序号。
- `templateKind` 固定为 `monolithicPart`，作为未来模板工程平台进行生成器路由的稳定契约；
- 焊接组合件、装配组件和产品模板不得通过扩展当前 CAD 算子进入本模块。

## 目录职责

- `apps/studio-web`：单体零部件模板开发工作台；
- `services/template-api`：材料绑定、草稿修订和任务入口；
- `services/cad-worker`：隔离的 OpenCascade 执行器；
- `libs/python/template_core`：无 UI/数据库依赖的领域模型和 Lowering；
- `packages/contracts`：跨语言协议事实源；
- `tests`：集合、材料和真实几何测试；
- `artifacts`：内容哈希目录下的生成工件；
- `data`：新平台自己的运行数据库。

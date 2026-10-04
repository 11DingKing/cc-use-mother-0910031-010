# 医美项目分级备案

本项目维护医美项目分级备案的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖机构合规员、执业人员、监管人员、复核专家，并明确项目分类版本、依赖完整性检查、备案历史冻结、规则影响分析等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/procedure_filing/`：分级备案后端服务（见下节）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性回归测试与后端行为测试。

## 后端服务

`procedure_filing.FilingService` 是内存后端，角色与状态词汇和 `domain/contract.json` 对齐：

- **项目目录与别名**：`register_project` / `add_alias` / `resolve_project` / `find_similar`。名称与别名全局唯一、精确解析，名称相近的项目（如“光子嫩肤”与“光子嫩肤术”）各自适用不同的风险等级、场地与人员要求；`current_classification` 按名称或别名返回当前生效分类。
- **备案流程**：`submit_filing` 先运行依赖完整性检查（设备、人员、场地），通过后进入待核验；`review_filing` 按执业人员 → 复核专家 → 监管人员的顺序多角色复核，退回则回到登记并开启新一轮；监管决定后经 `archive_filing` 归档冻结。
- **版本链**：`correct_classification`（分类更正）、`split_project`（项目拆分）、`substitute_dependency`（依赖替代）、`emergency_suspend`（紧急暂停）都在项目版本链上追加新版本，旧版本保留；暂停/拆分后停止新备案、复核与服务登记。
- **历史冻结**：`record_service` 登记的服务记录始终指向备案当时钉住的分类版本，后续规则变更不改写历史。
- **分析接口**：`diff_versions` 比较两个分类版本的差异（风险升降、场地、人员与设备增减），`impact_analysis` 列出规则变更影响的机构授权、在途申请与仍指向旧版本的历史服务。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

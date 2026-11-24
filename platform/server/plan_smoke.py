"""F22：update_plan 三状态与最多一个 in_progress 的直接执行测试。"""
from app.services.tools import ToolExecutor

executor = ToolExecutor("workspaces/1/cases/1")
valid = executor.execute("update_plan", {"steps": [
    {"title": "准备", "status": "completed"},
    {"title": "计算", "status": "in_progress"},
    {"title": "报告", "status": "pending"},
]})
assert valid["ok"] and "[completed] 准备" in valid["result"] and "[in_progress] 计算" in valid["result"], valid
print("1. 三状态计划投影 [OK]")

multiple = executor.execute("update_plan", {"steps": [
    {"title": "A", "status": "in_progress"},
    {"title": "B", "status": "in_progress"},
]})
assert not multiple["ok"] and "最多只能有一个 in_progress" in multiple["result"], multiple
print("2. 两个 in_progress 明确拒绝 [OK]")

invalid = executor.execute("update_plan", {"steps": [{"title": "bad", "status": "doing"}]})
assert not invalid["ok"] and "非法计划状态" in invalid["result"], invalid
print("3. 非三状态 fail closed [OK]")
print("\n=== update_plan 状态约束冒烟通过 ===")

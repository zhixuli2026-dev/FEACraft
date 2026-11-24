"""H12：原版 31,542-label 基准与自有上下文合并的精确集合和确定性。"""
import json
from pathlib import Path

from app.services import completion
from app.services.completion_merge import merge_index

baseline_path = Path(r"D:\vibeflow_reverse\extracted\share\vibeflow\completion\openfoam-v2406-keywords.json")
own_path = Path(__file__).parent / "data" / "completion" / "openfoam-keywords.json"
baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
own = json.loads(own_path.read_text(encoding="utf-8"))
merged = merge_index(baseline, own)
repeated = merge_index(baseline, own)
labels = [item["label"] for item in merged["completionItems"]]
assert len(labels) == 31542 and len(set(labels)) == 31542
assert set(labels) == {item["label"] for item in baseline["completionItems"]}
assert merged["index_sha256"] == repeated["index_sha256"]
assert merged["baseline"]["label_count"] == 31542 and len(merged["baseline"]["sha256"]) == 64
assert not any(ord(char) < 32 or ord(char) == 127 for label in labels for char in label)
assert all(0 < item["confidence"] <= 1 for item in merged["completionItems"])
assert merged["completionItems"] == sorted(merged["completionItems"],
    key=lambda item: (-item["confidence"], -item["occurrences"], item["label"]))
print("1. 精确 31,542 唯一 label、无控制字符、重复合并 hash 相同 [OK]")

completion._cache = merged
try:
    assert completion.search("", kind="dictionaryKey", limit=1)["items"]
    assert completion.search("", kind="dictionaryKey:source-backed", limit=1)["items"]
    fixed = completion.search("fixedValue", parent="type", limit=20)["items"]
    assert fixed and fixed[0]["parents"] == ["type"], fixed
    field = completion.search("fixedValue", parent="type", context="U", limit=200)["items"]
    assert field and field[0]["sourceExamples"], field
    assert completion.search("--2D", kind="commandLineToken", limit=10)["items"]
    print("2. 原版细分 kind + 自有通用 kind/parents/context/sourceExamples [OK]")
finally:
    completion._cache = None

print("\n=== 原版 label 基准合并冒烟通过 ===")

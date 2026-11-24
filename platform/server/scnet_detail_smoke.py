"""I18/I19：SCNET 详情模型、脱敏与变更安全 golden。"""
from app.services.scnet import safe_redirect, require_confirmation
from app.services.scnet_detail import copy_summary, detail_model, search_detail

job = detail_model({"id": "j1", "name": "cfd", "status": "running", "duration": 3725,
                    "token": "secret-token", "customCode": 99,
                    "url": "https://example.com/open?token=temporary"}, "jobs")
assert any(field["value"] == "1h 2m 5s" for field in job["fields"])
assert all(field["value"] != "secret-token" for field in job["fields"])
assert not any(field["key"] == "customCode" for field in search_detail(job))
assert any(field["key"] == "customCode" for field in search_detail(job, "custom"))
summary = copy_summary(job)
assert "secret-token" not in summary and "temporary" not in summary
assert "customCode: 99" in summary
panel = detail_model({"name": "desktop", "width": 1920, "length": 1080,
                      "password": "secret", "jobs": "SIG"}, "desktops")
assert panel["fields"][0]["value"] == "1920×1080"
assert "password: ***" in copy_summary(panel)
assert any(field["value"] == "SIG" for field in panel["fields"])
assert safe_redirect("https://example.com/hpc", "/next") == "https://example.com/next"
try:
    safe_redirect("https://example.com/hpc", "https://other.example/next")
except ValueError:
    pass
else:
    raise AssertionError("跨源重定向未拒绝")
try:
    require_confirmation("desktops.delete", False)
except ValueError:
    pass
else:
    raise AssertionError("未确认删除未拒绝")
require_confirmation("desktops.delete", True)
print("I18 detail cards/search/copy/redaction and I19 confirmation/same-origin lifecycle: OK")
print("\n=== SCNET 详情与生命周期安全冒烟通过 ===")

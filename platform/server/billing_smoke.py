"""J06：mock 套餐 checkout、续费、升降级、取消与到期生效。"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def register(label):
    result = call("POST", "/auth/register", {
        "email": f"billing-{label}-{time.time_ns()}@vibeflow.cn", "name": label,
        "password": "billing-pass-123"})
    return result["token"], result["user"]


admin, admin_user = register("admin")
member, member_user = register("member")
extra1, extra1_user = register("extra1")
extra2, extra2_user = register("extra2")
org_id = call("GET", "/org/mine", token=admin)["org"]["id"]
plans = call("GET", "/org/plans", token=admin)
assert {plan["code"] for plan in plans} == {"free", "pro", "team"}, plans
assert next(plan for plan in plans if plan["code"] == "pro")["quotas"]["seats"] == 10
assert call("POST", f"/org/{org_id}/members", {"email": member_user["email"], "role": "member"}, token=admin)["membership"]
assert call("POST", f"/org/{org_id}/invitation:accept", {}, token=member)["ok"] is True
assert call("POST", f"/org/{org_id}/billing:mock-checkout", {"plan_code": "pro"}, token=admin).get("_err") == 400
upgraded = call("POST", f"/org/{org_id}/billing:mock-checkout",
                {"plan_code": "pro", "period": "monthly", "confirmed": True}, token=admin)
assert upgraded["provider"] == "mock" and upgraded["charge"] is False, upgraded
assert upgraded["mock_payment_id"].startswith("mock_"), upgraded
assert upgraded["subscription"]["plan_code"] == "pro", upgraded
for account in (extra1_user, extra2_user):
    assert call("POST", f"/org/{org_id}/members", {"email": account["email"], "role": "viewer"}, token=admin)["membership"]
assert call("POST", f"/org/{org_id}/billing:mock-checkout",
            {"plan_code": "free", "confirmed": True}, token=admin).get("_err") == 409
for account in (extra1_user, extra2_user):
    assert call("DELETE", f"/org/{org_id}/members/{account['id']}", token=admin)["ok"] is True
before = call("GET", f"/org/{org_id}/billing", token=admin)["current"]
renewed = call("POST", f"/org/{org_id}/billing:renew", {"period": "yearly"}, token=admin)["subscription"]
assert renewed["expires_at"] > before["expires_at"], (before, renewed)
billing = call("GET", f"/org/{org_id}/billing", token=admin)
assert billing["current"]["plan_code"] == "pro" and len(billing["history"]) >= 2, billing
assert any(row["status"] == "superseded" for row in billing["history"]), billing
cancelled = call("POST", f"/org/{org_id}/billing:cancel", {}, token=admin)
assert cancelled["ok"] is True and cancelled["subscription"]["status"] == "cancelled", cancelled
assert call("GET", f"/org/{org_id}/billing", token=admin)["current"] is None
assert call("GET", "/org/mine", token=admin)["plan"] is None
for action in ("billing.subscription.changed", "billing.subscription.renewed", "billing.subscription.cancelled"):
    page = call("GET", f"/org/{org_id}/audit?action={action}", token=admin)
    assert page["total"] >= 1, (action, page)

sqlite_path = os.environ.get("VIBEFLOW_TEST_SQLITE")
if sqlite_path:
    again = call("POST", f"/org/{org_id}/billing:mock-checkout",
                 {"plan_code": "pro", "confirmed": True}, token=admin)
    with sqlite3.connect(sqlite_path) as db:
        db.execute("UPDATE subscriptions SET expires_at=? WHERE id=?",
                   ("2000-01-01 00:00:00+00:00", again["subscription"]["id"]))
        db.commit()
    assert call("GET", "/org/mine", token=admin)["plan"] is None
    expired = call("GET", f"/org/{org_id}/billing", token=admin)
    assert expired["current"] is None, expired
    assert any(row["id"] == again["subscription"]["id"] and row["status"] == "expired"
               for row in expired["history"]), expired
    print("1. 隔离 SQLite 到期后付费配额失效并归档 [OK]")
else:
    print("1. 主库不伪造到期时间；隔离模式另验到期 [SKIP]")
print("J06 mock checkout, renew, downgrade seats, cancel, history and audit: OK")
print("\n=== J06 套餐与 mock 支付生命周期冒烟通过 ===")

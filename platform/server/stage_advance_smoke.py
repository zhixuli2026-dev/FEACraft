"""Stage confirmation with actual CadQuery geometry; full C/D/E covered by CAE profile."""
import json
from app.services.casedict import set_block
from testsupport import IsolatedServer

SCRIPT = '''import cadquery as cq
def build_geometry(ctx):
    return cq.Workplane("XY").box(1, 2, 3)
'''


def main():
    with IsolatedServer("stage-advance") as api:
        api.create_case("stage-evidence")
        original = api.call("GET", api.prefix + "/caseDict")
        api.call("PUT", api.prefix + "/caseDict", {"content": set_block(
            original["content"], "caseMeta", {"status": "readyForGeometry"})})
        api.advance("B", expect=409)
        api.advance("C", expect=409)
        api.advance("B", confirmed=False, expect=409)
        preview = api.put("A-geo/geometry.py", SCRIPT)
        api.wait_job(preview["auto_preview_job"])
        api.advance("B", expected_revision=0, expect=409)
        assert api.advance("B")["workflow_state"] == "B"
        mirror = api.call("GET", api.prefix)
        assert json.loads(mirror["case_meta"])["status"] == "readyForMesh"
        api.advance("C", expect=409)
        api.advance("V")
        api.advance("D", expect=409)
        api.advance("A")
        invalid = api.put("A-geo/geometry.py", "raise ValueError('geometry failed')\n")
        api.wait_job(invalid["auto_preview_job"], expected="failed")
        api.advance("B", expect=409)
        print("STAGE_ADVANCE_OK: geometry/status/revision/confirmation/stale geometry/no fake mesh")


if __name__ == "__main__":
    main()

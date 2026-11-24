"""Pure, offline caseDict schema regression checks."""
from app.services.casedict import CASE_DICT_TEMPLATE, parse_casedict, set_block, write_casedict
from app.services.casedict_schema import validate_case_dict_data, error_counts


def make(meta, problem="steady", mesh=""):
    text = CASE_DICT_TEMPLATE.replace("caseMeta\n{", f"caseMeta\n{{\n    {meta}", 1)
    text = text.replace("physicalProblem\n{", f"physicalProblem\n{{\n    timeBehavior {problem};", 1)
    if mesh:
        text = text.replace("mesh\n{", f"mesh\n{{\n    {mesh}", 1)
    return validate_case_dict_data(parse_casedict(text))


def run_checks():
    assert make("analysisType steady;") == []
    assert any(error["code"] == "analysis_mismatch" for error in make("analysisType transient;", "steady"))
    assert make("analysisType harmonic;", "frequencyDomain") == []
    assert make("analysisType modal;", "frequencyDomain") == []
    assert any(error["code"] == "analysis_mismatch" for error in make("analysisType modal;", "periodic"))
    assert any(error["code"] in {"type", "range"} for error in make("", mesh="qualityTargets { maxNonOrtho NaN; }"))
    assert any(error["code"] in {"type", "range"} for error in make("", mesh="qualityTargets { maxSkewness inf; }"))
    assert any(error["code"] == "unsupported" for error in make("units { velocity m/s; }"))
    assert any(error["code"] == "type" for error in make("displayName { value 42; }"))
    assert any(error["code"] == "type" for error in make("tags { value 1; }"))
    invalid_count = CASE_DICT_TEMPLATE.replace("errors\n{\n    // entries { }\n}",
        "errors\n{\n    summary { open 1.5; }\n}")
    assert any(error["code"] == "type" for error in validate_case_dict_data(parse_casedict(invalid_count)))
    print("1. 跨分析/时间、NaN/Inf、未知量纲、字段类型/整数 lint [OK]")

    fields = {"empty":"", "cjk":"中文🙂", "quote":'He said "hello"', "newline":"line1\nline2",
              "path":r"C:\\input\\mesh", "comment":"a//b", "braces":"x;{y}", "plain":"steady"}
    assert parse_casedict(write_casedict(fields)) == fields
    updated = set_block(CASE_DICT_TEMPLATE, "caseMeta", fields)
    assert parse_casedict(updated)["caseMeta"] == fields
    assert "// userIntent" in updated
    assert parse_casedict(write_casedict({"yes":True,"no":False})) == {"yes":"true","no":"false"}
    assert parse_casedict(write_casedict({"metric":'"volume_p.p@0.3"'}))["metric"] == "volume_p.p@0.3"
    print("2. 空字符串、中文、转义与注释字符序列化往返，布尔值小写 [OK]")

    base = {"title":"error", "stage":"mesh", "category":"meshQuality", "severity":"error",
            "status":"open", "blocking":True}
    for blocking in (True, "true", "True", "TRUE"):
        parsed = parse_casedict(set_block(CASE_DICT_TEMPLATE, "errors", {"entries":{"e":dict(base,blocking=blocking)}}))
        assert error_counts(parsed)["blocking"] == 1
    for missing in (None, {}, {"text":""}, {"text":"   "}, {"nested":[{}]}):
        record = dict(base, status="fixProposed", fixOptions=missing)
        parsed = {**parse_casedict(CASE_DICT_TEMPLATE), "errors":{"entries":{"e":record}}}
        assert any(error["path"].endswith("fixOptions") for error in validate_case_dict_data(parsed))
    print("3. 旧大小写布尔值不丢阻断计数，空修复证据不通过门禁 [OK]")


if __name__ == "__main__":
    run_checks()

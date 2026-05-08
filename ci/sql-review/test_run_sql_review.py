import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_sql_review.py")
SPEC = importlib.util.spec_from_file_location("run_sql_review", MODULE_PATH)
assert SPEC and SPEC.loader
run_sql_review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_sql_review)


RULES = run_sql_review.load_rules(Path(__file__).with_name("rules") / "mysql_rules.yaml")


def make_entry(statement_id: str, sql_type: str, raw_sql: str, dynamic: bool = False) -> dict:
    normalized_sql = run_sql_review.normalize_sql(raw_sql)
    return {
        "file": "demo/CommentMapper.xml",
        "statement_id": statement_id,
        "sql_type": sql_type,
        "raw_sql": raw_sql,
        "normalized_sql": normalized_sql,
        "dynamic": dynamic,
    }


def rule_names(findings: list[dict]) -> set[str]:
    return {item["rule"] for item in findings}


def test_mbg_delete_by_example_not_flagged_without_where() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.deleteByExample",
        "DELETE",
        "delete from COMMENT <if> <include:Example_Where_Clause>",
        dynamic=True,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "dml_without_where" not in rule_names(findings)
    assert "dynamic_sql_full_scan_risk" not in rule_names(findings)


def test_dynamic_insert_not_flagged_as_full_scan() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.insertSelective",
        "INSERT",
        "insert into COMMENT <trim> <if> ID, </if> </trim> values <trim> <if> #{id,jdbcType=BIGINT}, </if> </trim>",
        dynamic=True,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "dynamic_sql_full_scan_risk" not in rule_names(findings)


def test_count_query_not_flagged_missing_pagination() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.countByExample",
        "SELECT",
        "select count(*) from COMMENT <if> <include:Example_Where_Clause>",
        dynamic=True,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "missing_pagination" not in rule_names(findings)


def test_rowbounds_query_not_flagged_missing_pagination() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.selectByExampleWithRowbounds",
        "SELECT",
        "select <include:Base_Column_List> from COMMENT <if> <include:Example_Where_Clause> order by ?",
        dynamic=True,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "missing_pagination" not in rule_names(findings)


def test_extract_index_usage_candidates_strip_mybatis_noise() -> None:
    lookup = run_sql_review.extract_index_usage_candidates(
        "select <include:Base_Column_List> from COMMENT where ID = #{id,jdbcType=BIGINT}",
        "SELECT",
    )

    assert lookup["table_refs"] == [{"schema": "", "table": "comment", "alias": "comment"}]
    assert lookup["where_columns"] == ["id"]


def test_real_delete_without_where_still_blocked() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.deleteAll",
        "DELETE",
        "delete from COMMENT",
        dynamic=False,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "dml_without_where" in rule_names(findings)


def test_select_star_still_flagged() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.selectAll",
        "SELECT",
        "select * from COMMENT",
        dynamic=False,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "select_star" in rule_names(findings)


def test_leading_wildcard_like_still_flagged() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.searchByContent",
        "SELECT",
        "select id from COMMENT where content like '%abc'",
        dynamic=False,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "leading_wildcard_like" in rule_names(findings)


def test_long_in_list_still_flagged() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.selectByIds",
        "SELECT",
        "select id from COMMENT where id in (?, ?, ?, ?, ?, ?)",
        dynamic=False,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "long_in_list" in rule_names(findings)


def test_function_on_column_still_flagged() -> None:
    entry = make_entry(
        "com.demo.CommentMapper.selectByDate",
        "SELECT",
        "select id from COMMENT where date(gmt_create) = ?",
        dynamic=False,
    )

    findings = run_sql_review.run_static_rules(entry, RULES)

    assert "function_on_column" in rule_names(findings)


def test_extract_from_xml_keeps_dynamic_include_markers(tmp_path: Path) -> None:
    mapper = tmp_path / "CommentMapper.xml"
    mapper.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="com.demo.CommentMapper">
  <sql id="Example_Where_Clause">
    <where>
      <if test="id != null">
        and ID = #{id,jdbcType=BIGINT}
      </if>
    </where>
  </sql>
  <delete id="deleteByExample">
    delete from COMMENT
    <if test="_parameter != null">
      <include refid="Example_Where_Clause" />
    </if>
  </delete>
</mapper>
""",
        encoding="utf-8",
    )

    entries = run_sql_review.extract_from_xml(mapper)

    assert len(entries) == 1
    assert entries[0]["statement_id"] == "com.demo.CommentMapper.deleteByExample"
    assert "<include:Example_Where_Clause>" in entries[0]["raw_sql"]
    assert entries[0]["dynamic"] is True

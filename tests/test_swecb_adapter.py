from __future__ import annotations

from wevibe_bench.adapters.swecontextbench import _normalize_fail_to_pass, _paths_from_patch


def test_paths_from_patch_extracts_b_side_dedupes_and_handles_empty() -> None:
    single_patch = """diff --git a/tests/test_x.py b/tests/test_x.py
index 1111111..2222222 100644
--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -1 +1 @@
-assert 1 == 2
+assert 1 == 1
"""
    assert _paths_from_patch(single_patch) == ["tests/test_x.py"]

    multi_patch = """diff --git a/tests/test_x.py b/tests/test_x.py
index 1111111..2222222 100644
diff --git a/pkg/test_y.py b/pkg/test_y.py
index 3333333..4444444 100644
diff --git a/tests/test_x.py b/tests/test_x.py
index 5555555..6666666 100644
"""
    assert _paths_from_patch(multi_patch) == ["tests/test_x.py", "pkg/test_y.py"]
    assert _paths_from_patch("") == []


def test_normalize_fail_to_pass_accepts_json_string_or_list() -> None:
    json_encoded = '["repo/tests/test_a.py::test_one", 42, ""]'
    assert _normalize_fail_to_pass(json_encoded) == ["repo/tests/test_a.py::test_one", "42"]

    as_list = ["repo/tests/test_b.py::test_two", 99]
    assert _normalize_fail_to_pass(as_list) == ["repo/tests/test_b.py::test_two", "99"]

    assert _normalize_fail_to_pass(None) == []
    assert _normalize_fail_to_pass("") == []

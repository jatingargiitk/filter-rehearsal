import pytest

from filter_rehearsal.globs import GlobError, glob_match
from filter_rehearsal.versions import compare, parse_release


@pytest.mark.parametrize(
    "pat,text,ok",
    [
        ("*panic*", "kernel PANIC here", True),  # docs: case insensitive
        ("*panic*", "PaNiC", True),
        ("1.2.*", "1.2.3", True),  # docs bash example
        ("1.2.[!3]", "1.2.4", True),
        ("1.2.[!3]", "1.2.3", False),
        ("v?", "v1", True),
        ("v?", "v12", False),
        (r"\*literal", "*literal", True),
        (r"\*literal", "xliteral", False),
        (r"\a", "a", True),  # backslash before non-meta char is ignored
        ("TypeError: *", "TypeError: x is undefined", True),
        ("TypeError: *", "Uncaught TypeError: x", False),  # whole string must match
        ("web@{1,2}.*", "web@2.0", True),
        ("web@{1,2}.*", "web@3.0", False),
        ("*", "", True),
        ("a*b", "a\nb", True),
    ],
)
def test_glob(pat, text, ok):
    assert glob_match(pat, text) is ok


@pytest.mark.parametrize("bad", ["[abc", "a}", "{a,b"])
def test_bad_glob(bad):
    with pytest.raises(GlobError):
        glob_match(bad, "x")


def test_version_rules_from_docs():
    assert compare("lt", "web@1.9.9", "2.0.0")
    assert not compare("lt", "web@2.0.0", "2.0.0")
    assert compare("gte", "web@1.2", "1.2.0")  # missing parts count as zero
    assert compare("lt", "web@1.0.0-rc.1", "1.0.0")  # pre-release sorts below final
    assert compare("lt", "web@1.0.0-alpha", "1.0.0-beta")
    assert compare("lt", "web@1.0.0-rc.2", "1.0.0-rc.10")  # numeric identifiers
    assert not compare("gt", "web@1.0.0+build.9", "1.0.0")  # build ignored
    assert compare("lt", "1.2.3.4", "1.2.3.5")  # four parts
    assert not compare("lt", "4f2a9c1", "2.0.0")  # commit hash never matches
    assert not compare("lt", "api@1.0.0", "web@2.0.0")  # package must match
    assert compare("lt", "api@1.0.0", "2.0.0")  # no package -> every package
    assert not compare("lt", None, "2.0.0")


def test_version_value_needs_version():
    with pytest.raises(ValueError):
        compare("lt", "web@1.0.0", "deadbeef")
    assert parse_release("my-example@1.4.0-beta.1").pre == "beta.1"

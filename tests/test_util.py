import pytest

from sbs.util import (
    format_duration,
    format_size,
    parse_count,
    parse_duration,
    parse_size,
    sparkline,
)


@pytest.mark.parametrize("text,expected", [
    ("7m", 420), ("45s", 45), ("1h30m", 5400), ("2d", 172800), ("10", 600),
    ("1.5h", 5400), ("off", None), ("manuel", None), ("0", None), (" 7M ", 420),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "7x", "m7", "7m junk", "0.1s"])
def test_parse_duration_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize("text,expected", [
    ("2048", 2048), ("500M", 500 * 1024**2), ("5G", 5 * 1024**3), ("1.5GB", int(1.5 * 1024**3)),
    ("10kb", 10240), ("2GiB", 2 * 1024**3), ("none", None), ("0", None),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "5X", "big", "-5M"])
def test_parse_size_invalid(text):
    with pytest.raises(ValueError):
        parse_size(text)


def test_parse_count():
    assert parse_count("50") == 50
    assert parse_count("none") is None
    with pytest.raises(ValueError):
        parse_count("-3")
    with pytest.raises(ValueError):
        parse_count("abc")


def test_formatters():
    assert format_duration(420) == "7dk"
    assert format_duration(5400) == "1sa 30dk"
    assert format_duration(None) == "manuel"
    assert format_size(512) == "512 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(5 * 1024**3) == "5.0 GB"


def test_sparkline():
    assert sparkline([]) == ""
    assert len(sparkline([1, 2, 3])) == 3
    assert sparkline([1, 8])[0] == "▁" and sparkline([1, 8])[-1] == "█"
    assert len(set(sparkline([5, 5, 5]))) == 1

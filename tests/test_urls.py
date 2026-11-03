import pytest

from release_watch.urls import host_of, normalize_url


@pytest.mark.parametrize("url, expected", [
    ("HTTP://Docs.X.com/a/", "https://docs.x.com/a"),
    ("https://x.com/f.pdf#page=12", "https://x.com/f.pdf"),
    ("https://x.com/p?sig=abc&b=2&a=1&utm_source=z", "https://x.com/p?a=1&b=2"),
    ("https://x.com:443/", "https://x.com/"),
    ("http://x.com:80/a", "https://x.com/a"),
    ("https://keepit.com/updates#pmc", "https://keepit.com/updates"),
    ("https://x.com", "https://x.com/"),
])
def test_normalize_url(url, expected):
    assert normalize_url(url) == expected


def test_host_of():
    assert host_of("https://Docs.Cohesity.com:443/x") == "docs.cohesity.com"

"""GitHub URL parsing and ingestion input validation (pure; no network or git)."""

from __future__ import annotations

import pytest

from xraymyrepo.cim import RepositoryRef
from xraymyrepo.ingestion import (
    IngestionInputError,
    IngestionTarget,
    ingestion_target,
    parse_github_url,
    validate_commit_sha,
    validate_ref,
)

SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "0123456789abcdef" * 4
URL = "https://github.com/acme/app"


def repo(owner: str, name: str) -> RepositoryRef:
    return RepositoryRef(host="github.com", owner=owner, name=name)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/app", repo("acme", "app")),
        ("https://github.com/acme/app/", repo("acme", "app")),
        ("https://github.com/acme/app.git", repo("acme", "app")),
        ("https://github.com/acme/app.git/", repo("acme", "app")),
        ("https://github.com/Acme/My-App", repo("acme", "my-app")),
        ("HTTPS://GitHub.COM/acme/app", repo("acme", "app")),
        ("https://github.com/a/b", repo("a", "b")),
        ("https://github.com/acme-co/app_v2.0", repo("acme-co", "app_v2.0")),
        ("https://github.com/acme/.github", repo("acme", ".github")),
        ("https://github.com/acme/app.git.git", repo("acme", "app.git")),
        ("https://github.com/" + "a" * 39 + "/" + "b" * 100, repo("a" * 39, "b" * 100)),
    ],
)
def test_valid_urls(url: str, expected: RepositoryRef) -> None:
    assert parse_github_url(url) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        # empty or malformed
        ("", "non-empty"),
        ("github.com/acme/app", "https://"),
        ("https://", "host must be"),
        ("https:/github.com/acme/app", "https://"),
        ("acme/app", "https://"),
        (" https://github.com/acme/app", "whitespace"),
        ("https://github.com/acme/app\n", "whitespace"),
        ("https://github.com/acme/a\tpp", "whitespace"),
        ("https://github.com/acme/a\x00pp", "printable"),
        ("https://github.com/acmé/app", "ASCII"),
        # wrong scheme
        ("http://github.com/acme/app", "https://"),
        ("ssh://git@github.com/acme/app.git", "https://"),
        ("git://github.com/acme/app.git", "https://"),
        ("file:///etc/passwd", "https://"),
        ("git@github.com:acme/app.git", "https://"),
        # alternate hosts and IP literals
        ("https://gitlab.com/acme/app", "host must be"),
        ("https://www.github.com/acme/app", "host must be"),
        ("https://github.com.evil.com/acme/app", "host must be"),
        ("https://evil.com/github.com/acme/app", "host must be"),
        ("https://github.com./acme/app", "host must be"),
        ("https://140.82.112.3/acme/app", "host must be"),
        ("https://[::1]/acme/app", "host must be"),
        ("https://[2606:50c0::]:443/acme/app", "host must be"),
        ("https:///acme/app", "host must be"),
        # userinfo and ports
        ("https://user@github.com/acme/app", "credentials"),
        ("https://user:token@github.com/acme/app", "credentials"),
        ("https://github.com@evil.com/acme/app", "credentials"),
        ("https://github.com:443/acme/app", "port"),
        ("https://github.com:/acme/app", "port"),
        # queries and fragments
        ("https://github.com/acme/app?tab=readme", "query"),
        ("https://github.com/acme/app?", "query"),
        ("https://github.com/acme/app#readme", "fragment"),
        ("https://github.com/acme/app/#", "fragment"),
        # extra, missing or empty path segments
        ("https://github.com", "/OWNER/NAME"),
        ("https://github.com/", "/OWNER/NAME"),
        ("https://github.com/acme", "/OWNER/NAME"),
        ("https://github.com/acme/", "/OWNER/NAME"),
        ("https://github.com/acme/app/tree/main", "/OWNER/NAME"),
        ("https://github.com/acme/app/extra", "/OWNER/NAME"),
        ("https://github.com/acme/app//", "/OWNER/NAME"),
        ("https://github.com//acme/app", "/OWNER/NAME"),
        ("https://github.com/acme//app", "/OWNER/NAME"),
        # dot segments
        ("https://github.com/./app", "/OWNER/NAME"),
        ("https://github.com/../app", "/OWNER/NAME"),
        ("https://github.com/acme/.", "/OWNER/NAME"),
        ("https://github.com/acme/..", "/OWNER/NAME"),
        ("https://github.com/acme/.git", "/OWNER/NAME"),
        ("https://github.com/acme/..git", "/OWNER/NAME"),
        ("https://github.com/acme/app/..", "/OWNER/NAME"),
        ("https://github.com/acme/../evil/app", "/OWNER/NAME"),
        # percent-encoding and other ambiguous forms
        ("https://github.com/acme/%61pp", "percent-encoding"),
        ("https://github.com/acme/app%2Fextra", "percent-encoding"),
        ("https://github.com/%2e%2e/app", "percent-encoding"),
        ("https://github%2ecom/acme/app", "percent-encoding"),
        ("https://github.com/acme\\app", "backslashes"),
        ("https://github.com\\@evil.com/acme/app", "backslashes"),
        # invalid owner or repository names
        ("https://github.com/-acme/app", "invalid GitHub owner"),
        ("https://github.com/acme-/app", "invalid GitHub owner"),
        ("https://github.com/ac_me/app", "invalid GitHub owner"),
        ("https://github.com/ac.me/app", "invalid GitHub owner"),
        ("https://github.com/" + "a" * 40 + "/app", "invalid GitHub owner"),
        ("https://github.com/acme/ap$p", "invalid GitHub repository name"),
        ("https://github.com/acme/ap+p", "invalid GitHub repository name"),
        ("https://github.com/acme/" + "b" * 101, "invalid GitHub repository name"),
    ],
)
def test_invalid_urls(url: str, message: str) -> None:
    with pytest.raises(IngestionInputError, match=message):
        parse_github_url(url)


def test_errors_are_value_errors() -> None:
    assert issubclass(IngestionInputError, ValueError)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(SHA1, SHA1), (SHA1.upper(), SHA1), (SHA256, SHA256), (SHA256.upper(), SHA256)],
)
def test_valid_commit_shas(value: str, expected: str) -> None:
    assert validate_commit_sha(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc1234",  # abbreviated
        SHA1[:-1],
        SHA1 + "0",
        SHA256[:-1],
        SHA256 + "0",
        "g" * 40,
        f" {SHA1}",
        f"{SHA1}\n",
        f"-{SHA1[1:]}",
        "HEAD",
    ],
)
def test_invalid_commit_shas(value: str) -> None:
    with pytest.raises(IngestionInputError, match="40 or 64 hexadecimal"):
        validate_commit_sha(value)


@pytest.mark.parametrize(
    "value",
    ["main", "v1.2.3", "release/2026-10", "refs/heads/main", "refs/tags/v1.0", "feature_x",
     "HEAD", "abc1234", "a" * 255],
)  # fmt: skip
def test_valid_refs(value: str) -> None:
    assert validate_ref(value) == value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "non-empty"),
        ("a" * 256, "at most 255"),
        ("-b", "start with '-'"),
        ("--upload-pack=evil", "start with '-'"),
        ("-", "start with '-'"),
        ("main branch", "printable ASCII"),
        (" main", "printable ASCII"),
        ("main\n", "printable ASCII"),
        ("ma\x00in", "printable ASCII"),
        ("ma\x1bin", "printable ASCII"),
        ("ma\x7fin", "printable ASCII"),
        ("mäin", "printable ASCII"),
        ("main..other", "invalid ref"),
        ("main@{1}", "invalid ref"),
        ("main~1", "invalid ref"),
        ("main^", "invalid ref"),
        ("main:other", "invalid ref"),
        ("ma?n", "invalid ref"),
        ("ma*n", "invalid ref"),
        ("ma[n", "invalid ref"),
        ("ma\\in", "invalid ref"),
        ("refs//heads", "invalid ref"),
        ("refs/.hidden", "invalid ref"),
        (".hidden", "invalid ref"),
        ("/main", "invalid ref"),
        ("main/", "invalid ref"),
        ("main.", "invalid ref"),
        ("main.lock", "invalid ref"),
        ("@", "invalid ref"),
        (SHA1, "looks like a commit SHA"),
        (SHA256.upper(), "looks like a commit SHA"),
    ],
)
def test_invalid_refs(value: str, message: str) -> None:
    with pytest.raises(IngestionInputError, match=message):
        validate_ref(value)


def test_target_with_commit_sha() -> None:
    target = ingestion_target("https://github.com/Acme/App.git", commit_sha=SHA1.upper())
    assert target == IngestionTarget(repo("acme", "app"), commit_sha=SHA1)
    assert target.ref is None


def test_target_with_ref() -> None:
    assert ingestion_target(URL, ref="main") == IngestionTarget(repo("acme", "app"), ref="main")


@pytest.mark.parametrize(
    ("commit_sha", "ref", "message"),
    [
        (None, None, "exactly one"),
        (SHA1, "main", "exactly one"),
        ("", "", "exactly one"),
        ("", None, "40 or 64 hexadecimal"),
        (None, "", "non-empty"),
        ("abc1234", None, "40 or 64 hexadecimal"),
        (None, "-x", "start with '-'"),
    ],
)
def test_invalid_targets(commit_sha: str | None, ref: str | None, message: str) -> None:
    with pytest.raises(IngestionInputError, match=message):
        ingestion_target(URL, commit_sha=commit_sha, ref=ref)


def test_target_validates_the_url() -> None:
    with pytest.raises(IngestionInputError, match="host must be"):
        ingestion_target("https://gitlab.com/acme/app", commit_sha=SHA1)


@pytest.mark.parametrize(
    "url",
    ["https://user:s3cret-token@github.com/acme/app", "http://s3cret-token@github.com/acme/app",
     "https://github.com/acme/app?access_token=s3cret-token", "ssh://git:s3cret-token@github.com/a/b",
     "https://github.com/acme/app#s3cret-token", "https://github.com/acme/app/s3cret-token",
     "https://s3cret-token.example.com/acme/app", "https://github.com/acme/app s3cret-token"],
)  # fmt: skip
def test_errors_never_repeat_the_url(url: str) -> None:
    with pytest.raises(IngestionInputError) as raised:
        parse_github_url(url)
    assert "s3cret" not in str(raised.value)

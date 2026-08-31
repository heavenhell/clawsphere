"""Guard against real environment data reaching this public repository.

Written after a live site's management IP, site name and hostname were copied
out of `logs/` into a test fixture and pushed. Credentials were never at risk —
they live in gitignored files — but a routable management address and a
hostname are reconnaissance material on their own.

The rule enforced here is deliberately structural rather than a denylist of the
values that leaked: writing those down would put them back in the repository.
A publicly routable IPv4 literal has no legitimate place in fixtures, mock data
or docs — real deployments are addressed through config, examples belong in the
RFC 5737 documentation ranges, and the internal mock data is RFC 1918. That one
rule catches the class of mistake that actually happened, with nothing to keep
up to date.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("backend", "tests", "docs")
SCANNED_ROOT_FILES = (".env.example", "docker-compose.yml", "README.md", "AGENTS.md")
SCANNED_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".ts", ".tsx", ".js"}
SKIPPED_PARTS = {"__pycache__", ".venv", "node_modules", "dist", ".pytest_cache"}

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _scanned_files() -> list[Path]:
    files = [ROOT / name for name in SCANNED_ROOT_FILES]
    for directory in SCANNED_DIRS:
        files.extend((ROOT / directory).rglob("*"))
    return [
        path for path in files
        if path.is_file()
        and path.suffix in SCANNED_SUFFIXES
        and not SKIPPED_PARTS.intersection(path.parts)
    ]


def _is_documentation_safe(address: ipaddress.IPv4Address) -> bool:
    """Addresses that may legitimately appear as literals in source.

    `is_private` covers RFC 1918 and carries the existing mock fixtures;
    `is_loopback`/`is_unspecified`/`is_link_local` cover local binding; the
    RFC 5737 blocks are the ranges reserved precisely for documentation and
    examples, and are what new fixtures must use.
    """
    if address.is_private or address.is_loopback or address.is_unspecified:
        return True
    if address.is_link_local or address.is_multicast or address.is_reserved:
        return True
    return any(
        address in ipaddress.ip_network(block)
        for block in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
    )


def test_no_publicly_routable_ip_addresses_are_committed():
    offenders: list[str] = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for candidate in IPV4.findall(line):
                try:
                    address = ipaddress.ip_address(candidate)
                except ValueError:
                    # Version strings and the like are not addresses.
                    continue
                if not _is_documentation_safe(address):
                    offenders.append(
                        f"{path.relative_to(ROOT).as_posix()}:{line_number}: {candidate}"
                    )

    assert not offenders, (
        "公网可路由 IP 不得出现在仓库中（这是公开仓库）。\n"
        "示例地址请使用 RFC 5737 文档专用段：192.0.2.0/24 / 198.51.100.0/24 / 203.0.113.0/24。\n"
        "真实环境地址属于配置，放在 gitignore 的 config/platforms.json 或 .env 里。\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "path",
    ["config/platforms.json", "config/platforms.real.json", ".env", "config/edme_ca.pem"],
)
def test_credential_bearing_files_are_ignored_by_git(path: str):
    """The gitignore rules that keep real credentials out are load-bearing.

    They are one line each and easy to drop during a merge, so assert them
    rather than trusting that the file simply never gets added.
    """
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{path} 未被 .gitignore 覆盖，可能被误提交"


def test_runtime_logs_are_not_committed():
    """`logs/` holds session transcripts and rejected answers, which contain
    whatever the live platform returned. It is the source the leaked fixture
    values were copied from, so it must never be tracked."""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "logs/"], cwd=ROOT, capture_output=True, text=True
    )
    assert not result.stdout.strip(), f"logs/ 下有文件被 git 跟踪：\n{result.stdout}"


CREDENTIAL_FILE_PATTERNS = ("*.pem", "*.key", "*.pfx", "*.p12", "*.cer", "*.crt")
CREDENTIAL_FILE_NAMES = ("platforms.json", ".env")


def test_no_credential_bearing_file_is_tracked_anywhere():
    """A catch-all for files nobody thought to add to .gitignore.

    The per-path assertions above only cover the files that exist today. This
    one catches the next private key or platform config that lands under a name
    no rule anticipated — which is how such files usually get committed.
    """
    import fnmatch
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
    ).stdout.splitlines()

    offenders = []
    for path in tracked:
        name = path.rsplit("/", 1)[-1]
        if "example" in name or "sample" in name:
            # Templates carry no secrets and are meant to be committed.
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in CREDENTIAL_FILE_PATTERNS):
            offenders.append(path)
        elif name in CREDENTIAL_FILE_NAMES:
            offenders.append(path)

    assert not offenders, (
        "以下文件可能携带凭据或证书，不应被 git 跟踪：\n" + "\n".join(offenders)
    )

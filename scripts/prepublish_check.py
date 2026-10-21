"""Scan a repository's full git history for personal or company data before it is made public.

    python scripts/prepublish_check.py --allow-author "Name <noreply@example.com>"

Checks every commit's author and committer identity, and every line ever added, with the same
patterns as tests/test_no_personal_data.py (emails, home paths, Atlassian sites, token shapes).
Exits 1 if anything is found. Patterns only: this file holds no names or domains.
The two files that hold deliberate fake examples of those patterns are skipped.
"""

import argparse
import re
import subprocess
import sys

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
OK_DOMAINS = {"example.com", "example.org", "users.noreply.github.com"}
HOME_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)(?!<|\.\.\.|\$|\{|path|you|name)\w")
SITE = re.compile(r"\b([A-Za-z0-9-]+)\.atlassian\.net\b")
SITES_OK = {"acme", "other", "nope", "yoursite", "yourcompany", "example", "site", "your-site"}
TOKEN = re.compile(r"ATATT[A-Za-z0-9_=-]{60,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def ok_domain(domain: str) -> bool:
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d) for d in OK_DOMAINS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-author", action="append", default=[], metavar='"Name <email>"')
    args = parser.parse_args()
    allowed = {a.strip() for a in args.allow_author}
    problems: list[str] = []

    for line in set(git("log", "--all", "--format=%an <%ae>%n%cn <%ce>").splitlines()):
        if line and line not in allowed:
            problems.append(f"identity not allowed: {line}")

    seen: set[str] = set()
    own = [":!tests/test_no_personal_data.py", ":!scripts/prepublish_check.py"]
    history = git("log", "--all", "-p", "--no-color", "--format=", "--", ".", *own)
    for line in history.splitlines():
        if not line.startswith("+") or line.startswith("+++") or line in seen:
            continue
        seen.add(line)
        line = line[1:]
        for m in EMAIL.finditer(line):
            if not ok_domain(m.group(1)):
                problems.append(f"email in history: {m.group(0)}")
        if HOME_PATH.search(line):
            problems.append(f"home path in history: {line[:80]}")
        for m in SITE.finditer(line):
            if m.group(1).lower() not in SITES_OK:
                problems.append(f"Atlassian site in history: {m.group(0)}")
        if TOKEN.search(line):
            problems.append("token or private key shaped string in history")

    for p in sorted(set(problems)):
        print(p)
    print(f"{len(set(problems))} problem(s) found" if problems else "history is clean")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Regenerate ~/WorkNotes/TODO.md from the open GitHub issues assigned to me.

Issues are grouped by their Status on the netFLEX_Development project board
(in the board's column order) and labelled with their Sprint. Checkboxes
already ticked in TODO.md are kept ticked for issues that are still open.

Usage: sync-todo.py [--dry-run] [--todo PATH]

Needs only the gh CLI (logged in with the 'project' scope) and Python 3.6+.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

ORG = "lightriversoftware"
REPO = "netflex"
PROJECT_NUMBER = 16          # netFLEX_Development
NO_STATUS = "No Status"
DEFAULT_TODO = os.path.expanduser("~/WorkNotes/TODO.md")

SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        number title url
        projectItems(first: 20) {
          nodes {
            project { number }
            status: fieldValueByName(name: "Status") {
              ... on ProjectV2ItemFieldSingleSelectValue { name }
            }
            sprint: fieldValueByName(name: "Sprint") {
              ... on ProjectV2ItemFieldIterationValue { title }
            }
          }
        }
      }
    }
  }
}
"""

STATUS_ORDER_QUERY = """
query($org: String!, $num: Int!) {
  organization(login: $org) {
    projectV2(number: $num) {
      field(name: "Status") {
        ... on ProjectV2SingleSelectField { options { name } }
      }
    }
  }
}
"""


def gh_graphql(query, **variables):
    """Run a GraphQL query through `gh api graphql` and return its data."""
    cmd = ["gh", "api", "graphql", "-f", "query=" + query]
    for key, val in variables.items():
        if val is None:
            continue
        # -F types ints for us; strings go through -f so they stay strings.
        flag = "-F" if isinstance(val, int) else "-f"
        cmd += [flag, "{}={}".format(key, val)]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True)
    if out.returncode != 0:
        sys.exit("gh api graphql failed: " + out.stderr.strip())
    return json.loads(out.stdout)["data"]


def my_login():
    out = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True)
    if out.returncode != 0:
        sys.exit("gh api user failed: " + out.stderr.strip())
    return out.stdout.strip()


def fetch_issues(login):
    q = "repo:{}/{} is:issue is:open assignee:{}".format(ORG, REPO, login)
    issues, after = [], None
    while True:
        page = gh_graphql(SEARCH_QUERY, q=q, after=after)["search"]
        for node in page["nodes"]:
            if not node:
                continue
            status, sprint = NO_STATUS, None
            for item in node["projectItems"]["nodes"]:
                if item["project"]["number"] != PROJECT_NUMBER:
                    continue
                if item.get("status"):
                    status = item["status"]["name"]
                if item.get("sprint"):
                    sprint = item["sprint"]["title"]
            issues.append({"number": node["number"], "title": node["title"],
                           "url": node["url"], "status": status,
                           "sprint": sprint})
        if not page["pageInfo"]["hasNextPage"]:
            return issues
        after = page["pageInfo"]["endCursor"]


def status_order():
    data = gh_graphql(STATUS_ORDER_QUERY, org=ORG, num=PROJECT_NUMBER)
    field = data["organization"]["projectV2"]["field"] or {}
    return [o["name"] for o in field.get("options", [])]


def read_existing(path):
    """Return (text, {issue number: checked}) from the current TODO.md."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return "", {}
    boxes = {}
    for m in re.finditer(r"^- \[([ xX])\] \[(\d+)\]", text, re.M):
        boxes[int(m.group(2))] = m.group(1) != " "
    return text, boxes


def render(issues, order, checked):
    today = datetime.date.today().isoformat()
    lines = ["# Things I should be Working On", "",
             "_Updated {}_".format(today), "",
             "## Open netFLEX Issues Assigned to Me", ""]

    by_status = {}
    for issue in issues:
        by_status.setdefault(issue["status"], []).append(issue)
    # Board order first, then any status the board no longer lists,
    # then issues that are not on the board at all.
    extras = sorted(s for s in by_status if s not in order and s != NO_STATUS)
    for status in order + extras + [NO_STATUS]:
        group = sorted(by_status.get(status, []), key=lambda i: i["number"])
        if not group:
            continue
        lines += ["### " + status, ""]
        for i in group:
            box = "x" if checked.get(i["number"]) else " "
            lines.append("- [{}] [{}]({}) ({}) — {}".format(
                box, i["number"], i["url"], i["sprint"] or "No Sprint",
                i["title"]))
        lines.append("")

    lines += ["### Notes", "",
              "Generated by `/sync-issue todo` "
              "(`~/WorkNotes/.claude/skills/sync-issue/sync-todo.py`). "
              "Edits other than ticking boxes are overwritten on the next "
              "sync.", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the new TODO.md instead of writing it")
    ap.add_argument("--todo", default=DEFAULT_TODO, help="path to TODO.md")
    args = ap.parse_args()

    login = my_login()
    issues = fetch_issues(login)
    old_text, checked = read_existing(args.todo)
    new_text = render(issues, status_order(), checked)

    old_nums = set(int(n) for n in re.findall(r"^- \[.\] \[(\d+)\]",
                                              old_text, re.M))
    new_nums = set(i["number"] for i in issues)
    added, removed = sorted(new_nums - old_nums), sorted(old_nums - new_nums)

    if args.dry_run:
        sys.stdout.write(new_text)
    else:
        # Write beside the target and rename, so Obsidian never sees a
        # half-written file.
        d = os.path.dirname(os.path.abspath(args.todo))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".TODO.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        if os.path.exists(args.todo):
            os.chmod(tmp, os.stat(args.todo).st_mode & 0o777)
        os.replace(tmp, args.todo)

    print("{}: {} open issues for {}; added {}; removed {}".format(
        "dry run" if args.dry_run else args.todo, len(issues), login,
        ", ".join(map(str, added)) or "none",
        ", ".join(map(str, removed)) or "none"),
        file=sys.stderr)


if __name__ == "__main__":
    main()

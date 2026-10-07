#!/usr/bin/env python3
"""Build Flow script.

Keeps the state of one run per Jira card, runs the checks itself, makes the
commits, and writes the log the monitor page reads. Every reply ends with the
next step, so the agent never has to remember where it is.

Standard library only. Python 3.8 or newer. Works on Windows, macOS and Linux.

    python flow.py <CARD> <command> [options]
    python flow.py ABC-123 status
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

VERSION = "0.2.0"

CARD_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
PROTECTED = re.compile(r"^(main|master|develop|development|dev|release([/-].*)?|hotfix([/-].*)?)$")
SUBJECT_MAX = 72
HEADLINE_MAX = 160
QUESTION_MAX = 300
CHECK_TIMEOUT_SEC = 300
SUITE_TIMEOUT_MIN = 30
LEARN_KINDS = ("insight", "issue_fix", "command", "correction", "went_well")

SELF_CHECK = [
    ("entry_logged", "Each new entry point logs once on the way in, with the ids needed to trace it."),
    ("branches_logged", "Each branch or early exit that changes the outcome logs its reason."),
    ("state_logged", "Each state change is logged with before and after, or the new value."),
    ("integrations", "Each call to another system logs attempt, success and failure, and has a timeout."),
    ("errors", "Each error is handled or passed on deliberately, and logged once with context."),
    ("no_pii", "No personal data or secrets are written to logs."),
    ("null_checks", "Data from outside (requests, events, other services, the database) is checked for missing values."),
    ("duplicate_safe", "Each Kafka consumer gives the same result when the same message arrives twice."),
    ("config_all_envs", "Each new config key exists in every environment file."),
    ("no_leftovers", "No debug code, TODOs, commented-out code or unrelated changes are left."),
    ("instructions", "The diff follows the instruction files listed below. Re-read them now, then compare."),
]
SELF_CHECK_IDS = [i for i, _ in SELF_CHECK]

REVIEW_AXES = [
    ("requirements", "Requirements"), ("correctness", "Correctness"), ("tests", "Test coverage"),
    ("failure_handling", "Failure handling"), ("security", "Security and data"),
    ("compatibility", "Compatibility and rollout"), ("design", "Design and implementation quality"),
    ("data_access", "Data access"), ("messaging", "Messaging and events"),
]
CROSS_AXIS = ("cross_repo", "Cross-repo agreement")
AXIS_LABEL = dict(REVIEW_AXES + [CROSS_AXIS])
SEVERITIES = ("blocker", "major", "minor", "question")
OUTCOMES = {"fixed": "Fixed", "asked": "Decided by you", "rejected": "Rejected", "left": "Left as it is"}
AGENT_ROLES = ("scout", "verifier", "other")


# ---------------------------------------------------------------- basics

class Refuse(Exception):
    """The command is not allowed right now. Nothing was changed."""

    def __init__(self, why, do=None):
        Exception.__init__(self, why)
        self.why = why
        self.do = do


def data_home():
    env = os.environ.get("BUILD_FLOW_HOME")
    return Path(env) if env else Path.home() / ".agent-data" / "build-flow"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


_PIPE_CLOSED = False


def say(*lines):
    """Print, but never fail a command because the reader stopped listening (for example `| head`)."""
    global _PIPE_CLOSED
    if _PIPE_CLOSED:
        return
    try:
        for line in lines:
            print(line)
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        _PIPE_CLOSED = True
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass


def read_text_any(path):
    """Read a text file whatever PowerShell did to its encoding."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def atomic_write(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(40):  # a reader may hold the file open for a moment on Windows
        try:
            os.replace(str(tmp), str(path))
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(str(tmp), str(path))


def one_line(text):
    return " ".join((text or "").split())


def load_json_file(path, what):
    p = Path(path)
    if not p.is_file():
        raise Refuse("%s file not found: %s" % (what, path), "Write the file first, then run the same command again.")
    try:
        return json.loads(read_text_any(p))
    except ValueError as e:
        raise Refuse("%s file is not valid JSON: %s" % (what, e), "Fix the file and run the same command again.")


# ---------------------------------------------------------------- git

def git(repo, *args, **kw):
    check = kw.get("check", True)
    try:
        r = subprocess.run(["git"] + list(args), cwd=str(repo), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise Refuse("git was not found on the PATH.")
    out = r.stdout.decode("utf-8", errors="replace")
    out = out.rstrip() if kw.get("keep_indent") else out.strip()
    err = r.stderr.decode("utf-8", errors="replace").strip()
    if kw.get("code"):
        return r.returncode
    if check and r.returncode != 0:
        raise Refuse("git %s failed in %s: %s" % (" ".join(args), repo, (err or out)[:500]))
    return out


EXCLUDES = []  # set in main: the flow's own files, in case the workspace is itself a repo


def tree_now(repo):
    """Stage everything and return the id of the resulting tree."""
    git(repo, "add", "-A", "--", ".", *EXCLUDES)
    return git(repo, "write-tree")


def dirty(repo):
    return bool(git(repo, "status", "--porcelain", "--", ".", *EXCLUDES))


def head_tree(repo):
    return git(repo, "rev-parse", "HEAD^{tree}")


def head(repo):
    return git(repo, "rev-parse", "--short", "HEAD")


def staged_files(repo):
    out = git(repo, "diff", "--cached", "--name-status", "HEAD")
    return [line.split("\t") for line in out.splitlines() if line.strip()]


def remote_web(repo):
    url = git(repo, "remote", "get-url", "origin", check=False)
    low = url.lower()
    host = "github" if "github" in low else ("bitbucket" if url else "none")
    m = re.search(r"(github\.com|bitbucket\.org)[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    web = "https://%s/%s/%s" % (m.group(1), m.group(2), m.group(3)) if m else ""
    return host, web


def commit_url(repo, h):
    host, web = remote_web(repo)
    if not web:
        return ""
    return web + ("/commit/" if host == "github" else "/commits/") + h


# ---------------------------------------------------------------- running checks

SUMMARY_RE = re.compile(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)")


def parse_tests(text):
    """Return (found, run, failed, skipped) from Maven surefire or failsafe output."""
    totals, per_class = [], []
    for line in text.splitlines():
        m = SUMMARY_RE.search(line)
        if not m:
            continue
        nums = [int(x) for x in m.groups()]
        (per_class if "Time elapsed" in line else totals).append(nums)
    rows = totals or per_class
    if not rows:
        return False, 0, 0, 0
    run = sum(r[0] for r in rows)
    failed = sum(r[1] + r[2] for r in rows)
    skipped = sum(r[3] for r in rows)
    return True, run, failed, skipped


def failure_lines(text, limit=12):
    lines = text.splitlines()
    picked = []
    for i, line in enumerate(lines):
        if re.match(r"\[ERROR\]\s+(Failures|Errors):\s*$", line):
            j = i + 1
            while j < len(lines) and re.match(r"\[ERROR\]\s{2,}\S", lines[j]):
                picked.append(lines[j][7:].strip())
                j += 1
    for line in lines:
        if "COMPILATION ERROR" in line or re.search(r"\.java:\[\d+,\d+\]", line):
            picked.append(re.sub(r"^\[ERROR\]\s*", "", line).strip())
    if not picked:
        picked = [re.sub(r"^\[ERROR\]\s*", "", l).strip() for l in lines if l.startswith("[ERROR]") and len(l) > 8]
    if not picked:
        picked = [l for l in lines if l.strip()][-limit:]
    seen, out = set(), []
    for p in picked:
        if p and p not in seen:
            seen.add(p)
            out.append(p[:300])
    return out[:limit]


def expects_tests(cmd):
    low = cmd.lower()
    if "mvn" not in low or "skiptests" in low or "maven.test.skip" in low:
        return False
    return bool(re.search(r"(^|\s)(test|verify|integration-test|install)(\s|$)", cmd)) or "-Dtest=" in cmd


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def run_command(cmd, cwd, timeout_sec, outfile):
    """Run a shell command with all output sent to a file. Returns (exit code, timed out, seconds)."""
    start = time.time()
    with open(str(outfile), "wb") as f:
        kwargs = dict(cwd=str(cwd), stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, shell=True)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)
        last_beat = start
        while True:
            try:
                code = proc.wait(timeout=1)
                return code, False, time.time() - start
            except subprocess.TimeoutExpired:
                pass
            now = time.time()
            if now - start > timeout_sec:
                kill_tree(proc)
                return None, True, now - start
            if now - last_beat >= 60:
                last_beat = now
                say("  ... still running, %d min" % int((now - start) // 60))


SLOW_HELP = [
    "  Find out why before trying again. Check in this order and change one thing at a time:",
    "   1. Scope: does the command run more tests than this step needs? Narrow -Dtest or -pl.",
    "   2. Build overhead: is Maven rebuilding modules this step did not touch?",
    "   3. Test type: is it a Spring context, component or contract test? Those belong in the closing slice.",
    "   4. The test itself: is it waiting on a sleep, a port or a container?",
    "  When you have a faster command, put it in the plan with plan-revise and record it with:",
    "   learn --kind command --text '<the command and why it is faster>'",
]


# ---------------------------------------------------------------- the run

class Run:
    def __init__(self, card):
        self.card = card
        self.dir = data_home() / "runs" / card
        self.state = None
        self.touched = False

    def exists(self):
        return (self.dir / "state.json").is_file()

    def load(self):
        self.state = json.loads(read_text_any(self.dir / "state.json"))

    def save(self):
        if self.state is None:
            return
        atomic_write(self.dir / "state.json", json.dumps(self.state, indent=1, ensure_ascii=False))
        try:
            render_plan(self)
        except Exception as e:  # the plan file is a view; never block work on it
            say("  (could not rewrite the plan file: %s)" % e)

    def log(self, type_, **fields):
        ev = {"t": now_iso(), "run": self.card, "type": type_}
        ev.update(fields)
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(str(self.dir / "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.touched = True

    def write_run_data(self):
        """Write the log as a script file the monitor page can load without a server."""
        if not self.touched:
            return
        try:
            events = []
            for line in read_text_any(self.dir / "log.jsonl").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
            body = json.dumps({"card": self.card, "events": events}, ensure_ascii=False)
            atomic_write(self.dir / "run-data.js", "__runData(" + body.replace("</", "<\\/") + ");\n")
        except Exception:
            pass

    # -- shortcuts
    @property
    def plan(self):
        return self.state.get("plan")

    def flow(self, rest):
        return "flow %s %s" % (self.card, rest)

    def workspace(self):
        return Path(self.state["workspace"])

    def plan_path(self):
        return self.workspace() / (self.card + ".plan.md")

    def work_dir(self):
        """Where the agent writes its plan JSON and report files: in the workspace, outside any commit."""
        return self.workspace() / ".build-flow"

    def repo_plan(self, name):
        for rp in self.plan["repos"]:
            if rp["repo"] == name:
                return rp
        raise Refuse("The plan has no repo named %s." % name,
                     "Repos in the plan: " + ", ".join(r["repo"] for r in self.plan["repos"]))

    def repo_path(self, name):
        rp = self.repo_plan(name)
        p = self.workspace() / rp.get("path", rp["repo"])
        if not (p / ".git").exists():
            raise Refuse("%s is not a git repository." % p, "Check the repo's path in the plan.")
        return p

    def limited(self, key, text, maxlen):
        """Enforce a length limit, but never block more than twice in a row."""
        text = one_line(text)
        rejects = self.state.setdefault("rejects", {})
        if len(text) <= maxlen:
            rejects[key] = 0
            return text
        rejects[key] = rejects.get(key, 0) + 1
        if rejects[key] <= 2:
            atomic_write(self.dir / "state.json", json.dumps(self.state, indent=1, ensure_ascii=False))
            raise Refuse("The %s is %d characters. The limit is %d." % (key, len(text), maxlen),
                         "Shorten it and run the same command again.")
        rejects[key] = 0
        return text[:maxlen - 3].rstrip() + "..."


def new_unit():
    return {"state": "todo", "commit": None, "fails": 0, "results": {}, "tree": None}


def new_repo_state(rp):
    return {"stage": "todo", "start_commit": None, "slices": [new_unit() for _ in rp["slices"]],
            "closing": new_unit() if rp.get("closing") else None,
            "selfcheck": {"started": False, "start_commit": None, "done": False},
            "review": new_review(),
            "verify": {"ok": False, "tree": None}, "suite": {"ok": False, "tree": None, "passed": 0},
            "fix_commits": [], "commits": [], "pushed_commit": None, "pr": None, "handoff": None}


def new_review(since=None):
    return {"started": False, "planned": False, "done": False, "start_commit": None, "since": since,
            "fixes_before": 0, "axes": []}


def review_of(rs):
    return rs.setdefault("review", new_review())


def needs_review(plan):
    """Trivial work skips the plan review and the code review, unless its risk is high."""
    return plan.get("work") != "trivial" or plan.get("risk") == "high"


def stage_after_tests(run, rs):
    return "code_review" if needs_review(run.plan) and not review_of(rs)["done"] else "suite"


def plan_review_due(s):
    return bool(s.get("plan")) and not s.get("approved") and needs_review(s["plan"]) and not s.get("plan_review")


# ---------------------------------------------------------------- plan

def unit_key(spec, done=False):
    """What makes a slice the same slice. A done slice may still change which criteria it is said to prove."""
    keys = ("title", "done_when", "checks") if done else ("title", "done_when", "checks", "proves")
    return json.dumps({k: spec.get(k) for k in keys}, sort_keys=True)


def validate_plan(plan, card):
    errors, warnings = [], []

    def need(obj, key, where, kind=str):
        v = obj.get(key) if isinstance(obj, dict) else None
        if not isinstance(v, kind) or (kind in (str, list) and not v):
            errors.append("%s: '%s' is missing or empty." % (where, key))
            return None
        return v

    if not isinstance(plan, dict):
        return ["The plan must be a JSON object."], warnings
    if plan.get("card") and plan["card"].upper() != card:
        errors.append("The plan is for %s but this run is %s." % (plan["card"], card))
    for key in ("title", "what", "why", "branch"):
        need(plan, key, "plan")
    if plan.get("work") not in ("trivial", "standard"):
        errors.append("plan: 'work' must be \"trivial\" or \"standard\".")
    if plan.get("risk") not in ("low", "standard", "high"):
        errors.append("plan: 'risk' must be \"low\", \"standard\" or \"high\".")
    need(plan, "risk_reason", "plan")
    branch = plan.get("branch") or ""
    if branch and PROTECTED.match(branch):
        errors.append("plan: branch '%s' is a base branch. Use a feature branch." % branch)
    if branch and card not in branch.upper():
        warnings.append("The branch name does not contain the card key %s." % card)

    ids = []
    for i, c in enumerate(need(plan, "criteria", "plan", list) or []):
        w = "criteria[%d]" % (i + 1)
        cid = need(c, "id", w)
        need(c, "text", w)
        t = c.get("test") if isinstance(c, dict) else None
        if not isinstance(t, dict) or not t.get("layer") or not t.get("scenario"):
            errors.append("%s: 'test' needs a 'layer' and a 'scenario'." % w)
        if cid:
            if cid in ids:
                errors.append("%s: id %s is used twice." % (w, cid))
            ids.append(cid)

    proven = set()
    names = []
    for ri, rp in enumerate(need(plan, "repos", "plan", list) or []):
        w = "repos[%d]" % (ri + 1)
        name = need(rp, "repo", w)
        need(rp, "base", w)
        if name:
            if name in names:
                errors.append("%s: repo %s is listed twice." % (w, name))
            names.append(name)
            w = name
        if isinstance(rp, dict) and rp.get("base") and rp.get("base") == branch:
            errors.append("%s: the base branch and the feature branch are the same." % w)
        units = [("slice %d" % (i + 1), s) for i, s in enumerate(need(rp, "slices", w, list) or [])]
        if isinstance(rp, dict) and rp.get("closing") is not None:
            units.append(("closing slice", rp["closing"]))
        for label, s in units:
            uw = "%s %s" % (w, label)
            title = need(s, "title", uw)
            need(s, "done_when", uw)
            checks = need(s, "checks", uw, list) or []
            for ci, chk in enumerate(checks):
                if not isinstance(chk, dict) or not isinstance(chk.get("cmd"), str) or not chk["cmd"].strip():
                    errors.append("%s check %d: needs a 'cmd' to run." % (uw, ci + 1))
                elif re.search(r"(^|\s)(-q|--quiet)(\s|$)", chk["cmd"]) and "mvn" in chk["cmd"]:
                    errors.append("%s check %d: remove -q. It hides the test counts, and the script trims the output anyway." % (uw, ci + 1))
            for cid in (s.get("proves") or []) if isinstance(s, dict) else []:
                if cid not in ids:
                    errors.append("%s: proves %s, which is not a criterion." % (uw, cid))
                proven.add(cid)
            if title and len(title) > 80:
                warnings.append("%s: the title is over 80 characters and will be cut on the page." % uw)
        if isinstance(rp, dict) and plan.get("work") == "standard" and rp.get("closing") is None:
            warnings.append("%s has no closing slice. Standard work normally ends each repo with one." % w)
    for cid in ids:
        if cid not in proven:
            errors.append("Coverage: no slice proves %s. Add it to a slice's 'proves', or drop the criterion." % cid)
    for i, t in enumerate(plan.get("tests_expected_to_change") or []):
        if not isinstance(t, dict) or not t.get("test") or not t.get("change"):
            errors.append("tests_expected_to_change[%d]: needs 'test' and 'change'." % (i + 1))
        elif t.get("repo") and t["repo"] not in names:
            errors.append("tests_expected_to_change[%d]: repo %s is not in the plan." % (i + 1, t["repo"]))
        elif not t.get("repo") and len(names) > 1:
            warnings.append("tests_expected_to_change[%d] names no 'repo', so it is allowed, and listed in the PR, "
                            "for every repo." % (i + 1))
    mo = plan.get("merge_order")
    if mo is None and len(names) > 1:
        warnings.append("No merge_order, so the PRs merge in build order: %s. If a consumer must be live before its "
                        "producer, set merge_order." % ", then ".join(names))
    if mo is not None and (not isinstance(mo, list) or sorted(map(str, mo)) != sorted(names)):
        errors.append("merge_order: must list every repo in the plan exactly once, in the order the PRs merge.")
    for i, d in enumerate(plan.get("decisions") or []):
        if not isinstance(d, dict) or not d.get("text") or d.get("by") not in ("user", "default"):
            errors.append("decisions[%d]: needs 'text' and 'by' (\"user\" or \"default\")." % (i + 1))
        elif d.get("repo") and d["repo"] not in names:
            errors.append("decisions[%d]: repo %s is not in the plan." % (i + 1, d["repo"]))
    return errors, warnings


def merge_names(plan):
    """Repo names in the order their PRs merge: the plan's merge_order, or the build order."""
    return list(plan.get("merge_order") or [rp["repo"] for rp in plan["repos"]])


def tests_for(plan, name):
    """Expected test changes that apply to one repo."""
    return [t for t in plan.get("tests_expected_to_change") or [] if t.get("repo") in (None, "", name)]


def approval_view(plan):
    """The parts of a plan that cannot change without the user's approval."""
    return json.dumps({
        "what": plan.get("what"), "why": plan.get("why"), "not_included": plan.get("not_included"),
        "criteria": plan.get("criteria"), "contract": plan.get("contract"), "branch": plan.get("branch"),
        "decisions": plan.get("decisions"), "tests": plan.get("tests_expected_to_change"),
        "risk": plan.get("risk"), "rollout": plan.get("rollout"), "merge_order": plan.get("merge_order"),
        "repos": [[r.get("repo"), r.get("base"), r.get("path"), r.get("full_suite")] for r in plan.get("repos", [])],
    }, sort_keys=True)


ICON = {"todo": "⬜", "in_progress": "\U0001f504", "done": "✅", "failed": "❌", "wait": "⚠️"}


def status_line(run):
    s = run.state
    if s["phase"] == "done":
        return ICON["done"] + " Finished. PRs are open for review."
    qs = open_questions(s)
    if qs:
        return ICON["wait"] + " Waiting on you: " + qs[0]["question"] + (" (and %d more)" % (len(qs) - 1) if len(qs) > 1 else "")
    if not s.get("plan"):
        return ICON["in_progress"] + " Planning"
    if plan_review_due(s):
        return ICON["in_progress"] + " Draft plan, being reviewed before it comes to you"
    if not s.get("approved"):
        return ICON["wait"] + " Draft plan, waiting for your approval"
    for rp in s["plan"]["repos"]:
        rs = s["repos"][rp["repo"]]
        if rs["stage"] == "built":
            continue
        done = sum(1 for u in rs["slices"] if u["state"] == "done")
        label = {"todo": "not started", "build": "building, %d of %d slices verified" % (done, len(rs["slices"])),
                 "self_check": "self-check", "closing": "closing tests", "code_review": "review",
                 "suite": "full suite"}[rs["stage"]]
        return ICON["in_progress"] + " %s: %s" % (rp["repo"], label)
    return ICON["in_progress"] + " Opening pull requests"


def render_plan(run):
    s = run.state
    plan = s.get("plan")
    if not plan:
        return
    L = []
    add = lambda line: L.append(line.replace("<", "\\<") if not line.startswith("<!--") else line)
    add("# %s %s" % (run.card, plan["title"]))
    add("")
    add("**Status:** " + status_line(run))
    add("")
    appr = s.get("approved")
    add("Plan version %d. %s" % (s.get("plan_rev", 1), ("Approved %s." % appr["t"][:16].replace("T", " ")) if appr
                                 else "Not approved yet."))
    add("")
    add("<!-- This file is written by the flow script. Change the plan through plan-submit or plan-revise, not here. -->")
    add("")
    add("## Summary")
    add("")
    add("- **What changes:** " + plan["what"])
    add("- **Why:** " + plan["why"])
    for item in plan.get("not_included") or []:
        add("- **Not included:** " + item)
    add("- **Risk:** %s. %s" % (plan["risk"], plan.get("risk_reason", "")))
    add("- **Work:** " + plan["work"])
    add("- **Branch:** `%s`" % plan["branch"])
    add("")
    add("## Acceptance criteria")
    add("")
    for c in plan["criteria"]:
        add("- **%s** %s  " % (c["id"], c["text"]))
        add("  Test: %s, %s" % (c["test"]["layer"], c["test"]["scenario"]))
    add("")
    if plan.get("decisions"):
        add("## Decisions")
        add("")
        for i, d in enumerate(plan["decisions"]):
            add("%d. %s (%s)" % (i + 1, d["text"], "you decided" if d["by"] == "user" else "default you can veto"))
        add("")
    if plan.get("assumptions"):
        add("## Assumptions")
        add("")
        add("Taken as given. Say so in chat if one is wrong.")
        add("")
        for a in plan["assumptions"]:
            add("- " + a)
        add("")
    if plan.get("contract"):
        add("## Contract")
        add("")
        con = plan["contract"]
        if isinstance(con, dict):
            if con.get("summary"):
                add(con["summary"])
                add("")
            for it in con.get("items") or []:
                add("- `%s` (%s): %s" % (it.get("name", ""), it.get("kind", ""), it.get("change", "")))
        else:
            add(str(con))
        add("")
    add("## Repos")
    add("")
    add("| Repo | Base branch | Other work in flight |")
    add("| --- | --- | --- |")
    for rp in plan["repos"]:
        add("| %s | `%s` | %s |" % (rp["repo"], rp["base"], rp.get("in_flight") or "None found"))
    add("")
    add("## Slices")
    for rp in plan["repos"]:
        rs = s["repos"].get(rp["repo"]) or new_repo_state(rp)
        add("")
        add("### " + rp["repo"])
        add("")

        def unit_lines(label, spec, u):
            add("- %s **%s%s**%s%s" % (ICON[u["state"]], label, spec["title"],
                                       (" `%s`" % u["commit"]) if u.get("commit") else "",
                                       (" (proves %s)" % ", ".join(spec["proves"])) if spec.get("proves") else ""))
            add("  - Done when: " + spec["done_when"])
            if spec.get("approach"):
                add("  - Approach: " + spec["approach"])
            if spec.get("tests_to_add"):
                add("  - Tests to add: " + ", ".join(spec["tests_to_add"]))
            if spec.get("depends_on"):
                add("  - Depends on: " + spec["depends_on"])
            for chk in spec["checks"]:
                add("  - Check: `%s`" % chk["cmd"])

        for i, spec in enumerate(rp["slices"]):
            unit_lines("%d. " % (i + 1), spec, rs["slices"][i] if i < len(rs["slices"]) else new_unit())
        sc = rs["selfcheck"]
        add("- %s **Self-check**" % ICON["done" if sc["done"] else "in_progress" if sc["started"] else "todo"])
        if rp.get("closing"):
            unit_lines("Closing: ", rp["closing"], rs["closing"] or new_unit())
        if needs_review(plan):
            rv = review_of(rs)
            add("- %s **Review** by reviewer subagents, one per axis the change needs" % ICON[
                "done" if rv["done"] else "in_progress" if rv["started"] else "todo"])
        add("- %s **Full suite:** `%s`" % (ICON["done" if rs["stage"] == "built" else "todo"],
                                           rp.get("full_suite") or "mvn verify"))
    add("")
    if plan.get("tests_expected_to_change"):
        add("## Existing tests expected to change")
        add("")
        for t in plan["tests_expected_to_change"]:
            add("- `%s`%s: %s" % (t["test"], (" in %s" % t["repo"]) if t.get("repo") else "", t["change"]))
        add("")
        add("A failing test that is not on this list is treated as a regression.")
        add("")
    if plan.get("rollout") or len(plan["repos"]) > 1:
        add("## Rollout")
        add("")
        if len(plan["repos"]) > 1:
            add("PRs merge in this order: " + ", then ".join(merge_names(plan)) + ".")
            add("")
        for i, r in enumerate(plan.get("rollout") or []):
            add("%d. %s" % (i + 1, r))
        add("")
    add("## Coverage")
    add("")
    add("| Criterion | Proven by | Test |")
    add("| --- | --- | --- |")
    for c in plan["criteria"]:
        where = []
        for rp in plan["repos"]:
            for i, spec in enumerate(rp["slices"]):
                if c["id"] in (spec.get("proves") or []):
                    where.append("%s slice %d" % (rp["repo"], i + 1))
            if rp.get("closing") and c["id"] in (rp["closing"].get("proves") or []):
                where.append("%s closing slice" % rp["repo"])
        add("| %s | %s | %s: %s |" % (c["id"], "; ".join(where), c["test"]["layer"], c["test"]["scenario"]))
    add("")
    if s.get("changes"):
        add("## Changes since approval")
        add("")
        for ch in s["changes"]:
            add("- %s: %s (%s)" % (ch["t"][:16].replace("T", " "), ch["text"], ch["approval"]))
        add("")
    atomic_write(run.plan_path(), "\n".join(L))


# ---------------------------------------------------------------- where are we

def cur_unit(rs):
    """The slice in progress as (unit, slice number or None for the closing slice)."""
    for i, u in enumerate(rs["slices"]):
        if u["state"] == "in_progress":
            return u, i + 1
    if rs.get("closing") and rs["closing"]["state"] == "in_progress":
        return rs["closing"], None
    return None, None


def unit_spec(rp, n):
    return rp["closing"] if n is None else rp["slices"][n - 1]


def unit_label(n):
    return "the closing slice" if n is None else "slice %d" % n


def unit_checked(u, spec):
    cmds = [c["cmd"] for c in spec["checks"]]
    return bool(u.get("tree")) and all(u["results"].get(c, {}).get("ok") for c in cmds)


def active_repo(run, wanted=None):
    """The repo a command applies to: the one named, or the first that is not built yet."""
    if not run.plan or not run.state.get("approved"):
        raise Refuse("The plan is not approved yet.", next_step(run)[0])
    names = [rp["repo"] for rp in run.plan["repos"]]
    if wanted:
        if wanted not in names:
            raise Refuse("The plan has no repo named %s." % wanted, "Repos in the plan: " + ", ".join(names))
        return wanted
    for name in names:
        if run.state["repos"][name]["stage"] != "built":
            return name
    if len(names) == 1:
        return names[0]
    raise Refuse("Every repo is built, so say which one you mean.", "Add --repo <name>. Repos: " + ", ".join(names))


def named_repo(run, name):
    """State of a repo named on the command line, once the plan is approved."""
    if not run.plan or not run.state.get("approved"):
        raise Refuse("The plan is not approved yet.", next_step(run)[0])
    run.repo_plan(name)
    return run.state["repos"][name]


def on_feature_branch(run, repo):
    branch = run.plan["branch"]
    cur = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if cur != branch or PROTECTED.match(cur):
        raise Refuse("%s is on branch '%s', not the feature branch '%s'." % (repo.name, cur, branch),
                     "Switch back with: git -C \"%s\" checkout %s" % (repo, branch))


def next_step(run):
    s = run.state
    F = lambda rest: run.flow(rest)
    if s["phase"] == "done":
        return ["Nothing. The run is finished. PR review is the user's.",
                "If the user asks for a change to this card: " + F("change-request --text '<what they asked>'")]
    qs = open_questions(s)
    if qs:
        return ["%d question%s open. If another real decision is open, record it with ask too." % (
                    len(qs), " is" if len(qs) == 1 else "s are"),
                "Then put every open question to the user in one chat message, each with what you found, the "
                "options and your recommendation. Wait for the reply.",
                "When they answer: " + F("answered --user-said '<their words>'")] + (
                    ["The plan review is not recorded yet. Record it before you show the plan: " +
                     F("plan-reviewed --file <path>")] if plan_review_due(s) else [])
    ask_form = F("ask --question '...' --recommend '...' --found '...' --options 'A: ... | B: ...'")
    cr = open_change(s)
    if cr:
        busy = next((n for n, rs in s["repos"].items() if cur_unit(rs)[0]), None)
        small = ("make it as part of the slice in progress in %s, then %s and slice-done." % (busy, F("check")) if busy else
                 "make it, then %s and %s." % (F("verify --repo <name>"), F("commit --repo <name> --subject '...' --why '...'")))
        return ["Change request %d is not settled yet: %s" % (cr["id"], cr["text"]),
                "Size it before anything else. Inside the approved plan (a rename, a log line, a small fix): " + small,
                "A plan change (criteria, contract, scope, decisions, new behavior): tell the user exactly what changes, "
                "wait for their yes, then " + F("plan-revise --file <path> --summary '...' --user-said '<their words>'"),
                "If the user drops it: " + F("change-request --withdrawn '<their words>'")]
    if not s.get("plan"):
        lead = ("If another real decision is open, record it with: " if s.get("asked")
                else "Read the card and the code (SKILL.md, Planning, says when to use flow-scout subagents). "
                     "Record each real open decision with: ")
        out = [lead + ask_form,
               "When none is open, write the plan as a JSON file (reference/plan-format.md) and run: " +
               F("plan-submit --file <path>")]
        waiting = [g for g in s.get("agents") or [] if not g.get("done")]
        if waiting:
            out.insert(0, "%d subagent%s out (%s). When each returns, record what it found: %s" % (
                len(waiting), " is" if len(waiting) == 1 else "s are", ", ".join(str(g["n"]) for g in waiting),
                F("agent-done <number> --takeaway '<one line>'")))
        return out
    if plan_review_due(s):
        depth = "light" if s["plan"].get("risk") == "low" else "full"
        return ["Do not show the plan to the user yet. It gets one review by a flow-plan-reviewer subagent at depth '%s'. "
                "If you have not launched it, launch it now (what to give it: reference/review.md)." % depth,
                "When it has returned: fix what stands (edit the JSON, run plan-submit again), then record the "
                "review once with: " + F("plan-reviewed --file <path>")]
    if not s.get("approved"):
        return ["Show the user the plan and wait for approval in chat: " + str(run.plan_path()),
                "When they approve: " + F("plan-approve --user-said '<their words>'"),
                "If they want any change, even with an approval attached: edit the JSON, run plan-submit again, "
                "and ask again."]
    plan = s["plan"]
    names = [rp["repo"] for rp in plan["repos"]]
    for i, rp in enumerate(plan["repos"]):
        name = rp["repo"]
        rs = s["repos"][name]
        st = rs["stage"]
        if st == "built":
            continue
        if st == "todo":
            return [F("repo-start " + name)]
        if st in ("build", "closing"):
            u, n = cur_unit(rs)
            if not u:
                return [F("slice-start")]
            if unit_checked(u, unit_spec(rp, n)):
                return [F("slice-done --subject '<what this commit does>' --why '<why it is needed>'") +
                        "   (format: reference/commit-and-pr.md)"]
            return ["Build %s, then: %s" % (unit_label(n), F("check"))]
        if st == "self_check":
            if not rs["selfcheck"]["started"]:
                return [F("selfcheck-start")]
            fixes = len(rs["fix_commits"]) - rs["selfcheck"].get("fixes_before", 0)
            return ["Walk the diff against the self-check list (%d fix%s committed so far). For each fix: %s then %s" % (
                        fixes, "" if fixes == 1 else "es", F("verify"), F("commit --subject '...' --why '...'")),
                    "Then write the report file and run: " + F("selfcheck-report --file <path>")]
        if st == "code_review":
            rv = review_of(rs)
            if not rv["started"]:
                return [F("review-start")]
            if not rv["planned"]:
                return ["Triage the review axes as the pr-review skill says (\"When build-flow calls you\"), write "
                        "them to a file and run: " + F("review-plan --file <path>") + "   (format: reference/review.md)"]
            fixes = len(rs["fix_commits"]) - rv.get("fixes_before", 0)
            return ["The reviewers are recorded. If you have not launched them, launch one pr-reviewer subagent per axis "
                    "that runs, all at once.",
                    "When they have returned: check what they say against the code, and give every finding an outcome "
                    "(reference/review.md). To fix one (%d fix%s committed so far): edit, %s, %s." % (
                        fixes, "" if fixes == 1 else "es", F("verify"), F("commit --subject '...' --why '...'")),
                    "Then write the report file and run: " + F("review-report --file <path>")]
        if st == "suite":
            if rs["suite"]["ok"]:
                try:
                    uncommitted = head_tree(run.repo_path(name)) != rs["suite"]["tree"]
                except Refuse:
                    uncommitted = False
                if uncommitted:
                    return [F("commit --subject '<the fix>' --why '<why>'") +
                            "   (the suite passed, but those changes are not committed)"]
                more = i < len(names) - 1
                return [F("repo-done" + (" --handoff '<what the next repo needs to know about what was built>'"
                                         if more else ""))]
            return [F("suite")]
    # every repo is built: push all, open all PRs, then see each through CI
    for name in names:
        rs = s["repos"][name]
        try:
            h = head(run.repo_path(name))
        except Refuse:
            h = rs["pushed_commit"]
        if rs["pushed_commit"] != h:
            return [F("push " + name)]
    for name in names:
        rs = s["repos"][name]
        if not rs["pr"]:
            if rs.get("pr_body"):
                return ["Open the draft PR for %s with the title and description file above. Then: %s" % (
                    name, F("pr-opened %s --url <url>" % name))]
            return [F("pr-body " + name)]
    for name in names:
        rs = s["repos"][name]
        if rs["pr"].get("stale"):
            return ["The PR for %s is open and its description is out of date: %s" % (name, F("pr-body " + name))]
    for name in names:
        rs = s["repos"][name]
        if not rs["pr"].get("ready"):
            mark = ("The PR is already open for review, so leave it as it is. When every check has finished green, run: "
                    if rs["pr"].get("was_ready") else
                    "When every check has finished green, mark the PR ready for review if it is still a draft, and run: ")
            return ["Check CI on %s. If it is still running, check again in a minute. To fix a failure: edit, %s, %s, %s." % (
                        rs["pr"]["url"], F("verify --repo " + name), F("commit --repo %s --subject ..." % name),
                        F("push " + name)),
                    mark + F("pr-ready %s --ci-result '<what ran and passed>'" % name)]
    return [F("finish")]


# ---------------------------------------------------------------- commands: planning

def cmd_start(run, a):
    if run.exists():
        raise Refuse("A run already exists for %s." % run.card,
                     "Continue it. See where it is with: " + run.flow("status"))
    ws = Path(a.workspace or os.getcwd()).resolve()
    if not ws.is_dir():
        raise Refuse("The workspace folder does not exist: %s" % ws)
    run.dir.mkdir(parents=True, exist_ok=True)
    (run.dir / "out").mkdir(exist_ok=True)
    (ws / ".build-flow").mkdir(exist_ok=True)
    title = one_line(a.title)
    run.state = {"version": 1, "card": run.card, "title": title, "url": a.url or "", "workspace": str(ws),
                 "created": now_iso(), "phase": "active", "plan": None, "plan_rev": 0, "approved": None,
                 "questions": [], "repos": {}, "rejects": {}, "changes": [], "asked": 0, "change_requests": [], "noticed": []}
    run.log("run_start", card=run.card, title=title, url=a.url or "", workspace=str(ws), script=VERSION)
    run.log("stage", name="intake", state="start")
    say("OK run started for %s: %s" % (run.card, title),
        "  Workspace:     %s" % ws,
        "  Working files: %s   (write the plan JSON and report files here)" % (ws / ".build-flow"),
        "  Log and test output: %s" % run.dir)
    page = Path(__file__).resolve().parent / "monitor.html"
    if page.is_file():
        (run.dir / "monitor.html").write_bytes(page.read_bytes())
        say("  Monitor page:  %s   (open it with: flow %s monitor)" % (run.dir / "monitor.html", run.card))
    found = sorted(ws.glob("*/.github/copilot-instructions.md")) + sorted(ws.glob(".github/copilot-instructions.md"))
    if found:
        say("  Instruction files in this workspace. Read the ones for the repos you will change before you plan:")
        for f in found:
            say("    " + str(f))
    if (ws / ".git").exists():
        say("  Note: this folder is itself a git repo. That is fine for a single-repo project: give the repo",
            "  \"path\": \".\" in the plan. Otherwise start again from the folder that contains the repos.")


def cmd_status(run, a):
    s = run.state
    say("%s %s" % (run.card, s["title"]), "  " + status_line(run).split(" ", 1)[1])
    if s.get("plan"):
        say("  Plan: %s (version %d, %s)" % (run.plan_path(), s["plan_rev"],
                                           "approved" if s.get("approved") else "not approved"))
        for rp in s["plan"]["repos"]:
            rs = s["repos"][rp["repo"]]
            done = sum(1 for u in rs["slices"] if u["state"] == "done")
            bits = ["%d of %d slices done" % (done, len(rs["slices"])), "stage: " + rs["stage"]]
            if rs["stage"] != "todo":
                bits.append(repo_summary(run, rp["repo"]))
            if rs["pr"]:
                bits.append("PR " + rs["pr"]["url"])
            say("  %s: %s" % (rp["repo"], "; ".join(bits)))
    for q in open_questions(s):
        say("  Open question %d: %s" % (q["n"], q["question"]))
    for g in s.get("agents") or []:
        if not g.get("done"):
            say("  Subagent %d (%s) has no takeaway yet: %s" % (g["n"], g["role"], g["task"]))
    for c in s.get("change_requests") or []:
        say("  Change request %d (%s): %s" % (c["id"], "open" if c.get("open") else "closed", c["text"]))
    for i, n in enumerate(s.get("noticed") or []):
        say("  Noticed outside the card %d: %s" % (i + 1, n))
    say("  Monitor page: %s" % (run.dir / "monitor.html"))


def cmd_now(run, a):
    run.log("now", text=run.limited("note", a.text, HEADLINE_MAX))
    say("OK shown on the monitor page.")


def cmd_ask(run, a):
    s = run.state
    q = run.limited("question", a.question, QUESTION_MAX)
    rec = run.limited("recommendation", a.recommend, QUESTION_MAX)
    if not s.get("plan") and not s.get("asked"):
        run.log("stage", name="intake", state="done")
        run.log("stage", name="interview", state="start")
    s["asked"] = s.get("asked", 0) + 1
    entry = {"n": s["asked"], "question": q, "recommend": rec, "t": now_iso()}
    s.setdefault("questions", []).append(entry)
    run.log("waiting", on="decision" if s.get("approved") else "interview", q=entry["n"], text=q, recommend=rec,
            found=one_line(a.found or ""), options=one_line(a.options or ""))
    say("OK question %d recorded." % entry["n"])


def cmd_answered(run, a):
    s = run.state
    qs = open_questions(s)
    if not qs:
        raise Refuse("No question is open.")
    said = one_line(a.user_said)
    if not said:
        raise Refuse("--user-said is empty.", "Quote what the user answered.")
    closing = [q for q in qs if a.question in (None, q["n"])]
    if not closing:
        raise Refuse("Question %s is not open." % a.question, "Open: " + ", ".join(str(q["n"]) for q in qs))
    run.log("resumed", text=said, closes=[q["n"] for q in closing], questions=[q["question"] for q in closing])
    s["questions"] = [q for q in qs if q not in closing]
    if not s.get("plan") and not s["questions"]:
        run.log("stage", name="interview", state="done")
        run.log("stage", name="plan", state="start")
        s["interview_closed"] = True
    say("OK answer recorded for question%s %s." % ("" if len(closing) == 1 else "s", ", ".join(str(q["n"]) for q in closing)))
    if s.get("approved"):
        say("  If the answer changes the criteria, contract, scope or decisions, revise the plan before going on:",
            "  " + run.flow("plan-revise --file <path> --summary '<what changed>' --user-said '<their words>'"))
    else:
        say("  Put each decision in the plan's 'decisions' with by: \"user\". If the plan is already submitted and",
            "  already says what they answered, it does not need to change.")


def cmd_plan_submit(run, a):
    s = run.state
    if s.get("approved"):
        raise Refuse("The plan is already approved.", "To change it, use: " +
                     run.flow("plan-revise --file <path> --summary '<what changed>'"))
    plan = load_json_file(a.file, "Plan")
    errors, warnings = validate_plan(plan, run.card)
    if errors:
        say("REFUSED: the plan has %d problem%s. Nothing was saved." % (len(errors), "" if len(errors) == 1 else "s"))
        for e in errors:
            say("  - " + e)
        say("DO: fix the file and run the same command again. Format: reference/plan-format.md")
        return 2
    plan["card"] = run.card
    first = s.get("plan") is None
    s["plan"] = plan
    s["plan_rev"] = s.get("plan_rev", 0) + 1
    s["repos"] = {rp["repo"]: new_repo_state(rp) for rp in plan["repos"]}
    s["title"] = plan["title"]
    if first:
        if not s.get("asked"):
            run.log("stage", name="intake", state="done")
            run.log("stage", name="plan", state="start")
        elif not s.get("interview_closed"):
            run.log("stage", name="interview", state="done")
            run.log("stage", name="plan", state="start")
        run.log("stage", name="plan", state="done")
    run.log("plan", rev=s["plan_rev"], plan=plan)
    if plan_review_due(s):
        if not s.get("plan_review_rev0"):
            s["plan_review_rev0"] = s["plan_rev"]
            run.log("stage", name="plan_review", state="start")
    else:
        run.log("stage", name="approval", state="start")
        run.log("waiting", on="approval", text="Read the plan and approve it, or ask for changes, in chat")
    open_agents = [g for g in s.get("agents") or [] if not g.get("done")]
    n_slices = sum(len(rp["slices"]) + (1 if rp.get("closing") else 0) for rp in plan["repos"])
    say("OK plan version %d saved: %d criteria, %d slices in %d repo%s." % (
        s["plan_rev"], len(plan["criteria"]), n_slices, len(plan["repos"]), "" if len(plan["repos"]) == 1 else "s"),
        "  Plan file: %s" % run.plan_path())
    for w in warnings:
        say("  Note: " + w)
    if not needs_review(plan):
        say("  Trivial work, so on purpose: no plan review, and no code review after the build. Tell the user so",
            "  when you show them the plan.")
    if open_agents:
        say("  Note: %d subagent%s you started ha%s no takeaway recorded (agent-done)." % (
            len(open_agents), "" if len(open_agents) == 1 else "s", "s" if len(open_agents) == 1 else "ve"))
    say("  Write no code until the plan is approved.")


def cmd_plan_approve(run, a):
    s = run.state
    if not s.get("plan"):
        raise Refuse("There is no plan to approve.", next_step(run)[-1])
    if s.get("approved"):
        raise Refuse("The plan is already approved.", next_step(run)[0])
    if plan_review_due(s):
        raise Refuse("This plan has not been reviewed yet. It is reviewed before the user is asked to approve it.",
                     next_step(run)[0])
    said = one_line(a.user_said)
    if not said:
        raise Refuse("--user-said is empty.", "Quote the user's approval. Never approve on their behalf.")
    s["approved"] = {"t": now_iso(), "user_said": said, "rev": s["plan_rev"]}
    run.log("resumed", text=said)
    run.log("stage", name="approval", state="done")
    say("OK plan version %d approved." % s["plan_rev"])


def cmd_plan_reviewed(run, a):
    s = run.state
    if not s.get("plan"):
        raise Refuse("There is no plan to review.", next_step(run)[-1])
    if not plan_review_due(s):
        raise Refuse("No plan review is due.", next_step(run)[0])
    rep = load_json_file(a.file, "Plan review")
    issues = rep.get("issues") if isinstance(rep, dict) else None
    errors, out = [], []
    if not isinstance(issues, list):
        errors.append("'issues' is missing. Use an empty list when the reviewer found nothing.")
        issues = []
    for i, it in enumerate(issues):
        w = "issues[%d]" % (i + 1)
        if not isinstance(it, dict) or not one_line(it.get("text") or "") or it.get("outcome") not in ("fixed", "kept", "asked"):
            errors.append("%s: needs 'text' and an 'outcome' of \"fixed\", \"kept\" or \"asked\"." % w)
            continue
        note = one_line(it.get("note") or "")
        if it["outcome"] == "kept" and not note:
            errors.append("%s: an issue you did not act on needs a 'note' saying why the plan is right as it is." % w)
        if it["outcome"] == "asked" and not open_questions(s) and not note:
            errors.append("%s: an issue put to the user needs an open question (ask) or a 'note' with their answer." % w)
        if len(one_line(it["text"])) > QUESTION_MAX:
            errors.append("%s: the text is over %d characters. Shorten it." % (w, QUESTION_MAX))
        out.append([one_line(it["text"])[:QUESTION_MAX], it["outcome"], note[:QUESTION_MAX]])
    fixed = sum(1 for x in out if x[1] == "fixed")
    if fixed and s["plan_rev"] <= s.get("plan_review_rev0", s["plan_rev"]):
        errors.append("%d issue%s marked fixed, but the plan has not been submitted again since the review. Edit the "
                      "JSON and run plan-submit first." % (fixed, " is" if fixed == 1 else "s are"))
    if errors:
        say("REFUSED: the plan review has %d problem%s." % (len(errors), "" if len(errors) == 1 else "s"))
        for e in errors:
            say("  - " + e)
        say("DO: fix them and run the same command again. Format: reference/review.md")
        return 2
    depth = "light" if s["plan"].get("risk") == "low" else "full"
    s["plan_review"] = {"t": now_iso(), "depth": depth, "issues": len(out), "fixed": fixed, "rev": s["plan_rev"]}
    run.log("plan_review", depth=depth, issues=out, rev=s["plan_rev"])
    run.log("stage", name="plan_review", state="done", fixes=fixed)
    run.log("stage", name="approval", state="start")
    run.log("waiting", on="approval", text="Read the plan and approve it, or ask for changes, in chat")
    asked = sum(1 for x in out if x[1] == "asked")
    say("OK plan review recorded: %d issue%s, %d fixed, %d kept as it was%s." % (
        len(out), "" if len(out) == 1 else "s", fixed, len(out) - fixed - asked,
        (", %d put to the user" % asked) if asked else ""))


def cmd_agent_start(run, a):
    s = run.state
    if a.role not in AGENT_ROLES:
        raise Refuse("--role must be one of: " + ", ".join(AGENT_ROLES),
                     "Use scout for a flow-scout. Do not record reviewers here: review-plan records the code "
                     "reviewers and plan-reviewed records the plan reviewer.")
    agents = s.setdefault("agents", [])
    entry = {"n": len(agents) + 1, "role": a.role, "task": run.limited("task", a.task, HEADLINE_MAX),
             "why": one_line(a.why or "")[:HEADLINE_MAX], "done": False}
    agents.append(entry)
    run.log("agent", n=entry["n"], role=entry["role"], task=entry["task"], why=entry["why"], state="start")
    say("OK subagent %d recorded (%s). When it returns: %s" % (
        entry["n"], entry["role"], run.flow("agent-done %d --takeaway '<what it found, in one line>'" % entry["n"])))


def cmd_agent_done(run, a):
    agents = run.state.get("agents") or []
    entry = next((g for g in agents if g["n"] == a.n), None)
    if not entry:
        raise Refuse("There is no subagent %s." % a.n, "Open: " + (", ".join(str(g["n"]) for g in agents if not g["done"]) or "none"))
    entry["done"] = True
    entry["takeaway"] = run.limited("takeaway", a.takeaway, QUESTION_MAX)
    run.log("agent", n=entry["n"], role=entry["role"], task=entry["task"], state="done", takeaway=entry["takeaway"])
    say("OK takeaway recorded for subagent %d." % entry["n"])


def cmd_plan_revise(run, a):
    s = run.state
    if not s.get("approved"):
        raise Refuse("The plan is not approved yet, so there is nothing to revise.",
                     "Edit the JSON and run: " + run.flow("plan-submit --file <path>"))
    old = s["plan"]
    plan = load_json_file(a.file, "Plan")
    errors, warnings = validate_plan(plan, run.card)

    def refuse_with(errs):
        say("REFUSED: the revised plan has %d problem%s. Nothing was saved." % (len(errs), "" if len(errs) == 1 else "s"))
        for e in errs:
            say("  - " + e)
        say("DO: fix the file and run the same command again. Format: reference/plan-format.md")
        return 2

    if errors:
        return refuse_with(errors)
    old_by = {rp["repo"]: rp for rp in old["repos"]}
    new_repos, reopen = {}, []
    for rp in plan["repos"]:
        name = rp["repo"]
        rs = s["repos"].get(name)
        if rs is None:
            new_repos[name] = new_repo_state(rp)
            continue
        orp = old_by[name]
        slices = []
        for i, u in enumerate(rs["slices"]):
            has = i < len(rp["slices"])
            is_done = u["state"] == "done"
            same = has and unit_key(rp["slices"][i], is_done) == unit_key(orp["slices"][i], is_done)
            if u["state"] == "done" and not same:
                errors.append("%s slice %d is already done and cannot change, move or be removed. Add new work as "
                              "a new slice after it." % (name, i + 1))
            elif has:
                slices.append(u if same else dict(new_unit(), state="in_progress" if u["state"] == "in_progress" else "todo",
                                                  fails=u["fails"]))
            elif u["state"] != "todo":
                errors.append("%s slice %d is %s and cannot be removed." % (name, i + 1, u["state"].replace("_", " ")))
        for _ in range(len(rs["slices"]), len(rp["slices"])):
            slices.append(new_unit())
        closing = rs.get("closing")
        if closing and closing["state"] == "done":
            if not rp.get("closing") or unit_key(rp["closing"], True) != unit_key(orp["closing"], True):
                errors.append("%s closing slice is already done and cannot change. Put new tests in a new build slice." % name)
        elif rp.get("closing"):
            same = orp.get("closing") and unit_key(rp["closing"]) == unit_key(orp["closing"])
            closing = closing if (closing and same) else dict(new_unit(), state=closing["state"] if closing else "todo")
        else:
            if closing and closing["state"] != "todo":
                errors.append("%s closing slice is in progress and cannot be removed." % name)
            closing = None
        if rs["stage"] in ("self_check", "closing", "code_review", "suite", "built") and any(u["state"] == "todo" for u in slices):
            rv0 = review_of(rs)
            if closing and closing["state"] == "in_progress":
                errors.append("%s: the closing slice is in progress. Finish it with slice-done first, then add the "
                              "build slice." % name)
            elif rs["stage"] == "code_review" and rv0["started"] and not rv0["done"]:
                errors.append("%s: its review is open. Record it first with review-report (a finding the user decided "
                              "has the outcome \"asked\"), then run plan-revise again. The new slice gets its own "
                              "review." % name)
            else:
                reopen.append(name)
        new_repos[name] = dict(rs, slices=slices, closing=closing)
    for name in old_by:
        if name not in new_repos and s["repos"][name]["stage"] != "todo":
            errors.append("Repo %s has work in it and cannot be removed from the plan." % name)
    if errors:
        return refuse_with(errors)
    needs_user = approval_view(old) != approval_view(plan)
    said = one_line(a.user_said or "")
    if needs_user and not said:
        raise Refuse("This revision changes what the user approved (criteria, contract, scope, decisions, "
                     "expected test changes, risk, rollout, branch or repos).",
                     "Stop. Tell the user exactly what changes and why. When they agree, run the same command with "
                     "--user-said '<their words>'.")
    text = run.limited("change summary", a.summary, HEADLINE_MAX * 2)
    plan["card"] = run.card
    s["plan"] = plan
    s["plan_rev"] += 1
    s["repos"] = new_repos
    notes = []
    for name, rs in new_repos.items():
        if name in reopen:
            was = rs["stage"]
            prev = rs["selfcheck"]
            since = prev.get("since")
            if prev["done"]:
                try:
                    since = git(run.repo_path(name), "rev-parse", "HEAD")
                except Refuse:
                    pass
            rs["selfcheck"] = {"started": False, "start_commit": None, "done": False, "since": since}
            rv = review_of(rs)
            rs["review"] = new_review(since if rv["done"] else rv.get("since"))
            rs["suite"] = {"ok": False, "tree": None, "passed": 0}
            rs["verify"] = {"ok": False, "tree": None}
            rs["stage"] = "build"
            run.log("stage", name="build", state="start", repo=name, reopened=True, was=was)
            notes.append("%s is back in its build stage for the new slice. Its self-check%s and full suite will run again." % (
                name, ", review" if needs_review(plan) else ""))
        elif rs["stage"] == "build" and rs["slices"] and all(u["state"] == "done" for u in rs["slices"]):
            rs["stage"] = "self_check"
            run.log("stage", name="build", state="done", repo=name)
    approval = ("approved by the user: " + said) if needs_user else "no approval needed, slices only"
    reqs = s.get("change_requests") or []
    change = {"t": now_iso(), "text": text, "approval": approval, "rev": s["plan_rev"]}
    s["changes"].append(change)
    run.log("plan", rev=s["plan_rev"], plan=plan)
    run.log("plan_change", text=text, approval=approval, rev=s["plan_rev"], needs_user=needs_user,
            change=reqs[-1]["id"] if reqs else None)
    if needs_user:
        for rs in new_repos.values():
            if rs["pr"]:
                rs["pr"]["stale"] = True
    settled = [c["id"] for c in reqs if c.get("open")]
    for c in reqs:
        c["open"] = False
    if settled:
        notes.insert(0, "Change request %s is settled by this plan change." % ", ".join(map(str, settled)))
    say("OK plan revised to version %d. %s." % (s["plan_rev"], (approval[0].upper() + approval[1:]).rstrip(". ")))
    for n in notes:
        say("  " + n)
    for w in warnings:
        say("  Note: " + w)


# ---------------------------------------------------------------- commands: build

def instruction_files(run, repo):
    found = []
    for base in (repo, run.workspace()):
        p = base / ".github" / "copilot-instructions.md"
        if p.is_file() and p not in found:
            found.append(p)
        d = base / ".github" / "instructions"
        if d.is_dir():
            found.extend(sorted(d.glob("*.instructions.md")))
    return found


def cmd_repo_start(run, a):
    s = run.state
    if not s.get("approved"):
        raise Refuse("The plan is not approved yet.", next_step(run)[0])
    name = a.repo
    rp = run.repo_plan(name)
    rs = s["repos"][name]
    for other in s["plan"]["repos"]:
        if other["repo"] == name:
            break
        if s["repos"][other["repo"]]["stage"] != "built":
            raise Refuse("%s comes first in the plan and is not built yet." % other["repo"], next_step(run)[0])
    if rs["stage"] != "todo":
        raise Refuse("%s is already started (stage: %s)." % (name, rs["stage"]), next_step(run)[0])
    repo = run.repo_path(name)
    branch, base = s["plan"]["branch"], rp["base"]
    cur = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    notes = []
    if git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch, code=True) == 0:
        if cur != branch:
            if dirty(repo):
                raise Refuse("%s has uncommitted changes on '%s'." % (name, cur),
                             "Tell the user. Do not stash, reset or discard them yourself.")
            git(repo, "checkout", branch)
        notes.append("The branch already existed, so work continues on it.")
        start = git(repo, "merge-base", "HEAD", "origin/" + base, check=False) or git(repo, "rev-parse", "HEAD")
    else:
        if dirty(repo):
            raise Refuse("%s has uncommitted changes on '%s'." % (name, cur),
                         "Tell the user. Do not stash, reset or discard them yourself.")
        if git(repo, "fetch", "origin", base, code=True) != 0:
            notes.append("WARNING: could not fetch origin/%s. Branching from the local copy, which may be stale. Tell the user." % base)
        if git(repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + base, code=True) == 0:
            origin = "origin/" + base
        elif git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + base, code=True) == 0:
            origin = base
        else:
            raise Refuse("Base branch '%s' was not found in %s." % (base, name), "Check the base branch in the plan.")
        git(repo, "checkout", "--no-track", "-b", branch, origin)
        start = git(repo, "rev-parse", "HEAD")
        notes.append("Created branch %s from %s." % (branch, origin))
    rs["start_commit"] = start
    rs["stage"] = "build"
    run.log("stage", name="build", state="start", repo=name)
    say("OK %s started on branch %s." % (name, branch))
    for n in notes:
        say("  " + n)
    files = instruction_files(run, repo)
    if files:
        say("  Read these instruction files now. They may not have loaded on their own:")
        for f in files:
            say("    " + str(f))
    for other in s["plan"]["repos"]:
        h = s["repos"][other["repo"]].get("handoff")
        if h:
            say("  Handoff from %s: %s" % (other["repo"], h))


def cmd_slice_start(run, a):
    name = active_repo(run, a.repo)
    rp, rs = run.repo_plan(name), run.state["repos"][name]
    if rs["stage"] not in ("build", "closing"):
        raise Refuse("%s is not at a slice right now (stage: %s)." % (name, rs["stage"]), next_step(run)[0])
    u, n = cur_unit(rs)
    if u:
        raise Refuse("%s is still in progress. The next slice starts only when this one is verified and done."
                     % unit_label(n).capitalize(), next_step(run)[0])
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    if rs["stage"] == "closing":
        u, n = rs["closing"], None
        run.log("stage", name="closing", state="start", repo=name)
    else:
        todo = [i + 1 for i, x in enumerate(rs["slices"]) if x["state"] == "todo"]
        if not todo:
            raise Refuse("Every slice in %s is done." % name, next_step(run)[0])
        n = todo[0]
        u = rs["slices"][n - 1]
        run.log("slice", repo=name, n=n, state="in_progress")
    u["state"] = "in_progress"
    spec = unit_spec(rp, n)
    total = len(rs["slices"])
    say("OK %s: %s%s" % (name, ("slice %d of %d: " % (n, total)) if n else "closing slice: ", spec["title"]),
        "  Done when: " + spec["done_when"])
    if spec.get("approach"):
        say("  Approach:  " + spec["approach"])
    if spec.get("tests_to_add"):
        say("  Tests to add: " + ", ".join(spec["tests_to_add"]))
    if spec.get("proves"):
        crit = {c["id"]: c["text"] for c in run.plan["criteria"]}
        for cid in spec["proves"]:
            say("  Proves %s: %s" % (cid, crit.get(cid, "")))
    for chk in spec["checks"]:
        say("  Check: " + chk["cmd"])
    say("  Rules: build only this slice. Leave no stubs or TODOs. Follow the instruction files.",
        "  If the slice turns out wrong or too big, resplit with plan-revise before going on.")


def do_checks(run, name, repo, cmds, label, slice_n=None, stage=None):
    """Run commands in order, stop at the first failure. Returns (all ok, results by command)."""
    results, all_ok = {}, True
    for i, (cmd, timeout) in enumerate(cmds):
        outfile = run.dir / "out" / ("%s-%s-%d.txt" % (name, label, i + 1))
        say("RUN %d/%d  %s" % (i + 1, len(cmds), cmd))
        sys.stdout.flush()
        code, timed_out, secs = run_command(cmd, repo, timeout, outfile)
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", read_text_any(outfile))
        found, ran, failed, skipped = parse_tests(text)
        reason = ""
        if timed_out:
            ok, reason = False, "timed out after %d s" % timeout
        elif code != 0:
            ok, reason = False, "exit code %s" % code
        elif failed:
            ok, reason = False, "%d failing" % failed
        elif expects_tests(cmd) and ran - skipped <= 0:
            ok, reason = False, "no tests ran"
        else:
            ok = True
        passed = max(ran - failed - skipped, 0)
        res = {"ok": ok, "passed": passed, "failed": failed, "skipped": skipped, "secs": int(round(secs)),
               "timed_out": timed_out, "reason": reason, "out": str(outfile), "t": now_iso()}
        results[cmd] = res
        ev = dict(repo=name, command=cmd, passed=passed, failed=failed, skipped=skipped, secs=res["secs"], ok=ok,
                  timed_out=timed_out, reason=reason)
        if not ok and not timed_out:
            ev["errors"] = [l[:200] for l in failure_lines(text)[:3]]
        if slice_n:
            ev["slice"] = slice_n
        else:
            ev["stage"] = stage
        run.log("check", **ev)
        counts = ("%d passed, %d failed%s in %d s" % (passed, failed, (", %d skipped" % skipped) if skipped else "",
                                                     res["secs"])) if found else ("finished in %d s" % res["secs"])
        if ok:
            say("  PASS  " + counts)
            continue
        all_ok = False
        say("  FAIL  %s  (%s)" % (counts, reason))
        if timed_out:
            say(*SLOW_HELP)
        else:
            if reason == "no tests ran":
                say("  The command finished but ran no tests. Check the test name or module in the command.")
            else:
                say("  First errors:")
                for line in failure_lines(text):
                    say("    " + line)
            say("  Full output, only if the lines above are not enough: " + str(outfile))
        break
    return all_ok, results


def cmd_check(run, a):
    name = active_repo(run, a.repo)
    rp, rs = run.repo_plan(name), run.state["repos"][name]
    u, n = cur_unit(rs)
    if not u:
        raise Refuse("No slice is in progress in %s." % name, next_step(run)[0])
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    spec = unit_spec(rp, n)
    cmds = [(c["cmd"], int(c.get("timeout_sec") or CHECK_TIMEOUT_SEC)) for c in spec["checks"]]
    ok, results = do_checks(run, name, repo, cmds, "slice-%s" % (n or "closing"), slice_n=n, stage="closing")
    u["results"] = results
    if ok:
        u["tree"] = tree_now(repo)
        files = staged_files(repo)
        say("OK %s is verified. %d file%s will be committed:" % (unit_label(n), len(files), "" if len(files) == 1 else "s"))
        for f in files[:25]:
            say("    %s %s" % (f[0][0], f[-1]))
        if len(files) > 25:
            say("    ... and %d more" % (len(files) - 25))
        if not files:
            say("    (nothing has changed since the last commit)")
        say("  If a file in that list does not belong to this slice, remove or revert it and run check again.")
        return 0
    u["tree"] = None
    u["fails"] += 1
    if u["fails"] >= 2:
        run.log("event", kind="check_failed_again", repo=name, text="%s has failed its check %d times" % (
            unit_label(n).capitalize(), u["fails"]))
    if u["fails"] >= 3:
        say("  This check has now failed %d times. Stop guessing. Re-read the slice and the first error, and if "
            "the plan is wrong, say so to the user." % u["fails"])
    return 1


def expected_test_guard(run, name, repo, rs):
    """Refuse a commit that rewrites a test from the base branch unless the plan lists it. Pure additions pass."""
    allowed = set()
    for t in tests_for(run.plan, name):
        allowed.add(Path(t["test"].split(".")[0].split("#")[0]).stem)
        allowed.add(Path(t["test"]).name)
    bad = []
    for parts in staged_files(repo):
        status, path = parts[0][0], parts[1]
        if status not in "MDR" or "src/test/" not in path.replace("\\", "/"):
            continue
        if git(repo, "cat-file", "-e", "%s:%s" % (rs["start_commit"], path), code=True) != 0:
            continue  # the file is new on this branch
        if status == "M":
            stat = git(repo, "diff", "--cached", "--numstat", "HEAD", "--", path).split()
            if len(stat) >= 2 and stat[1] == "0":
                continue  # lines were only added: new tests in an existing class
        if Path(path).stem not in allowed and Path(path).name not in allowed:
            bad.append(path)
    if bad:
        raise Refuse("This commit changes or removes lines in existing tests that are not on the plan's "
                     "expected-to-change list: " + ", ".join(bad),
                     "Treat it as a regression: restore the test and fix the code. If the test really must change, "
                     "that is a plan change. Tell the user, add it to tests_expected_to_change, and run plan-revise "
                     "with --user-said. (Adding new test methods to an existing class needs no listing.)")


def make_commit(run, name, repo, a, slice_n=None, kind="slice"):
    """Commit what is staged, in the house format. Returns the event fields."""
    subject = one_line(a.subject)
    if subject.upper().startswith(run.card):
        subject = subject[len(run.card):].lstrip(" :-")
    subject = run.limited("commit subject", run.card + " " + subject, SUBJECT_MAX)
    why = one_line(a.why or "")
    if not why and not a.mechanical:
        raise Refuse("A commit that changes behavior needs --why.",
                     "Add --why '<the reason>'. For a purely mechanical commit (rename, formatting) use --mechanical.")
    body = []
    if why:
        body.append("Why: " + why)
    if a.what:
        body.append("What: " + one_line(a.what))
    tail = []
    if a.touches:
        tail.append("Touches: " + one_line(a.touches))
    if a.note:
        tail.append("Note: " + one_line(a.note))
    msg = subject
    if body:
        msg += "\n\n" + "\n".join(body)
    if tail:
        msg += "\n\n" + "\n".join(tail)
    msgfile = run.dir / "out" / "commit-message.txt"
    msgfile.write_text(msg + "\n", encoding="utf-8")
    git(repo, "commit", "-F", str(msgfile))
    h = head(repo)
    stat = git(repo, "show", "--shortstat", "--format=", "HEAD")
    nums = lambda pat: int((re.search(pat, stat) or [0, 0])[1])
    ev = dict(repo=name, hash=h, subject=subject, url=commit_url(repo, h), files=nums(r"(\d+) files? changed"),
              added=nums(r"(\d+) insertions?"), deleted=nums(r"(\d+) deletions?"), kind=kind,
              why=why, what=one_line(a.what or ""), touches=one_line(a.touches or ""), note=one_line(a.note or ""))
    if slice_n:
        ev["slice"] = slice_n
    run.log("commit", **ev)
    rs = run.state["repos"][name]
    rs["commits"].append({"hash": h, "subject": subject, "what": ev["what"],
                          "touches": ev["touches"], "note": ev["note"], "kind": kind})
    if rs["pr"]:
        # a commit after the PR is open: CI must pass again, and after a change request the description is out of date
        rs["pr"]["ready"] = False
        if run.state.get("change_requests") or kind == "slice":
            rs["pr"]["stale"] = True
    for c in run.state.get("change_requests") or []:
        if c.get("open"):
            c["open"] = False
            ev["settles"] = c["id"]
    return ev


def cmd_slice_done(run, a):
    name = active_repo(run, a.repo)
    rp, rs = run.repo_plan(name), run.state["repos"][name]
    u, n = cur_unit(rs)
    if not u:
        raise Refuse("No slice is in progress in %s." % name, next_step(run)[0])
    spec = unit_spec(rp, n)
    if not unit_checked(u, spec):
        raise Refuse("%s has no passing check yet. A slice is done only when its check has run and passed."
                     % unit_label(n).capitalize(), run.flow("check"))
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    if tree_now(repo) != u["tree"]:
        u["tree"] = None
        run.save()
        raise Refuse("Files changed after the check passed, so the check no longer covers what would be committed.",
                     run.flow("check"))
    if u["tree"] == head_tree(repo):
        raise Refuse("Nothing has changed since the last commit, so there is nothing to commit for %s." % unit_label(n),
                     "If the slice needs no code, remove it from the plan with plan-revise.")
    expected_test_guard(run, name, repo, rs)
    prod = [p[-1] for p in staged_files(repo) if "src/main/" in p[-1].replace("\\", "/")] if n is None else []
    ev = make_commit(run, name, repo, a, slice_n=n, kind="slice" if n else "closing")
    if prod:
        say("  Note: the closing slice changed production code (%s). The self-check ran before it and did not" % ", ".join(prod[:3]),
            "  cover those lines. Check them against reference/self-check.md now, and tell the user. Next time put",
            "  behavior in a build slice: plan-revise can add one at any point.")
    u["state"], u["commit"] = "done", ev["hash"]
    say("OK %s committed as %s: %s" % (unit_label(n), ev["hash"], ev["subject"]),
        "  %d file%s, +%d -%d" % (ev["files"], "" if ev["files"] == 1 else "s", ev["added"], ev["deleted"]))
    if ev.get("settles"):
        say("  Change request %d is settled by this commit." % ev["settles"])
    if n is None:
        run.log("stage", name="closing", state="done", repo=name)
        rs["stage"] = stage_after_tests(run, rs)
    else:
        run.log("slice", repo=name, n=n, state="done", commit=ev["hash"])
        if all(x["state"] == "done" for x in rs["slices"]):
            run.log("stage", name="build", state="done", repo=name)
            rs["stage"] = "self_check"


def cmd_selfcheck_start(run, a):
    name = active_repo(run, a.repo)
    rs = run.state["repos"][name]
    if rs["stage"] != "self_check":
        raise Refuse("%s is not at the self-check (stage: %s)." % (name, rs["stage"]), next_step(run)[0])
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    sc = rs["selfcheck"]
    if not sc["started"]:
        sc.update(started=True, start_commit=head(repo), fixes_before=len(rs["fix_commits"]))
        run.log("stage", name="self_check", state="start", repo=name)
    base = sc.get("since") or rs["start_commit"]
    say("OK self-check for %s. Walk the whole diff once%s:" % (
            name, " (only what was added since the last self-check)" if sc.get("since") else ""),
        "    git -C \"%s\" diff %s..HEAD" % (repo, base[:10]),
        "  Judge each item below against that diff:")
    for cid, text in SELF_CHECK:
        say("    %-16s %s" % (cid, text))
    files = instruction_files(run, repo) + global_instruction_files()
    if files:
        say("  Instruction files to re-read for the last item:")
        for f in files:
            say("    " + str(f))
    else:
        say("  No instruction file was found for this repo. Apply whatever coding instructions you were given.")
    say("  Fix what you find directly. After each fix: verify, then commit. One commit per fix.",
        "  A fix that changes behavior, or a pattern the repo is missing, goes to the user first (ask).",
        "  Then write the report: reference/self-check.md has the file format and the rules for edge cases.")


def cmd_selfcheck_report(run, a):
    name = active_repo(run, a.repo)
    rs = run.state["repos"][name]
    sc = rs["selfcheck"]
    if rs["stage"] != "self_check" or not sc["started"]:
        raise Refuse("The self-check for %s has not been started." % name, next_step(run)[0])
    rep = load_json_file(a.file, "Self-check report")
    items = rep.get("items") if isinstance(rep, dict) else None
    trivial = run.plan.get("work") == "trivial"
    errors = []
    if not isinstance(items, dict):
        errors.append("'items' is missing.")
        items = {}
    out_items = []
    for cid, text in SELF_CHECK:
        it = items.get(cid)
        if not isinstance(it, dict) or it.get("result") not in ("ok", "fixed", "na", "accepted"):
            errors.append("%s: needs a 'result' of \"ok\", \"fixed\", \"na\" or \"accepted\"." % cid)
            continue
        note = one_line(it.get("note") or "")
        if not note and (it["result"] in ("fixed", "accepted") or (it["result"] == "na" and not trivial)):
            errors.append("%s: a result of \"%s\" needs a 'note' saying %s." % (
                cid, it["result"], {"fixed": "what was fixed", "accepted": "what the gap is and that the user accepted it",
                                    "na": "why it does not apply"}[it["result"]]))
        out_items.append([text, it["result"], note[:HEADLINE_MAX]])
    logging_ids = ("entry_logged", "branches_logged", "state_logged", "integrations", "errors")
    if any(items.get(c, {}).get("result") in ("ok", "fixed") for c in logging_ids if isinstance(items.get(c), dict)):
        for key in ("log_success", "log_failure"):
            if not one_line(rep.get(key) or ""):
                errors.append("'%s' is missing. Paste one log line the new code writes, or write \"none: <why this "
                              "change has no such line>\"." % key)
    fixed = [c for c in SELF_CHECK_IDS if isinstance(items.get(c), dict) and items[c].get("result") == "fixed"]
    repo = run.repo_path(name)
    if not errors:
        if fixed and len(rs["fix_commits"]) <= sc.get("fixes_before", 0):
            errors.append("Items are marked fixed (%s) but no fix was committed since the self-check started."
                          % ", ".join(fixed))
        if tree_now(repo) != head_tree(repo):
            errors.append("There are uncommitted changes. Verify and commit each fix first.")
    if errors:
        say("REFUSED: the self-check report has %d problem%s." % (len(errors), "" if len(errors) == 1 else "s"))
        for e in errors:
            say("  - " + e)
        say("DO: fix them and run the same command again. Format: reference/self-check.md")
        return 2
    sc["done"] = True
    run.log("selfcheck", repo=name, items=out_items, log_success=one_line(rep.get("log_success") or "")[:400],
            log_failure=one_line(rep.get("log_failure") or "")[:400])
    run.log("stage", name="self_check", state="done", repo=name, fixes=len(fixed))
    closing = rs.get("closing")
    rs["stage"] = "closing" if closing and closing["state"] != "done" else stage_after_tests(run, rs)
    say("OK self-check recorded for %s: %d fixed, %d not applicable." % (
        name, len(fixed), sum(1 for x in out_items if x[1] == "na")))
    if not needs_review(run.plan):
        say("  Trivial work, so no code review runs for this repo. That is on purpose.")


def targeted_cmds(run, name):
    rp, rs = run.repo_plan(name), run.state["repos"][name]
    cmds, seen = [], set()
    units = [(rp["slices"][i], u) for i, u in enumerate(rs["slices"])]
    if rs.get("closing"):
        units.append((rp["closing"], rs["closing"]))
    for spec, u in units:
        if u["state"] != "done":
            continue
        for c in spec["checks"]:
            if c["cmd"] not in seen:
                seen.add(c["cmd"])
                cmds.append((c["cmd"], int(c.get("timeout_sec") or CHECK_TIMEOUT_SEC)))
    return cmds


def cmd_verify(run, a):
    name = active_repo(run, a.repo)
    rs = run.state["repos"][name]
    u, n = cur_unit(rs)
    if u:
        raise Refuse("%s is in progress. Use check for a slice." % unit_label(n).capitalize(), run.flow("check"))
    rp = run.repo_plan(name)
    cmds = targeted_cmds(run, name)
    if a.full:
        cmds = [(rp.get("full_suite") or "mvn verify", int(rp.get("suite_timeout_min") or SUITE_TIMEOUT_MIN) * 60)]
    if not cmds:
        raise Refuse("No slice is done in %s yet, so there is nothing to verify." % name, next_step(run)[0])
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    ok, results = do_checks(run, name, repo, cmds, "verify", stage={"suite": "review", "built": "pr"}.get(rs["stage"], rs["stage"]))  # "review" is the log's old name for the full-suite step
    rs["verify"] = {"ok": ok, "tree": tree_now(repo) if ok else None}
    if ok and a.full:
        rs["suite"].update(tree=rs["verify"]["tree"], passed=results[cmds[0][0]]["passed"])
        say("OK the full suite passes with your change: %d tests." % rs["suite"]["passed"])
        return 0
    if ok:
        say("OK the slices' own tests pass with your change.")
        if rs["stage"] == "built":
            say("  They cover what the slices built. If your change touches code outside that, run the whole suite",
                "  instead: " + run.flow("verify --full" + ((" --repo " + name) if a.repo else "")))
        return 0
    return 1


def cmd_commit(run, a):
    name = active_repo(run, a.repo)
    rs = run.state["repos"][name]
    u, n = cur_unit(rs)
    if u or rs["stage"] == "todo":
        raise Refuse("In a slice, the commit is made by slice-done.", run.flow("check") + " then slice-done.")
    if rs["stage"] == "self_check" and not rs["selfcheck"]["started"]:
        raise Refuse("Start the self-check first.", run.flow("selfcheck-start"))
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    tree = tree_now(repo)
    if tree == head_tree(repo):
        raise Refuse("Nothing has changed since the last commit.")
    if not ((rs["verify"]["ok"] and rs["verify"]["tree"] == tree) or (rs["suite"]["ok"] and rs["suite"]["tree"] == tree)):
        raise Refuse("These changes have not passed a test run. Either nothing was run, or files changed afterwards.",
                     run.flow("verify" + ((" --repo " + name) if a.repo else "")))
    expected_test_guard(run, name, repo, rs)
    ev = make_commit(run, name, repo, a, kind="fix")
    rs["fix_commits"].append(ev["hash"])
    say("OK fix committed as %s: %s" % (ev["hash"], ev["subject"]))
    if ev.get("settles"):
        say("  Change request %d is settled by this commit." % ev["settles"])


def cmd_review_start(run, a):
    name = active_repo(run, a.repo)
    rp, rs = run.repo_plan(name), run.state["repos"][name]
    if rs["stage"] != "code_review":
        raise Refuse("%s is not at the review (stage: %s)." % (name, rs["stage"]), next_step(run)[0])
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    rv = review_of(rs)
    if not rv["started"]:
        rv.update(started=True, start_commit=head(repo), fixes_before=len(rs["fix_commits"]))
        run.log("stage", name="code_review", state="start", repo=name)
    base = rv.get("since") or rs["start_commit"]
    names = [r["repo"] for r in run.plan["repos"]]
    cross = len(names) > 1 and name == names[-1]
    say("OK review of %s. The pr-review skill does it, in the way its section \"When build-flow calls you\" says." % name,
        "  If the pr-review skill is not installed, stop and tell the user. Do not review the code yourself instead.",
        "  What to review%s:" % (" (only what was added since the last review)" if rv.get("since") else ""),
        "    git -C \"%s\" diff %s..HEAD" % (repo, base[:10]),
        "  The card's criteria and decisions: %s" % run.plan_path())
    files = instruction_files(run, repo) + global_instruction_files()
    for f in files:
        say("  Instruction file for the reviewers: %s" % f)
    say("  Axes to triage, each with a reason to run or to skip: " + ", ".join(i for i, _ in REVIEW_AXES))
    if cross:
        say("  This is the last repo of %d, so also run the axis cross_repo. Give that reviewer every repo's diff:" % len(names))
        for other in names:
            ors = run.state["repos"][other]
            if ors.get("start_commit"):
                say("    git -C \"%s\" diff %s..HEAD" % (run.repo_path(other), ors["start_commit"][:10]))
    say("  File formats for review-plan and review-report: reference/review.md")


def cmd_review_plan(run, a):
    name = active_repo(run, a.repo)
    rs = run.state["repos"][name]
    rv = review_of(rs)
    if rs["stage"] != "code_review" or not rv["started"]:
        raise Refuse("The review of %s has not been started." % name, next_step(run)[0])
    rep = load_json_file(a.file, "Review plan")
    axes = rep.get("axes") if isinstance(rep, dict) else None
    names = [r["repo"] for r in run.plan["repos"]]
    cross = len(names) > 1 and name == names[-1]
    wanted = REVIEW_AXES + ([CROSS_AXIS] if cross else [])
    errors, out = [], []
    if not isinstance(axes, dict):
        errors.append("'axes' is missing.")
        axes = {}
    for aid, label in wanted:
        it = axes.get(aid)
        if not isinstance(it, dict) or not isinstance(it.get("run"), bool) or not one_line(it.get("why") or ""):
            errors.append("%s: needs 'run' (true or false) and a 'why' in one line." % aid)
            continue
        out.append([aid, it["run"], one_line(it["why"])[:HEADLINE_MAX]])
    if cross and isinstance(axes.get("cross_repo"), dict) and axes["cross_repo"].get("run") is False:
        errors.append("cross_repo: must run for the last repo of a multi-repo card.")
    if not errors and not any(x[1] for x in out):
        errors.append("No axis runs. A change that is being reviewed needs at least one reviewer.")
    if errors:
        say("REFUSED: the review plan has %d problem%s." % (len(errors), "" if len(errors) == 1 else "s"))
        for e in errors:
            say("  - " + e)
        say("DO: fix the file and run the same command again. Format: reference/review.md")
        return 2
    rv.update(planned=True, axes=out)
    run.log("review_plan", repo=name, axes=[[x[0], AXIS_LABEL[x[0]], x[1], x[2]] for x in out])
    running = [x[0] for x in out if x[1]]
    say("OK %d reviewer%s to launch: %s. Skipped: %s." % (
        len(running), "" if len(running) == 1 else "s", ", ".join(running),
        ", ".join(x[0] for x in out if not x[1]) or "none"))


def cmd_review_report(run, a):
    name = active_repo(run, a.repo)
    rs = run.state["repos"][name]
    rv = review_of(rs)
    if rs["stage"] != "code_review" or not rv["planned"]:
        raise Refuse("The review of %s has no recorded axes yet." % name, next_step(run)[0])
    rep = load_json_file(a.file, "Review report")
    if not isinstance(rep, dict):
        rep = {}
    takes = rep.get("axes") if isinstance(rep.get("axes"), dict) else {}
    running = [x[0] for x in rv["axes"] if x[1]]
    errors, out_axes, out_find = [], [], []
    for aid in running:
        t = one_line(str(takes.get(aid) or ""))
        if not t:
            errors.append("axes.%s: needs the reviewer's takeaway in one line." % aid)
        out_axes.append([aid, AXIS_LABEL[aid], t[:QUESTION_MAX]])
    findings = rep.get("findings")
    if not isinstance(findings, list):
        errors.append("'findings' is missing. Use an empty list when no finding stood.")
        findings = []
    for i, f in enumerate(findings):
        w = "findings[%d]" % (i + 1)
        if not isinstance(f, dict):
            errors.append("%s: must be an object." % w)
            continue
        text, note = one_line(f.get("text") or ""), one_line(f.get("note") or "")
        if f.get("axis") not in AXIS_LABEL:
            errors.append("%s: 'axis' must be one of the axis ids." % w)
        if f.get("severity") not in SEVERITIES:
            errors.append("%s: 'severity' must be one of: %s." % (w, ", ".join(SEVERITIES)))
        if not text:
            errors.append("%s: needs 'text': what is wrong and why it matters." % w)
        elif len(text) > QUESTION_MAX:
            errors.append("%s: the text is %d characters. The limit is %d." % (w, len(text), QUESTION_MAX))
        if f.get("outcome") not in OUTCOMES:
            errors.append("%s: 'outcome' must be one of: %s." % (w, ", ".join(OUTCOMES)))
        elif f["outcome"] != "fixed" and not note:
            errors.append("%s: an outcome of \"%s\" needs a 'note': %s." % (w, f["outcome"], {
                "asked": "what the user decided, in their words", "rejected": "why the finding is wrong",
                "left": "why it is left as it is"}[f["outcome"]]))
        if f.get("outcome") == "left" and f.get("severity") in ("blocker", "major"):
            errors.append("%s: a %s cannot be left. Fix it, put it to the user (ask), or reject it with the reason." % (
                w, f.get("severity")))
        out_find.append({"axis": f.get("axis"), "severity": f.get("severity"), "where": one_line(f.get("where") or "")[:200],
                         "text": text[:QUESTION_MAX], "outcome": f.get("outcome"), "note": note[:QUESTION_MAX]})
    repo = run.repo_path(name)
    n_fixed = sum(1 for f in out_find if f["outcome"] == "fixed")
    if not errors:
        if n_fixed and len(rs["fix_commits"]) <= rv.get("fixes_before", 0):
            errors.append("%d finding%s marked fixed, but no fix was committed since the review started." % (
                n_fixed, " is" if n_fixed == 1 else "s are"))
        if tree_now(repo) != head_tree(repo):
            errors.append("There are uncommitted changes. Verify and commit each fix first.")
    if errors:
        say("REFUSED: the review report has %d problem%s." % (len(errors), "" if len(errors) == 1 else "s"))
        for e in errors:
            say("  - " + e)
        say("DO: fix them and run the same command again. Format: reference/review.md")
        return 2
    for f in out_find:
        run.log("finding", repo=name, review=True, axis=f["axis"], severity=f["severity"],
                caught_by=AXIS_LABEL[f["axis"]] + " reviewer", where=f["where"], text=f["text"],
                outcome=OUTCOMES[f["outcome"]] + (": " + f["note"] if f["note"] else "."), result=f["outcome"])
    dropped = rep.get("dropped") if isinstance(rep.get("dropped"), int) else 0
    run.log("review", repo=name, axes=out_axes, findings=len(out_find), fixed=n_fixed, dropped=dropped)
    run.log("stage", name="code_review", state="done", repo=name, fixes=n_fixed)
    rv["done"] = True
    rs["stage"] = "suite"
    rs.setdefault("review_left", []).extend(
        "%s (%s)" % (f["text"], f["note"]) for f in out_find if f["outcome"] == "left")
    counts = ", ".join("%d %s" % (sum(1 for f in out_find if f["outcome"] == k), v.lower()) for k, v in OUTCOMES.items()
                       if any(f["outcome"] == k for f in out_find))
    say("OK review recorded for %s: %d finding%s%s%s." % (
        name, len(out_find), "" if len(out_find) == 1 else "s", (" (" + counts + ")") if counts else "",
        (", %d dropped when you checked them" % dropped) if dropped else ""))
    if any(f["outcome"] == "asked" for f in out_find):
        say("  If a decision the user made here changes the plan (a criterion, a decision, a new slice), run plan-revise",
            "  now, before the suite. A new slice gets its own self-check and review.")
    if any(f["outcome"] == "left" for f in out_find):
        say("  Findings left as they are will be listed for the user when the run finishes.")


def cmd_suite(run, a):
    name = active_repo(run, a.repo)
    rp, rs = run.repo_plan(name), run.state["repos"][name]
    if rs["stage"] != "suite":
        raise Refuse("%s is not at the full suite (stage: %s)." % (name, rs["stage"]), next_step(run)[0])
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    if not rs["suite"].get("started"):
        rs["suite"]["started"] = True
        run.log("stage", name="review", state="start", repo=name)
    cmd = rp.get("full_suite") or "mvn verify"
    timeout = int(rp.get("suite_timeout_min") or SUITE_TIMEOUT_MIN) * 60
    ok, results = do_checks(run, name, repo, [(cmd, timeout)], "suite", stage="review")
    res = results[cmd]
    rs["suite"].update(ok=False, tree=None)
    if not ok:
        say("  Fix the cause, then run suite again. A failing test you did not plan to change is a regression:",
            "  fix the code, not the test.")
        return 1
    tree = tree_now(repo)
    rs["suite"].update(ok=True, tree=tree, passed=res["passed"])
    if tree != head_tree(repo):
        say("OK the full suite passes: %d tests. The changes it covered are not committed yet." % res["passed"])
        return 0
    say("OK the full suite passes on the committed code: %d tests." % res["passed"])


def cmd_repo_done(run, a):
    name = active_repo(run, a.repo)
    rs = run.state["repos"][name]
    if rs["stage"] != "suite" or not rs["suite"]["ok"]:
        raise Refuse("%s has not passed its full suite." % name, next_step(run)[0])
    repo = run.repo_path(name)
    if tree_now(repo) != rs["suite"]["tree"]:
        rs["suite"]["ok"] = False
        run.save()
        raise Refuse("The code changed after the full suite passed.", run.flow("suite"))
    if head_tree(repo) != rs["suite"]["tree"]:
        raise Refuse("The changes the full suite covered are not committed.",
                     run.flow("commit --subject '...' --why '...'"))
    names = [rp["repo"] for rp in run.plan["repos"]]
    if names.index(name) < len(names) - 1:
        note = one_line(a.handoff or "")
        if not note:
            raise Refuse("Another repo follows, and it starts without your memory of this one.",
                         "Add --handoff '<what was actually built that the next repo relies on: final names, "
                         "where the schema lives, anything that differs from the plan>'")
        rs["handoff"] = note[:600]
    rs["stage"] = "built"
    run.log("stage", name="review", state="done", repo=name, handoff=rs["handoff"] or "")
    say("OK %s is built and verified." % name)


# ---------------------------------------------------------------- commands: finish

def cmd_push(run, a):
    def wants(name, rs):
        try:
            return rs["pushed_commit"] != head(run.repo_path(name))
        except Refuse:
            return False
    name = pick_repo(run, a.repo, wants, "a push")
    rs = run.state["repos"][name]
    if any(run.state["repos"][rp["repo"]]["stage"] != "built" for rp in run.plan["repos"]):
        raise Refuse("Pushing waits until every repo is built and verified.", next_step(run)[0])
    repo = run.repo_path(name)
    on_feature_branch(run, repo)
    if tree_now(repo) != head_tree(repo):
        raise Refuse("%s has uncommitted changes." % name, run.flow("verify --repo %s" % name) + " then commit.")
    branch = run.plan["branch"]
    git(repo, "push", "-u", "origin", branch)  # never forced
    rs["pushed_commit"] = head(repo)
    run.log("pushed", repo=name, commit=rs["pushed_commit"], branch=branch)
    say("OK pushed %s to origin/%s at %s." % (name, branch, rs["pushed_commit"]))


def pr_body(run, name):
    plan, s = run.plan, run.state
    rp, rs = run.repo_plan(name), s["repos"][name]
    names = merge_names(plan)
    units = rp["slices"] + ([rp["closing"]] if rp.get("closing") else [])
    L = ["## Summary", "", plan["what"], "", "Why: " + plan["why"]]
    if plan.get("not_included"):
        L += ["", "Not included:"] + ["- " + x for x in plan["not_included"]]
    L += ["", "## Changes", ""]
    for c in rs["commits"]:
        subj = c["subject"][len(run.card):].strip() if c["subject"].startswith(run.card) else c["subject"]
        L.append("- %s%s%s" % ("Fix: " if c.get("kind") == "fix" else "", subj, (". " + c["what"]) if c.get("what") else ""))
        if c.get("note"):
            L.append("  - Note: " + c["note"])
    decisions = [d for d in plan.get("decisions") or [] if d.get("repo") in (None, "", name)]
    if decisions:
        L += ["", "Decisions:"]
        L += ["- %s (%s)" % (d["text"], "decided by the developer" if d["by"] == "user" else "default")
              for d in decisions]
    L += ["", "## Testing and rollout", ""]
    for c in plan["criteria"]:
        if any(c["id"] in (u.get("proves") or []) for u in units):
            L.append("- %s %s. Proven at %s level: %s" % (c["id"], c["text"].rstrip("."), c["test"]["layer"], c["test"]["scenario"]))
    tests = []
    for u in units:
        for t in u.get("tests_to_add") or []:
            t = re.sub(r"\s*\(.*?\)\s*$", "", t)
            if t and t not in tests:
                tests.append(t)
    if tests:
        L.append("- Tests added or extended: " + ", ".join("`%s`" % t for t in tests))
    try:
        later = rs["suite"].get("tree") != head_tree(run.repo_path(name))
    except Refuse:
        later = False
    L.append("- Full suite: `%s`, %d tests passing locally%s" % (
        rp.get("full_suite") or "mvn verify", rs["suite"].get("passed", 0),
        ". Commits made after that run passed the slices' own checks" if later else ""))
    for t in tests_for(plan, name):
        L.append("- Existing test changed: `%s`. %s" % (t["test"], t["change"]))
    if len(names) > 1:
        L.append("- Merge order: %d of %d (%s)" % (names.index(name) + 1, len(names), ", then ".join(names)))
    if plan.get("rollout"):
        L += ["", "Rollout:"] + ["%d. %s" % (i + 1, step) for i, step in enumerate(plan["rollout"])]
    touches = []
    for c in rs["commits"]:
        for t in re.split(r"\s*,\s*", c.get("touches") or ""):
            if t and t not in touches:
                touches.append(t)
    L += ["", "## Touches", ""]
    if touches:
        L.append("- Names: " + ", ".join("`%s`" % t for t in touches))
    L.append("- Card: %s%s" % (run.card, (" " + s["url"]) if s.get("url") else ""))
    sib = ["%s %s" % (n, s["repos"][n]["pr"]["url"]) for n in names if n != name and s["repos"][n]["pr"]]
    if len(names) > 1:
        L.append("- Sibling PRs: " + ("; ".join(sib) if sib else "added when they are opened"))
    return "\n".join(L) + "\n"


def cmd_pr_body(run, a):
    name = pick_repo(run, a.repo, lambda n, rs: not rs["pr"] or rs["pr"].get("stale"), "a PR description")
    rs = run.state["repos"][name]
    repo = run.repo_path(name)
    if not rs["pushed_commit"]:
        raise Refuse("%s has not been pushed yet." % name, run.flow("push " + name))
    run.work_dir().mkdir(exist_ok=True)
    path = run.work_dir() / ("pr-%s.md" % name)
    title = "%s %s" % (run.card, run.plan["title"])
    body = pr_body(run, name)
    covered = rs.get("pr_body_commits", 0)
    rs["pr_body_commits"] = len(rs["commits"])
    if rs["pr"] and path.is_file():
        # the PR is open and the file may hold hand edits: leave it, and say what to add
        fresh = run.work_dir() / ("pr-%s.generated.md" % name)
        fresh.write_text(body, encoding="utf-8")
        rs["pr"]["stale"] = False
        say("OK the PR for %s is already open: %s" % (name, rs["pr"]["url"]),
            "  Your description file is untouched: %s" % path)
        new = rs["commits"][covered:]
        if new:
            say("  Add these under Changes, in your own words:")
            for c in new:
                subj = c["subject"][len(run.card):].strip() if c["subject"].startswith(run.card) else c["subject"]
                say("    - %s%s" % ("Fix: " if c.get("kind") == "fix" else "", subj))
        else:
            say("  No commits were added since the description was written.")
        say("  If the plan changed (criteria, decisions, merge order, rollout, tests), bring those lines up to date too.",
            "  A freshly generated description, to copy lines from: %s" % fresh,
            "  Then update the open PR with your file (gh pr edit --body-file, or your Bitbucket skill).",
            "  Leave the PR open as it is. Do not turn it back into a draft.")
        return
    existed = path.is_file()
    path.write_text(body, encoding="utf-8")
    rs["pr_body"] = True
    base, branch = run.repo_plan(name)["base"], run.plan["branch"]
    host, _ = remote_web(repo)
    say("OK PR description %s: %s" % ("rewritten" if existed else "written", path),
        "  The file is yours to edit. Tighten the wording, keep the four sections.",
        "  Title: " + title)
    if not rs.get("pr_stage_logged"):
        rs["pr_stage_logged"] = True
        run.log("stage", name="pr", state="start", repo=name)
    say("  From %s into %s. Open it as a draft." % (branch, base))
    if host == "github":
        say("  This repo is on GitHub. Run this inside the repo folder:",
            "    gh pr create --draft --base %s --head %s --title \"%s\" --body-file \"%s\"" % (base, branch, title, path))
    else:
        say("  This repo is not on GitHub. Use your Bitbucket skill to open a draft PR with that title and file.")


def cmd_pr_opened(run, a):
    name = pick_repo(run, a.repo, lambda n, rs: not rs["pr"] and rs["pushed_commit"], "a PR recorded")
    rs = run.state["repos"][name]
    if not rs["pushed_commit"]:
        raise Refuse("%s has not been pushed yet." % name, run.flow("push " + name))
    url = one_line(a.url)
    if not re.match(r"^https?://", url):
        raise Refuse("--url must be the PR's web address.")
    m = re.search(r"/(?:pull|pull-requests)/(\d+)", url)
    names = merge_names(run.plan)
    rs["pr"] = {"url": url, "id": "#" + m.group(1) if m else "", "ready": False}
    run.log("pr_opened", repo=name, id=rs["pr"]["id"], url=url, title=run.plan["title"], order=names.index(name) + 1)
    say("OK draft PR recorded for %s: %s" % (name, url))
    for other in names:
        opr = run.state["repos"][other]["pr"]
        if other != name and opr:
            say("  Add this new PR's link (%s) under Touches in the description of the %s PR (%s)." % (url, other, opr["url"]))


def cmd_pr_ready(run, a):
    name = pick_repo(run, a.repo, lambda n, rs: rs["pr"] and not rs["pr"].get("ready"), "to be marked ready")
    rs = run.state["repos"][name]
    if not rs["pr"]:
        raise Refuse("No PR is recorded for %s." % name, next_step(run)[0])
    repo = run.repo_path(name)
    if head(repo) != rs["pushed_commit"] or tree_now(repo) != head_tree(repo):
        raise Refuse("%s has commits or changes that are not pushed." % name, run.flow("push " + name))
    ci = one_line(a.ci_result)
    if not ci:
        raise Refuse("--ci-result is empty.", "Say which CI checks ran and passed. Never mark a PR ready on a red build.")
    rs["pr"]["ready"] = True
    rs["pr"]["was_ready"] = True
    rs["pr"]["ci"] = ci[:300]
    run.log("stage", name="pr", state="done", repo=name, ci=ci[:300])
    say("OK %s PR is ready for review." % name)


def finished_prs(s):
    return [{"repo": n, "id": s["repos"][n]["pr"]["id"], "url": s["repos"][n]["pr"]["url"], "order": i + 1}
            for i, n in enumerate(merge_names(s["plan"]))]


def cmd_finish(run, a):
    s = run.state
    if s["phase"] == "done":
        raise Refuse("The run is already finished.", next_step(run)[1])
    if not s.get("plan") or not s.get("approved"):
        raise Refuse("There is no approved plan.", next_step(run)[0])
    for rp in s["plan"]["repos"]:
        rs = s["repos"][rp["repo"]]
        if rs["stage"] != "built" or not rs["pr"] or not rs["pr"].get("ready"):
            raise Refuse("%s does not have a PR that is ready for review." % rp["repo"], next_step(run)[0])
    s["phase"] = "done"
    prs = finished_prs(s)
    run.log("run_end", prs=prs)
    say("OK run finished. Tell the user:")
    for p in prs:
        say("  %s %s%s" % (p["repo"], p["url"], (", merge %s" % ("first" if p["order"] == 1 else "after the one above"))
                           if len(prs) > 1 else ""))
    say("  Each PR needs their review, and an approval from someone else, before it merges.")
    left = [(n, t) for n, rs in s["repos"].items() for t in rs.get("review_left") or []]
    if left:
        say("  Review findings left as they are:")
        for n, t in left:
            say("    - %s%s" % ((n + ": ") if len(s["repos"]) > 1 else "", t))
    if s.get("noticed"):
        say("  Noticed outside this card and left alone:")
        for i, n in enumerate(s["noticed"]):
            say("    %d. %s" % (i + 1, n))


# ---------------------------------------------------------------- commands: anytime

def cmd_learn(run, a):
    if a.kind not in LEARN_KINDS:
        raise Refuse("--kind must be one of: " + ", ".join(LEARN_KINDS))
    run.log("learning", kind=a.kind, scope=a.scope or "", text=run.limited("learning", a.text, HEADLINE_MAX * 2))
    say("OK recorded.")


def cmd_brief(run, a):
    s = run.state
    if not s.get("plan"):
        raise Refuse("There is no plan yet.", next_step(run)[-1])
    plan = s["plan"]
    name = a.repo or next((r["repo"] for r in plan["repos"] if s["repos"][r["repo"]]["stage"] != "built"),
                          plan["repos"][0]["repo"])
    rp, rs = run.repo_plan(name), s["repos"][name]
    say("BRIEF %s %s, repo %s%s" % (run.card, plan["title"], name,
                                    " (every repo is built; all are listed below)" if len(plan["repos"]) > 1 and not a.repo and all(
                                        s["repos"][r["repo"]]["stage"] == "built" for r in plan["repos"]) else ""),
        "  What: " + plan["what"], "  Why: " + plan["why"])
    for x in plan.get("not_included") or []:
        say("  Not included: " + x)
    say("  Branch %s from %s. Repo folder: %s" % (plan["branch"], rp["base"], run.workspace() / rp.get("path", name)))
    if rs["stage"] != "todo":
        say("  Git: " + repo_summary(run, name))
    for c in plan["criteria"]:
        say("  %s: %s" % (c["id"], c["text"]))
    for d in plan.get("decisions") or []:
        say("  Decision (%s): %s" % ("the user's" if d["by"] == "user" else "default", d["text"]))
    con = plan.get("contract")
    if isinstance(con, dict):
        if con.get("summary"):
            say("  Contract: " + con["summary"])
        for it in con.get("items") or []:
            say("    %s (%s): %s" % (it.get("name", ""), it.get("kind", ""), it.get("change", "")))
    for t in tests_for(plan, name):
        say("  Existing test expected to change: %s. %s" % (t["test"], t["change"]))
    for other in plan["repos"]:
        h = s["repos"][other["repo"]].get("handoff")
        if h:
            say("  Handoff from %s: %s" % (other["repo"], h))
    all_built = all(s["repos"][r["repo"]]["stage"] == "built" for r in plan["repos"])
    for orp in (plan["repos"] if all_built and not a.repo else [rp]):
        ors = s["repos"][orp["repo"]]
        tag = (orp["repo"] + " ") if len(plan["repos"]) > 1 else ""
        for i, spec in enumerate(orp["slices"]):
            say("  [%s] %sslice %d: %s. Done when: %s" % (ors["slices"][i]["state"], tag, i + 1, spec["title"], spec["done_when"]))
        say("  [%s] %sself-check" % ("done" if ors["selfcheck"]["done"] else "in progress" if ors["selfcheck"]["started"] else "todo", tag))
        if orp.get("closing"):
            say("  [%s] %sclosing slice: %s" % (ors["closing"]["state"], tag, orp["closing"]["title"]))
        if needs_review(plan):
            orv = review_of(ors)
            say("  [%s] %sreview" % ("done" if orv["done"] else "in progress" if orv["started"] else "todo", tag))
        if ors["pr"]:
            say("  %sPR: %s" % (tag, ors["pr"]["url"]))
    for ch in s.get("changes") or []:
        say("  Plan change: %s (%s)" % (ch["text"], ch["approval"]))
    for q in open_questions(s):
        say("  Open question %d: %s" % (q["n"], q["question"]))
    for c in s.get("change_requests") or []:
        say("  Change request %d (%s): %s" % (c["id"], "open" if c.get("open") else "closed", c["text"]))
    for i, n in enumerate(s.get("noticed") or []):
        say("  Noticed outside the card %d: %s" % (i + 1, n))
    say("  Full plan: %s" % run.plan_path())


def open_questions(s):
    return s.get("questions") or []


def repo_summary(run, name):
    """One line on a repo's git state, for someone picking the run up cold."""
    try:
        repo = run.repo_path(name)
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        changed = [l for l in git(repo, "status", "--porcelain", "--", ".", *EXCLUDES, keep_indent=True).splitlines() if l.strip()]
        return "on %s at %s, %s" % (branch, head(repo), ("%d uncommitted file%s: %s" % (
            len(changed), "" if len(changed) == 1 else "s", ", ".join(c[3:] for c in changed[:4]) +
            (" ..." if len(changed) > 4 else ""))) if changed else "working tree clean")
    except Refuse as r:
        return "git state unknown (%s)" % r.why[:80]


def open_change(s):
    return next((c for c in s.get("change_requests") or [] if c.get("open")), None)


def merged_repos(run):
    """Repos whose pushed feature commits are already in the base branch. Best effort: a squash merge is not seen."""
    out = []
    for rp in run.plan["repos"]:
        rs = run.state["repos"][rp["repo"]]
        if not rs.get("pushed_commit"):
            continue
        try:
            repo = run.repo_path(rp["repo"])
            git(repo, "fetch", "origin", rp["base"], check=False)
            ahead = git(repo, "rev-list", "--count", "origin/%s..%s" % (rp["base"], rs["pushed_commit"]), check=False)
            if ahead == "0":
                out.append(rp["repo"])
        except Refuse:
            pass
    return out


def cmd_change_request(run, a):
    s = run.state
    if not s.get("approved"):
        raise Refuse("The plan is not approved yet. Before approval, change the plan with plan-submit.", next_step(run)[0])
    reqs = s.setdefault("change_requests", [])
    if a.withdrawn:
        cr = open_change(s)
        if not cr:
            raise Refuse("No change request is open.", next_step(run)[0])
        cr["open"] = False
        run.log("change_withdrawn", id=cr["id"], text=one_line(a.withdrawn))
        say("OK change request %d withdrawn. Nothing in the plan changed." % cr["id"])
        if cr.get("after_finish") and all(rs["stage"] == "built" and rs["pr"] and rs["pr"].get("ready")
                                          and not rs["pr"].get("stale") for rs in s["repos"].values()):
            s["phase"] = "done"
            run.log("run_end", prs=finished_prs(s), again=True)
        return
    if not a.text:
        raise Refuse("--text is missing.", run.flow("change-request --text '<what they asked>'"))
    merged = merged_repos(run)
    if merged:
        raise Refuse("The feature branch of %s is already merged into its base branch." % ", ".join(merged),
                     "Stop and tell the user. A change after the merge needs a new branch, so it is a new card or a "
                     "new run, not a change to this one.")
    text = run.limited("change request", a.text, QUESTION_MAX)
    entry = {"id": len(reqs) + 1, "text": text, "t": now_iso(), "after_finish": s["phase"] == "done", "open": True}
    reqs.append(entry)
    run.log("change_request", id=entry["id"], text=text, after_finish=entry["after_finish"])
    s["phase"] = "active"
    say("OK change request %d recorded. Nothing else has changed yet." % entry["id"])
    prs = ["%s %s" % (n, rs["pr"]["url"]) for n, rs in s["repos"].items() if rs["pr"]]
    if prs:
        say("  PRs are open (%s)." % "; ".join(prs),
            "  Checked the remotes: no feature branch has been merged into its base. A squash merge would not show,",
            "  so if you have not looked at the PRs themselves, look now. If one is merged, stop and tell the user.")


def cmd_noticed(run, a):
    s = run.state
    if a.resolved is not None:
        items = s.get("noticed") or []
        if not 1 <= a.resolved <= len(items):
            raise Refuse("There is no noticed item %s." % a.resolved,
                         "Items: " + ("; ".join("%d: %s" % (i + 1, t[:60]) for i, t in enumerate(items)) or "none"))
        gone = items.pop(a.resolved - 1)
        run.log("finding_resolved", text=gone)
        say("OK removed from the list: " + gone)
        return
    if not a.text:
        items = s.get("noticed") or []
        say("Noticed outside this card, %d item%s:" % (len(items), "" if len(items) == 1 else "s"))
        for i, n in enumerate(items):
            say("  %d. %s" % (i + 1, n))
        say("  Add one: " + run.flow("noticed --text '<what you noticed>'"),
            "  Remove one that was fixed or is no longer true: " + run.flow("noticed --resolved <number>"))
        return
    text = run.limited("note", a.text, QUESTION_MAX)
    s.setdefault("noticed", []).append(text)
    run.log("finding", severity="note", caught_by="the agent, outside this card", text=text, outcome="Left alone. Reported to you at the end.")
    say("OK noted. It will be listed for the user when the run finishes. Do not fix it in this card.")


def cmd_monitor(run, a):
    page = run.dir / "monitor.html"
    src = Path(__file__).resolve().parent / "monitor.html"
    if src.is_file():
        page.write_bytes(src.read_bytes())
    if not page.is_file():
        raise Refuse("The monitor page is not installed with this skill.")
    run.state["monitor_shown"] = True
    say("OK monitor page: %s" % page)
    if a.no_open or os.environ.get("BUILD_FLOW_NO_OPEN"):
        say("  Not opened (opening is switched off here). Tell the user they can open that file in a browser.")
        return
    try:
        import webbrowser
        opened = webbrowser.open(page.as_uri())
    except Exception:
        opened = False
    if opened:
        say("  Opened in the default browser. It updates on its own. Tell the user it is open.")
    else:
        say("  Could not open a browser. Tell the user to open that file in a browser by hand.")


def global_instruction_files():
    found = []
    roots = [os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA"), str(Path.home() / ".config"),
             str(Path.home() / "Library" / "Application Support")]
    for root in roots:
        if root:
            p = Path(root) / "github-copilot" / "intellij" / "global-copilot-instructions.md"
            if p.is_file() and p not in found:
                found.append(p)
    return found


def pick_repo(run, given, wants, what):
    """The repo a finish-phase command applies to: the one named, or the first that needs the step."""
    if not run.plan or not run.state.get("approved"):
        raise Refuse("The plan is not approved yet.", next_step(run)[0])
    names = [rp["repo"] for rp in run.plan["repos"]]
    if given:
        run.repo_plan(given)
        return given
    for name in names:
        if wants(name, run.state["repos"][name]):
            return name
    raise Refuse("No repo needs %s right now." % what, next_step(run)[0])


# ---------------------------------------------------------------- entry

def build_parser():
    p = argparse.ArgumentParser(prog="flow", description="Build Flow script " + VERSION)
    p.add_argument("card", help="Jira card key, for example ABC-123")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    def cmd(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    def commit_args(sp):
        sp.add_argument("--subject", required=True)
        sp.add_argument("--why")
        sp.add_argument("--what")
        sp.add_argument("--touches")
        sp.add_argument("--note")
        sp.add_argument("--mechanical", action="store_true")
        sp.add_argument("--repo")

    sp = cmd("start", cmd_start, "start a run for a card")
    sp.add_argument("--title", required=True)
    sp.add_argument("--url")
    sp.add_argument("--workspace")
    cmd("status", cmd_status, "where the run is and what comes next")
    sp = cmd("now", cmd_now, "one line on what you are doing")
    sp.add_argument("text")
    sp = cmd("ask", cmd_ask, "record a question for the user")
    sp.add_argument("--question", required=True)
    sp.add_argument("--recommend", required=True)
    sp.add_argument("--found")
    sp.add_argument("--options")
    sp = cmd("answered", cmd_answered, "record the user's answer to the open questions")
    sp.add_argument("--user-said", required=True)
    sp.add_argument("--question", type=int, help="close only this question number")
    sp = cmd("change-request", cmd_change_request, "record a change the user asked for after approval")
    sp.add_argument("--text")
    sp.add_argument("--withdrawn", help="the user dropped the open change request: their words")
    sp = cmd("noticed", cmd_noticed, "record a problem outside this card, to report at the end")
    sp.add_argument("--text")
    sp.add_argument("--resolved", type=int, help="remove item N from the list: it was fixed or is no longer true")
    sp = cmd("monitor", cmd_monitor, "open the monitor page")
    sp.add_argument("--no-open", action="store_true")
    sp = cmd("plan-submit", cmd_plan_submit, "save a draft plan")
    sp.add_argument("--file", required=True)
    sp = cmd("plan-reviewed", cmd_plan_reviewed, "record the plan reviewer's issues and what happened to each")
    sp.add_argument("--file", required=True)
    sp = cmd("agent-start", cmd_agent_start, "record a subagent you are launching (scouts, verifiers)")
    sp.add_argument("--role", required=True)
    sp.add_argument("--task", required=True)
    sp.add_argument("--why")
    sp = cmd("agent-done", cmd_agent_done, "record what a subagent found")
    sp.add_argument("n", type=int)
    sp.add_argument("--takeaway", required=True)
    sp = cmd("plan-approve", cmd_plan_approve, "record the user's approval")
    sp.add_argument("--user-said", required=True)
    sp = cmd("plan-revise", cmd_plan_revise, "change the plan after approval")
    sp.add_argument("--file", required=True)
    sp.add_argument("--summary", required=True)
    sp.add_argument("--user-said")
    sp = cmd("repo-start", cmd_repo_start, "create or switch to the feature branch")
    sp.add_argument("repo")
    sp = cmd("slice-start", cmd_slice_start, "start the next slice")
    sp.add_argument("--repo")
    sp = cmd("check", cmd_check, "run the checks of the slice in progress")
    sp.add_argument("--repo")
    commit_args(cmd("slice-done", cmd_slice_done, "commit the verified slice"))
    sp = cmd("selfcheck-start", cmd_selfcheck_start, "start the self-check")
    sp.add_argument("--repo")
    sp = cmd("selfcheck-report", cmd_selfcheck_report, "record the self-check result")
    sp.add_argument("--file", required=True)
    sp.add_argument("--repo")
    sp = cmd("verify", cmd_verify, "rerun the targeted tests after a fix")
    sp.add_argument("--repo")
    sp.add_argument("--full", action="store_true", help="run the full suite instead of the slices' own checks")
    commit_args(cmd("commit", cmd_commit, "commit a verified fix"))
    sp = cmd("review-start", cmd_review_start, "start the review of a repo")
    sp.add_argument("--repo")
    sp = cmd("review-plan", cmd_review_plan, "record which review axes run and why")
    sp.add_argument("--file", required=True)
    sp.add_argument("--repo")
    sp = cmd("review-report", cmd_review_report, "record the review's findings and what happened to each")
    sp.add_argument("--file", required=True)
    sp.add_argument("--repo")
    sp = cmd("suite", cmd_suite, "run the full suite")
    sp.add_argument("--repo")
    sp = cmd("repo-done", cmd_repo_done, "close a built and verified repo")
    sp.add_argument("--handoff")
    sp.add_argument("--repo")
    sp = cmd("push", cmd_push, "push the feature branch")
    sp.add_argument("repo", nargs="?")
    sp = cmd("pr-body", cmd_pr_body, "write the PR description")
    sp.add_argument("repo", nargs="?")
    sp = cmd("pr-opened", cmd_pr_opened, "record the opened draft PR")
    sp.add_argument("repo", nargs="?")
    sp.add_argument("--url", required=True)
    sp = cmd("pr-ready", cmd_pr_ready, "record that CI passed and the PR is ready")
    sp.add_argument("repo", nargs="?")
    sp.add_argument("--ci-result", required=True)
    cmd("finish", cmd_finish, "end the run")
    sp = cmd("learn", cmd_learn, "record a learning")
    sp.add_argument("--kind", required=True)
    sp.add_argument("--text", required=True)
    sp.add_argument("--scope")
    sp = cmd("brief", cmd_brief, "the brief for one repo")
    sp.add_argument("repo", nargs="?")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    card = args.card.upper()
    if not CARD_RE.match(card):
        say("REFUSED: '%s' is not a Jira card key." % args.card, "DO: use the form ABC-123.")
        return 2
    EXCLUDES[:] = [":(exclude,top).build-flow", ":(exclude,top)%s.plan.md" % card]
    run = Run(card)
    rc = 0
    try:
        if args.cmd == "status" and not run.exists():
            say("No run exists yet for %s." % card,
                "NEXT: Read the card. Then, from the workspace folder: flow %s start --title '<card title>' --url '<card link>'" % card)
            return 0
        if args.cmd != "start":
            if not run.exists():
                raise Refuse("No run exists for %s." % card,
                             "flow %s start --title '<card title>' --url '<card link>'   (run it from the workspace folder)" % card)
            run.load()
        rc = args.fn(run, args) or 0
        run.save()
        if rc != 2 and not getattr(args, "quiet", False):
            lines = next_step(run)
            if (args.cmd == "start" and not run.state.get("monitor_shown")
                    and (Path(__file__).resolve().parent / "monitor.html").is_file()):
                lines = [run.flow("monitor") + "   (opens the page where the user follows the run)"]
            for i, line in enumerate(lines):
                say(("NEXT: " if i == 0 else "      ") + line)
    except Refuse as r:
        say("REFUSED: " + r.why)
        if r.do:
            say("DO: " + r.do)
        rc = 2
    finally:
        run.write_run_data()
    return rc


if __name__ == "__main__":
    sys.exit(main())

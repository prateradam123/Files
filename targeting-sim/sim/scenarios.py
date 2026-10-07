"""Remediation scenarios. Each one answers: does this (repo, branch) need the change?

A scenario declares
  policy      which branches are eligible (default / long_lived / release / active)
  cls         signal class, used to analyse which approach wins for which kind of change
  select()    which files a checker must read, given the branch's full path listing
  guess       exact paths someone would probe without listing the tree
  evaluate()  the real check (same code for ground truth and every strategy)
  search      code-search queries a careful engineer would run
  naive       the first query most people (or a freehand agent) would type
  langs       repo languages it can apply to (None = any)
"""
import re
import fnmatch
from dataclasses import dataclass, field
from typing import Callable

BOOT_SNAKE = {"2.7.18": "1.30", "3.1.5": "1.33", "3.2.4": "2.2", "3.3.1": "2.2"}


def V(s):
    s = (s or "").strip().lstrip("^~=v")
    out = []
    for p in s.split("."):
        m = re.match(r"\d+", p)
        out.append(int(m.group()) if m else 0)
    return tuple(out + [0] * (4 - len(out)))


def flat_yaml(text):
    """Tiny indentation-based YAML flattener -> {dotted.key: value}."""
    out, stack = {}, []
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#") or raw.strip().startswith("- "):
            continue
        ind = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        while stack and stack[-1][0] >= ind:
            stack.pop()
        key = ".".join([s[1] for s in stack] + [k.strip()])
        v = v.strip()
        if v:
            out[key] = v
        else:
            stack.append((ind, k.strip()))
    return out


def pom_props(text):
    m = re.search(r"<properties>(.*?)</properties>", text, re.S)
    return dict(re.findall(r"<([\w.\-]+)>([^<]*)</\1>", m.group(1))) if m else {}


def pom_deps(text, props):
    out = []
    for g, a, v in re.findall(r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>\s*(?:<version>([^<]+)</version>)?", text):
        if v.startswith("${"):
            v = props.get(v[2:-1], "")
        out.append((g, a, v))
    return out


def pom_parent(text):
    m = re.search(r"<parent>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>\s*<version>([^<]+)</version>", text)
    return m.groups() if m else None


def gradle_deps(files):
    """(group, artifact, version) from build.gradle literals and libs.versions.toml."""
    out = []
    for p, t in files.items():
        if p.endswith("build.gradle"):
            out += [tuple(x) for x in re.findall(r"'([\w.\-]+):([\w.\-]+):([\w.\-]+)'", t)]
        if p.endswith("libs.versions.toml"):
            vers = dict(re.findall(r'^(\w+) = "([^"]+)"', t, re.M))
            for g, a, ref in re.findall(r'module = "([\w.\-]+):([\w.\-]+)", version\.ref = "(\w+)"', t):
                out.append((g, a, vers.get(ref, "")))
    return out


def gradle_boot(files):
    for p, t in files.items():
        if p.endswith("build.gradle"):
            m = re.search(r'org\.springframework\.boot" version "([\d.]+)"', t)
            if m:
                return m.group(1)
    return None


def build_files(paths):
    return [p for p in paths if p.endswith("pom.xml") or p.endswith("build.gradle") or p.endswith("libs.versions.toml")]


def java_declared_deps(files):
    poms = {p: t for p, t in files.items() if p.endswith("pom.xml")}
    deps = []
    for p, t in poms.items():
        deps += pom_deps(t, pom_props(t))
    deps += gradle_deps(files)
    return deps


class Resolver:
    """Knowledge that lives outside the repo being checked: internal libs at tags (must be
    fetched from the SCM) and public facts (Spring Boot's managed snakeyaml)."""

    def __init__(self):
        self.starter_snake, self.parent_snake, self.parent_boot = {}, {}, {}

    def load(self, read):  # read(repo, tag, path) -> text
        for tag in ["v3.1.0", "v3.2.0", "v3.4.0", "v4.0.1"]:
            t = read("acme-kafka-starter", tag, "pom.xml")
            self.starter_snake[tag[1:]] = re.search(r"<artifactId>snakeyaml</artifactId>\s*<version>([^<]+)", t).group(1)
        for tag in ["v5.0.0", "v5.1.0", "v5.2.0"]:
            t = read("acme-parent", tag, "pom.xml")
            self.parent_snake[tag[1:]] = pom_props(t)["snakeyaml.version"]
            self.parent_boot[tag[1:]] = pom_parent(t)[2]
        return self


def effective_snakeyaml(files, R):
    poms = {p: t for p, t in files.items() if p.endswith("pom.xml")}
    if poms:
        props, managed, direct = {}, None, None
        for t in poms.values():
            props.update(pom_props(t))
        for t in poms.values():
            par = pom_parent(t)
            if par and par[1] == "spring-boot-starter-parent":
                managed = BOOT_SNAKE.get(par[2])
            elif par and par[1] == "acme-parent":
                managed = R.parent_snake.get(par[2])
        if "snakeyaml.version" in props:
            managed = props["snakeyaml.version"]
        for t in poms.values():
            for g, a, v in pom_deps(t, props):
                if a == "snakeyaml" and v:
                    direct = v
        return direct or managed
    if any(p.endswith("build.gradle") for p in files):
        cands = []
        b = gradle_boot(files)
        if b: cands.append(BOOT_SNAKE.get(b))
        for g, a, v in gradle_deps(files):
            if a == "snakeyaml": cands.append(v)
            if a == "acme-kafka-starter": cands.append(R.starter_snake.get(v))
        cands = [c for c in cands if c]
        return max(cands, key=V) if cands else None
    return None


def java_version(files):
    for p, t in files.items():
        if p.endswith("pom.xml"):
            m = re.search(r"<(?:java\.version|maven\.compiler\.release)>(\d+)<", t)
            if m: return int(m.group(1))
        if p.endswith("build.gradle"):
            m = re.search(r"JavaLanguageVersion\.of\((\d+)\)", t)
            if m: return int(m.group(1))
    return None


def boot_version(files, R):
    for p, t in files.items():
        if p.endswith("pom.xml"):
            par = pom_parent(t)
            if par and par[1] == "spring-boot-starter-parent": return par[2]
            if par and par[1] == "acme-parent": return R.parent_boot.get(par[2])
    return gradle_boot(files)


def is_main_src(p):
    return "src/main/" in p


def strip_java_comments(t):
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    return re.sub(r"//[^\n]*", "", t)


@dataclass
class Scenario:
    key: str
    title: str
    policy: str
    cls: str
    select: Callable
    evaluate: Callable
    search: list
    naive: tuple
    guess: list = field(default_factory=list)
    langs: tuple = None
    needs_resolver: bool = False


def sel_glob(*pats):
    return lambda paths: [p for p in paths if any(fnmatch.fnmatch(p, g) for g in pats)]


def yml_files(paths):
    return [p for p in paths if re.search(r"application[\w\-]*\.(ya?ml|properties)$", p)]


S = []

S.append(Scenario(
    "S01", "snakeyaml < 2.0 on the classpath (direct, property, parent-managed, transitive)", "active", "resolution",
    build_files, lambda f, R: (lambda v: v is not None and V(v) < V("2.0"))(effective_snakeyaml(f, R)),
    search=[("snakeyaml", {}), ("spring-boot-starter-parent", {}), ("acme-parent", {}), ("org.springframework.boot", {"filename": "build.gradle"})],
    naive=("snakeyaml", {}), guess=["pom.xml", "service/pom.xml", "build.gradle", "gradle/libs.versions.toml"],
    langs=("Java",), needs_resolver=True))

S.append(Scenario(
    "S02", "log4j-core < 2.17.1 declared anywhere (incl. nested modules)", "active", "manifest",
    build_files, lambda f, R: any(a == "log4j-core" and v and V(v) < V("2.17.1") for g, a, v in java_declared_deps(f)),
    search=[("log4j-core", {})], naive=("log4j-core", {}),
    guess=["pom.xml", "service/pom.xml", "build.gradle", "gradle/libs.versions.toml"], langs=("Java",)))

S.append(Scenario(
    "S03", "Java version below 21", "long_lived", "manifest",
    build_files, lambda f, R: (lambda j: j is not None and j < 21)(java_version(f)),
    search=[("java.version", {}), ("maven.compiler.release", {}), ("JavaLanguageVersion", {})], naive=("java.version", {}),
    guess=["pom.xml", "service/pom.xml", "build.gradle"], langs=("Java",)))


def legacy_key(f, R):
    for p, t in f.items():
        if p.endswith(".properties") and "acme.kafka.consumer.legacy-retry" in t: return True
        if re.search(r"\.ya?ml$", p) and "acme.kafka.consumer.legacy-retry" in flat_yaml(t): return True
    return False


S.append(Scenario(
    "S04", "Deprecated config key acme.kafka.consumer.legacy-retry (nested YAML, flat, .properties, env-only)", "active", "config",
    yml_files, legacy_key, search=[("legacy-retry", {})], naive=("acme.kafka.consumer.legacy-retry", {}),
    guess=["src/main/resources/application.yml", "service/src/main/resources/application.yml"], langs=("Java",)))

S.append(Scenario(
    "S05", "Deprecated class LegacyRetryTemplate used in production code (not tests/comments/docs)", "active", "code",
    lambda paths: [p for p in paths if p.endswith(".java") and is_main_src(p)],
    lambda f, R: any("LegacyRetryTemplate" in strip_java_comments(t) for t in f.values()),
    search=[("LegacyRetryTemplate", {})], naive=("LegacyRetryTemplate", {}), guess=[], langs=("Java",)))

S.append(Scenario(
    "S06", "Kafka topic contract.status.v1 referenced by production config or code", "active", "code",
    lambda paths: [p for p in paths if is_main_src(p) and (p.endswith(".java") or re.search(r"\.(ya?ml|properties)$", p))],
    lambda f, R: any("contract.status.v1" in (strip_java_comments(t) if p.endswith(".java") else t) for p, t in f.items()),
    search=[("contract.status.v1", {})], naive=("contract.status.v1", {}), guess=[], langs=("Java",)))


def listener_no_dlq(f, R):
    has_listener = any("@KafkaListener" in strip_java_comments(t) for p, t in f.items() if p.endswith(".java"))
    dlq = any(flat_yaml(t).get("acme.kafka.dead-letter.enabled") == "true" for p, t in f.items() if re.search(r"\.ya?ml$", p))
    return has_listener and not dlq


S.append(Scenario(
    "S07", "Has a @KafkaListener but no dead-letter config (cross-file, absence)", "default", "cross-file",
    lambda paths: [p for p in paths if is_main_src(p) and (p.endswith(".java") or re.search(r"application[\w\-]*\.ya?ml$", p))],
    listener_no_dlq, search=[("KafkaListener", {})], naive=("KafkaListener", {}), guess=[], langs=("Java",)))

S.append(Scenario(
    "S08", "Dockerfile on eclipse-temurin:17 (any Dockerfile path)", "release", "manifest",
    lambda paths: [p for p in paths if p.split("/")[-1] == "Dockerfile"],
    lambda f, R: any(re.search(r"^FROM eclipse-temurin:17", t, re.M) for t in f.values()),
    search=[("eclipse-temurin 17", {"filename": "Dockerfile"})], naive=("eclipse-temurin:17", {}), guess=["Dockerfile"]))

S.append(Scenario(
    "S09", "CI workflow pinned to actions/checkout@v3", "default", "manifest",
    sel_glob(".github/workflows/*.yml"),
    lambda f, R: any("actions/checkout@v3" in t for t in f.values()),
    search=[("actions/checkout v3", {"path_contains": ".github/workflows"})], naive=("actions/checkout@v3", {}),
    guess=[".github/workflows/ci.yml"]))

S.append(Scenario(
    "S10", "Terraform ECS module pinned below v2", "release", "manifest",
    sel_glob("*.tf", "*/*.tf", "*/*/*.tf"),
    lambda f, R: any(re.search(r"terraform-ecs-service\.git\?ref=v1\.", t) for t in f.values()),
    search=[("terraform-ecs-service", {"extension": "tf"})], naive=("terraform-ecs-service", {}),
    guess=["main.tf", "envs/prod/main.tf"], langs=("HCL",)))


def axios_vuln(f, R):
    for p, t in f.items():
        if p.endswith("package-lock.json"):
            for m in re.finditer(r'"node_modules/axios": \{\s*"version": "([^"]+)"', t):
                if V(m.group(1)) < V("1.6.0"): return True
        if p.endswith("package.json"):
            m = re.search(r'"axios": "([^"]+)"', t)
            if m and V(m.group(1)) < V("1.6.0"): return True
    return False


S.append(Scenario(
    "S11", "axios < 1.6.0 (direct or transitive via lockfile; lockfiles often > 384 KB)", "active", "manifest",
    sel_glob("package.json", "package-lock.json"), axios_vuln,
    search=[("axios", {})], naive=("axios", {}), guess=["package.json", "package-lock.json"], langs=("JavaScript",)))

S.append(Scenario(
    "S12", "Deprecated endpoint /v1/contracts/legacy-status in resources (incl. huge OpenAPI specs)", "active", "code",
    lambda paths: [p for p in paths if is_main_src(p) and "/resources/" in p],
    lambda f, R: any("/v1/contracts/legacy-status" in t for t in f.values()),
    search=[("legacy-status", {})], naive=("/v1/contracts/legacy-status", {}), guess=[], langs=("Java",)))

S.append(Scenario(
    "S13", "jackson-databind < 2.15 on supported release branches", "release", "manifest",
    build_files, lambda f, R: any(a == "jackson-databind" and v and V(v) < V("2.15") for g, a, v in java_declared_deps(f)),
    search=[("jackson-databind", {})], naive=("jackson-databind", {}),
    guess=["pom.xml", "service/pom.xml", "build.gradle", "gradle/libs.versions.toml"], langs=("Java",)))

S.append(Scenario(
    "S14", "Spring Boot < 3.2 (direct parent, Gradle plugin, or via internal acme-parent)", "long_lived", "resolution",
    build_files, lambda f, R: (lambda b: b is not None and V(b) < V("3.2"))(boot_version(f, R)),
    search=[("spring-boot-starter-parent", {}), ("acme-parent", {}), ("org.springframework.boot", {"filename": "build.gradle"})],
    naive=("spring-boot-starter-parent", {}), guess=["pom.xml", "build.gradle"], langs=("Java",), needs_resolver=True))


def hardcoded_secret(f, R):
    for p, t in f.items():
        v = flat_yaml(t).get("acme.auth.client-secret")
        if v and not v.startswith("${"): return True
    return False


S.append(Scenario(
    "S15", "Hard-coded acme.auth.client-secret (mostly appears on in-flight branches)", "active", "config",
    yml_files, hardcoded_secret, search=[("client-secret", {})], naive=("client-secret", {}),
    guess=["src/main/resources/application.yml"], langs=("Java",)))

S.append(Scenario(
    "S16", "Internal acme-kafka-starter on 3.x (literal, property or version catalog)", "long_lived", "manifest",
    build_files, lambda f, R: any(a == "acme-kafka-starter" and v.startswith("3.") for g, a, v in java_declared_deps(f)),
    search=[("acme-kafka-starter", {})], naive=("acme-kafka-starter", {}),
    guess=["pom.xml", "service/pom.xml", "build.gradle", "gradle/libs.versions.toml"], langs=("Java",)))


def prod_missing_poll(f, R):
    base = [t for p, t in f.items() if re.search(r"application\.ya?ml$", p)]
    prod = [t for p, t in f.items() if re.search(r"application-prod\.ya?ml$", p)]
    uses = any("spring.kafka.consumer.group-id" in flat_yaml(t) for t in base)
    return uses and prod and not any("max.poll.interval.ms" in t for t in prod)


S.append(Scenario(
    "S17", "Kafka consumer whose application-prod.yml lacks max.poll.interval.ms (absence)", "default", "cross-file",
    yml_files, prod_missing_poll, search=[("group-id", {"filename": "application.yml"})], naive=("max.poll.interval.ms", {}),
    guess=["src/main/resources/application.yml", "src/main/resources/application-prod.yml"], langs=("Java",)))


def requests_vuln(f, R):
    for p, t in f.items():
        m = re.search(r"requests==([\d.]+)", t)
        if m and V(m.group(1)) < V("2.31"): return True
    return False


S.append(Scenario(
    "S18", "Python requests < 2.31 (requirements*.txt in any folder, or pyproject)", "active", "manifest",
    lambda paths: [p for p in paths if re.search(r"requirements[\w\-/]*\.txt$|requirements/.*\.txt$|pyproject\.toml$", p)],
    requests_vuln, search=[("requests", {})], naive=("requests", {}),
    guess=["requirements.txt", "pyproject.toml"], langs=("Python",)))

SCENARIOS = {s.key: s for s in S}

# Default-branch-only variants: the one case where code search *could* be enough on its own.
_s = SCENARIOS
S.append(Scenario(
    "S19", "Default branch only: deprecated endpoint in OpenAPI specs (some specs > 384 KB)", "default", "code",
    _s["S12"].select, _s["S12"].evaluate, search=_s["S12"].search, naive=_s["S12"].naive, langs=("Java",)))
S.append(Scenario(
    "S20", "Default branch only: axios < 1.6.0 incl. lockfile-only (big lockfiles)", "default", "manifest",
    _s["S11"].select, _s["S11"].evaluate, search=_s["S11"].search, naive=_s["S11"].naive,
    guess=_s["S11"].guess, langs=("JavaScript",)))
S.append(Scenario(
    "S21", "Default branch only: LegacyRetryTemplate in production code (some repos pushed minutes ago)", "default", "code",
    _s["S05"].select, _s["S05"].evaluate, search=_s["S05"].search, naive=_s["S05"].naive, langs=("Java",)))
SCENARIOS = {s.key: s for s in S}

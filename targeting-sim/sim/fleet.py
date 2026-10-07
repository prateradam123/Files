"""Generate a synthetic fleet of real git repositories (bare remotes) that look like an
enterprise Java/Spring shop plus some Node, Python and Terraform repos.

Every repo gets a branching model (gitflow, per-stream develops, trunk), a history whose
older states feed release branches, stale and merged branches, archived repos and forks,
big binaries buried in history, huge files above code-search size limits, and an internal
framework (acme-kafka-starter / acme-parent) whose versions transitively decide whether a
service is vulnerable.

Usage: python -m sim.fleet --out fleets/f1 --repos 120 --seed 1
"""
import argparse, json, os, random, subprocess, time, copy, hashlib
from pathlib import Path

NOW = 1791331200  # 2026-10-07T00:00:00Z
DAY = 86400

STARTER_SNAKE = {"3.1.0": "1.30", "3.2.0": "1.33", "3.4.0": "2.0", "4.0.1": "2.2"}
PARENT_SNAKE = {"5.0.0": "1.33", "5.1.0": "1.33", "5.2.0": "2.2"}
PARENT_BOOT = {"5.0.0": "3.1.5", "5.1.0": "3.2.4", "5.2.0": "3.3.1"}


# ----------------------------------------------------------------------------- rendering
WORDS = ("contract vendor status amount ledger account party term clause rule decision event payload record "
         "request response mapper service handler config retry topic schedule notice balance rate fee period "
         "approval workflow audit snapshot draft signer document version policy region branch limit").split()


def lorem_java(pkg, name, rnd, body_lines=40):
    """Code-like filler with realistic entropy (compresses ~3-4x like real Java, not 20x)."""
    def ident():
        return rnd.choice(WORDS) + "".join(w.capitalize() for w in rnd.sample(WORDS, rnd.randint(1, 2)))
    lines = [f"package {pkg};", "", "import java.util.*;", "import java.time.*;", "", f"public class {name} {{"]
    for i in range(body_lines):
        k = rnd.random()
        if k < 0.3:
            lines.append(f"    private final {rnd.choice(['String','Long','BigDecimal','Instant','List<String>'])} {ident()};")
        elif k < 0.6:
            lines.append(f"    public {rnd.choice(['void','boolean','String'])} {ident()}({rnd.choice(['String','Long'])} {ident()}) {{ return {ident()}.{ident()}({rnd.randint(0, 9999)}); }}")
        elif k < 0.8:
            lines.append(f"    // {' '.join(rnd.choice(WORDS) for _ in range(rnd.randint(4, 12)))}")
        else:
            lines.append(f"        if ({ident()} > {rnd.randint(0, 500)} && {ident()}.isPresent()) {{ log.debug(\"{ident()} {{}}\", {ident()}); }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_pom_service(st, name, module=None):
    deps = []
    props = []
    if st["java_decl"] == "java.version":
        props.append(f"<java.version>{st['java']}</java.version>")
    else:
        props.append(f"<maven.compiler.release>{st['java']}</maven.compiler.release>")
    if st.get("starter"):
        if st["starter_decl"] == "property":
            props.append(f"<acme.platform.version>{st['starter']}</acme.platform.version>")
            v = "${acme.platform.version}"
        else:
            v = st["starter"]
        deps.append(("com.acme.platform", "acme-kafka-starter", v))
    if st.get("snake_direct"):
        if st["snake_decl"] == "property":
            props.append(f"<snakeyaml.version>{st['snake_direct']}</snakeyaml.version>")
            deps.append(("org.yaml", "snakeyaml", "${snakeyaml.version}"))
        else:
            deps.append(("org.yaml", "snakeyaml", st["snake_direct"]))
    elif st.get("snake_override"):
        props.append(f"<snakeyaml.version>{st['snake_override']}</snakeyaml.version>")
    if st.get("log4j") and (st.get("log4j_loc", "root") == "root" or module == "service"):
        if module is None or module == "service":
            deps.append(("org.apache.logging.log4j", "log4j-core", st["log4j"]))
    deps.append(("com.fasterxml.jackson.core", "jackson-databind", st["jackson"]))
    deps.append(("org.springframework.boot", "spring-boot-starter-web", None))
    if st["parent"] == "spring":
        parent = ("org.springframework.boot", "spring-boot-starter-parent", st["boot"])
    else:
        parent = ("com.acme.platform", "acme-parent", st["acme_parent"])
    dep_xml = "\n".join(
        f"        <dependency>\n            <groupId>{g}</groupId>\n            <artifactId>{a}</artifactId>\n"
        + (f"            <version>{v}</version>\n" if v else "")
        + "        </dependency>" for g, a, v in deps)
    prop_xml = "\n".join("        " + p for p in props)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>{parent[0]}</groupId>
        <artifactId>{parent[1]}</artifactId>
        <version>{parent[2]}</version>
    </parent>
    <groupId>com.acme</groupId>
    <artifactId>{name}{'-' + module if module else ''}</artifactId>
    <version>1.0.0-SNAPSHOT</version>
    <properties>
{prop_xml}
    </properties>
    <dependencies>
{dep_xml}
    </dependencies>
</project>
"""


def render_multi_root(st, name):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.acme</groupId>
    <artifactId>{name}-parent</artifactId>
    <version>1.0.0-SNAPSHOT</version>
    <packaging>pom</packaging>
    <modules>
        <module>api</module>
        <module>service</module>
        <module>client</module>
    </modules>
</project>
"""


def render_gradle(st, catalog):
    if catalog:
        lines = ['plugins { id "java"; id "org.springframework.boot" version "%s" }' % st["boot"], "",
                 "java { toolchain { languageVersion = JavaLanguageVersion.of(%s) } }" % st["java"], "",
                 "dependencies {"]
        if st.get("starter"): lines.append("    implementation libs.acme.kafka.starter")
        if st.get("snake_direct"): lines.append("    implementation libs.snakeyaml")
        if st.get("log4j"): lines.append("    implementation libs.log4j.core")
        lines.append("    implementation libs.jackson.databind")
        lines.append("}")
        return "\n".join(lines) + "\n"
    lines = ['plugins { id "java"; id "org.springframework.boot" version "%s" }' % st["boot"], "",
             "java { toolchain { languageVersion = JavaLanguageVersion.of(%s) } }" % st["java"], "",
             "dependencies {"]
    if st.get("starter"): lines.append(f"    implementation 'com.acme.platform:acme-kafka-starter:{st['starter']}'")
    if st.get("snake_direct"): lines.append(f"    implementation 'org.yaml:snakeyaml:{st['snake_direct']}'")
    if st.get("log4j"): lines.append(f"    implementation 'org.apache.logging.log4j:log4j-core:{st['log4j']}'")
    lines.append(f"    implementation 'com.fasterxml.jackson.core:jackson-databind:{st['jackson']}'")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_catalog(st):
    out = ["[versions]"]
    if st.get("starter"): out.append(f'acmeStarter = "{st["starter"]}"')
    if st.get("snake_direct"): out.append(f'snakeyaml = "{st["snake_direct"]}"')
    if st.get("log4j"): out.append(f'log4j = "{st["log4j"]}"')
    out.append(f'jackson = "{st["jackson"]}"')
    out.append("")
    out.append("[libraries]")
    if st.get("starter"): out.append('acme-kafka-starter = { module = "com.acme.platform:acme-kafka-starter", version.ref = "acmeStarter" }')
    if st.get("snake_direct"): out.append('snakeyaml = { module = "org.yaml:snakeyaml", version.ref = "snakeyaml" }')
    if st.get("log4j"): out.append('log4j-core = { module = "org.apache.logging.log4j:log4j-core", version.ref = "log4j" }')
    out.append('jackson-databind = { module = "com.fasterxml.jackson.core:jackson-databind", version.ref = "jackson" }')
    return "\n".join(out) + "\n"


def render_yaml(st, env=None):
    lines = ["spring:", "  application:", f"    name: {st['svc']}"]
    if st["kafka"]:
        lines += ["  kafka:", "    bootstrap-servers: ${KAFKA_BOOTSTRAP}", "    consumer:",
                  f"      group-id: {st['svc']}-group"]
        if env == "prod" and st.get("prod_max_poll"):
            lines += ["      properties:", "        max.poll.interval.ms: 600000"]
    acme = []
    if st["kafka"]:
        topics = list(st["topics"])
        if st.get("topic_loc") == "yaml" and "contract.status.v1" in st.get("legacy_topic", []):
            topics.append("contract.status.v1")
        acme += ["  kafka:", "    topics:"] + [f"      - {t}" for t in topics]
        if st.get("dlq"):
            acme += ["    dead-letter:", "      enabled: true", f"      topic: {st['svc']}.dlq"]
        lr = st.get("legacy_key")
        if lr == "nested" and env is None or lr == "env-only" and env == "dev":
            acme += ["    consumer:", "      legacy-retry: true"]
    if st.get("secret") and env is None:
        acme += ["  auth:", "    client-secret: s3cr3t-hardcoded-value"]
    if acme:
        lines += ["acme:"] + acme
    if st.get("legacy_key") == "flat" and env is None:
        lines += ["acme.kafka.consumer.legacy-retry: true"]
    return "\n".join(lines) + "\n"


def render_java(st, rnd, pkg):
    files = {}
    base = f"src/main/java/com/acme/{pkg}"
    listener = ""
    if st["kafka"]:
        topic_ref = "Topics.STATUS" if "contract.status.v1" in st.get("legacy_topic", []) and st.get("topic_loc") == "java" else '"${acme.kafka.topics[0]}"'
        listener = f"""    @KafkaListener(topics = {topic_ref})
    public void onMessage(String payload) {{
        log.info("received payload size={{}}", payload.length());
        service.handle(payload);
    }}
"""
    retry = ""
    if st.get("legacy_class") == "main":
        retry = "    private final LegacyRetryTemplate retryTemplate = new LegacyRetryTemplate(3);\n"
    elif st.get("legacy_class") == "comment-only":
        retry = "    // TODO: removed LegacyRetryTemplate in favour of DefaultErrorHandler\n"
    files[f"{base}/messaging/InboundConsumer.java"] = f"""package com.acme.{pkg}.messaging;

import com.acme.platform.kafka.*;
import org.springframework.kafka.annotation.KafkaListener;

public class InboundConsumer {{
{retry}{listener}}}
"""
    if st["kafka"] and "contract.status.v1" in st.get("legacy_topic", []) and st.get("topic_loc") == "java":
        files[f"{base}/messaging/Topics.java"] = f"""package com.acme.{pkg}.messaging;

public final class Topics {{
    public static final String STATUS = "contract.status.v1";
}}
"""
    if st.get("legacy_class") == "test-only":
        files[f"src/test/java/com/acme/{pkg}/RetryFixture.java"] = f"""package com.acme.{pkg};

class RetryFixture {{
    // exercises behaviour formerly provided by LegacyRetryTemplate
    Object template = new com.acme.platform.kafka.LegacyRetryTemplate(1);
}}
"""
    for i in range(st["filler"]):
        r2 = random.Random(f"{pkg}-{i}")  # stable across branches: only real changes differ
        files[f"{base}/domain/Model{i}.java"] = lorem_java(f"com.acme.{pkg}.domain", f"Model{i}", r2, r2.randint(20, 120))
    return files


def render_openapi(st, rnd, big):
    paths = []
    n = 5200 if big else 30  # big specs ~570 KB: above GitHub's 384 KB and Bitbucket's 512 KiB search limits
    for i in range(n):
        paths.append(f'    "/v2/contracts/{{id}}/item{i}": {{"get": {{"operationId": "getItem{i}", "summary": "Fetches item {i} for a contract record with all details"}}}}')
    if st.get("legacy_endpoint"):
        pos = rnd.randint(int(n * 0.6), n - 1) if big else rnd.randint(0, n - 1)
        paths.insert(pos, '    "/v1/contracts/legacy-status": {"get": {"operationId": "legacyStatus", "deprecated": true}}')
    return '{\n  "openapi": "3.0.1",\n  "paths": {\n' + ",\n".join(paths) + "\n  }\n}\n"


def render_java_service(st, rnd, name, kind):
    files = {}
    pkg = name.replace("-", "")
    if kind == "maven":
        files["pom.xml"] = render_pom_service(st, name)
    elif kind == "maven-multi":
        files["pom.xml"] = render_multi_root(st, name)
        for m in ("api", "service", "client"):
            files[f"{m}/pom.xml"] = render_pom_service(st, name, module=m) if m == "service" else render_pom_service({**st, "snake_direct": None, "log4j": None, "starter": None}, name, module=m)
    elif kind in ("gradle", "gradle-catalog"):
        files["build.gradle"] = render_gradle(st, kind == "gradle-catalog")
        files["settings.gradle"] = f"rootProject.name = '{name}'\n"
        if kind == "gradle-catalog":
            files["gradle/libs.versions.toml"] = render_catalog(st)
    prefix = "service/" if kind == "maven-multi" else ""
    files[prefix + "src/main/resources/application.yml"] = render_yaml(st)
    files[prefix + "src/main/resources/application-dev.yml"] = render_yaml(st, "dev")
    files[prefix + "src/main/resources/application-prod.yml"] = render_yaml(st, "prod")
    if st.get("legacy_key") == "properties":
        files[prefix + "src/main/resources/application.properties"] = "acme.kafka.consumer.legacy-retry=true\nserver.port=8080\n"
    for p, c in render_java(st, rnd, pkg).items():
        files[prefix + p] = c
    dpath = st.get("docker_path", "Dockerfile")
    files[dpath] = f"FROM {st['docker']}\nCOPY target/app.jar /app/app.jar\nENTRYPOINT [\"java\",\"-jar\",\"/app/app.jar\"]\n"
    for i in range(st["workflows"]):
        wf = ["ci", "release", "sonar"][i]
        files[f".github/workflows/{wf}.yml"] = f"""name: {wf}
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{st['checkout'] if i == 0 else st.get('checkout2', st['checkout'])}
      - uses: actions/setup-java@v4
        with:
          java-version: '{st['java']}'
      - run: mvn -B verify
"""
    if st.get("heavy_assets"):
        r3 = random.Random(name + "-assets")
        for i in range(st["heavy_assets"]):
            files[f"src/test/resources/fixtures/sample-{i}.pdf"] = r3.randbytes(r3.randint(1, 6) * 1024 * 1024)
    if st.get("openapi"):
        files[prefix + "src/main/resources/openapi/vendor-api.json"] = render_openapi(st, rnd, st["openapi"] == "big")
    readme = f"# {name}\n\nService owned by team {st['team']}.\n"
    if st.get("legacy_class") == "docs-only":
        readme += "\nMigration note: LegacyRetryTemplate was replaced by DefaultErrorHandler in 2025.\n"
    if st.get("docs_topic"):
        readme += "\nHistorically published to contract.status.v1 (retired).\n"
    files["README.md"] = readme
    return files


def render_node(st, rnd, name):
    files = {}
    deps = {"express": "^4.18.2"}
    if st.get("axios_direct"):
        deps["axios"] = st["axios_direct"]
    if st.get("axios_transitive"):
        deps["@acme/http-client"] = "^2.1.0"
    files["package.json"] = json.dumps({"name": name, "version": "1.0.0", "dependencies": deps}, indent=2) + "\n"
    pkgs = {"": {"name": name, "dependencies": deps}}
    if st.get("axios_direct"):
        pkgs["node_modules/axios"] = {"version": st["axios_direct"].lstrip("^~")}
    if st.get("axios_transitive"):
        pkgs["node_modules/@acme/http-client"] = {"version": "2.1.0", "dependencies": {"axios": st["axios_transitive"]}}
        pkgs["node_modules/axios"] = {"version": st["axios_transitive"]}
    for i in range(st["lock_pkgs"]):
        pkgs[f"node_modules/pkg-{i}-{rnd.randint(0, 9999)}"] = {"version": f"{rnd.randint(0, 9)}.{rnd.randint(0, 20)}.{rnd.randint(0, 30)}",
                                                                "resolved": f"https://registry.npmjs.org/pkg-{i}/-/pkg-{i}.tgz",
                                                                "integrity": "sha512-" + hashlib.sha512(str(i).encode()).hexdigest()[:86]}
    files["package-lock.json"] = json.dumps({"name": name, "lockfileVersion": 3, "packages": pkgs}, indent=2) + "\n"
    files["Dockerfile"] = f"FROM {st['docker']}\nCOPY . /app\nCMD [\"node\",\"server.js\"]\n"
    files["server.js"] = "const express = require('express');\nconst app = express();\napp.listen(3000);\n"
    files[".github/workflows/ci.yml"] = f"name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@{st['checkout']}\n      - run: npm ci && npm test\n"
    files["README.md"] = f"# {name}\n"
    return files


def render_python(st, rnd, name):
    files = {}
    reqs = ["flask==3.0.0", "boto3==1.34.0"]
    if st.get("requests"):
        reqs.append(f"requests=={st['requests']}")
    loc = st.get("req_loc", "requirements.txt")
    if loc == "pyproject.toml":
        files["pyproject.toml"] = "[project]\nname = \"%s\"\ndependencies = [\n%s\n]\n" % (name, ",\n".join(f'  "{r}"' for r in reqs))
    else:
        files[loc] = "\n".join(reqs) + "\n"
    files["app/main.py"] = "import flask\napp = flask.Flask(__name__)\n"
    files["Dockerfile"] = "FROM python:3.11-slim\nCOPY . /app\n"
    files[".github/workflows/ci.yml"] = f"name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@{st['checkout']}\n      - run: pytest\n"
    files["README.md"] = f"# {name}\n"
    return files


def render_terraform(st, rnd, name):
    files = {}
    for i, env in enumerate(st["envs"]):
        ref = st["tf_ref"] if i == 0 else st.get("tf_ref2", st["tf_ref"])
        files[f"envs/{env}/main.tf"] = f"""module "ecs_service" {{
  source = "git::https://github.com/acme/terraform-ecs-service.git?ref={ref}"
  name   = "{name}-{env}"
  cpu    = 512
}}
"""
    files[".github/workflows/plan.yml"] = f"name: plan\non: [pull_request]\njobs:\n  plan:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@{st['checkout']}\n      - run: terraform plan\n"
    files["README.md"] = f"# {name}\n"
    return files


def render_lib(name, version):
    if name == "acme-kafka-starter":
        snake = STARTER_SNAKE[version]
        pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project>
    <groupId>com.acme.platform</groupId>
    <artifactId>acme-kafka-starter</artifactId>
    <version>{version}</version>
    <dependencies>
        <dependency>
            <groupId>org.yaml</groupId>
            <artifactId>snakeyaml</artifactId>
            <version>{snake}</version>
        </dependency>
    </dependencies>
</project>
"""
        return {"pom.xml": pom, "src/main/java/com/acme/platform/kafka/LegacyRetryTemplate.java":
                "package com.acme.platform.kafka;\n@Deprecated\npublic class LegacyRetryTemplate { public LegacyRetryTemplate(int n) {} }\n",
                "README.md": "# acme-kafka-starter\n"}
    snake, boot = PARENT_SNAKE[version], PARENT_BOOT[version]
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>{boot}</version>
    </parent>
    <groupId>com.acme.platform</groupId>
    <artifactId>acme-parent</artifactId>
    <version>{version}</version>
    <packaging>pom</packaging>
    <properties>
        <snakeyaml.version>{snake}</snakeyaml.version>
    </properties>
</project>
"""
    return {"pom.xml": pom, "README.md": "# acme-parent\n"}


# ----------------------------------------------------------------------------- state model
JAVA_KINDS = ["maven"] * 40 + ["maven-multi"] * 12 + ["gradle"] * 10 + ["gradle-catalog"] * 8


def java_state(rnd, name):
    parent = rnd.choice(["spring", "spring", "acme"])
    st = dict(svc=name, team=rnd.choice(["contracts", "vendor", "servicing", "platform", "risk"]),
              parent=parent, boot=rnd.choice(["2.7.18", "3.1.5", "3.2.4", "3.3.1", "3.3.1"]),
              acme_parent=rnd.choice(list(PARENT_SNAKE)), java=rnd.choice(["11", "17", "17", "21", "21", "21"]),
              java_decl=rnd.choice(["java.version", "maven.compiler.release"]),
              starter=rnd.choice([None, "3.1.0", "3.2.0", "3.4.0", "4.0.1", "4.0.1"]),
              starter_decl=rnd.choice(["literal", "property"]),
              snake_direct=rnd.choice([None] * 5 + ["1.33", "2.2"]), snake_decl=rnd.choice(["literal", "property"]),
              snake_override=None, log4j=rnd.choice([None] * 5 + ["2.14.1", "2.17.2"]),
              log4j_loc=rnd.choice(["root", "module"]), jackson=rnd.choice(["2.13.4", "2.15.3", "2.17.1"]),
              kafka=rnd.random() < 0.7, topics=[f"{name}.events", "contract.verification.result"],
              legacy_topic=["contract.status.v1"] if rnd.random() < 0.15 else [],
              topic_loc=rnd.choice(["yaml", "java"]), dlq=rnd.random() < 0.6, prod_max_poll=rnd.random() < 0.6,
              legacy_key=rnd.choice([None] * 6 + ["nested", "nested", "flat", "properties", "env-only"]),
              legacy_class=rnd.choice([None] * 6 + ["main", "main", "test-only", "comment-only", "docs-only"]),
              docker=rnd.choice(["eclipse-temurin:17-jre", "eclipse-temurin:21-jre", "eclipse-temurin:21-jre", "amazoncorretto:21"]),
              docker_path=rnd.choice(["Dockerfile"] * 4 + ["docker/Dockerfile"]),
              checkout=rnd.choice(["v3", "v4", "v4"]), workflows=rnd.randint(1, 3),
              openapi=rnd.choice([None] * 6 + ["small", "big"]), legacy_endpoint=False,
              docs_topic=rnd.random() < 0.1, secret=False, heavy_assets=rnd.choice([0] * 6 + [1, 3, 6]), filler=min(1500, int(rnd.lognormvariate(5.3, 0.7))))
    if st["openapi"]:
        st["legacy_endpoint"] = rnd.random() < 0.5
    if parent == "acme" and rnd.random() < 0.3:
        st["snake_override"] = rnd.choice(["2.2", "1.33"])
    st["checkout2"] = rnd.choice(["v3", "v4"])
    return st


def older(st, rnd):
    """A plausible earlier state of the same repo (feeds release branches)."""
    o = copy.deepcopy(st)
    if rnd.random() < 0.5 and o.get("starter") in ("3.4.0", "4.0.1"): o["starter"] = rnd.choice(["3.1.0", "3.2.0"])
    if rnd.random() < 0.35 and not o.get("log4j") and not st.get("node"): o["log4j"] = "2.14.1"
    if rnd.random() < 0.5: o["jackson"] = "2.13.4"
    if rnd.random() < 0.4 and "java" in o: o["java"] = "17"
    if rnd.random() < 0.4: o["checkout"] = "v3"
    if rnd.random() < 0.4 and "docker" in o and "temurin" in o["docker"]: o["docker"] = "eclipse-temurin:17-jre"
    if rnd.random() < 0.3 and o.get("legacy_class") is None: o["legacy_class"] = "main"
    if "tf_ref" in o and rnd.random() < 0.5: o["tf_ref"] = rnd.choice(["v1.4.0", "v1.9.2"])
    if "requests" in o and rnd.random() < 0.5: o["requests"] = "2.28.1"
    if "axios_direct" in o and o.get("axios_direct") and rnd.random() < 0.5: o["axios_direct"] = "^0.27.2"
    return o


def mutate_dev(st, rnd):
    """Work in flight on develop / streams / features: sometimes fixes, sometimes regressions."""
    o = copy.deepcopy(st)
    roll = rnd.random()
    if "java" in o:
        if roll < 0.15: o["legacy_class"] = "main"
        elif roll < 0.3: o["legacy_key"] = "nested"
        elif roll < 0.4: o["starter"] = "4.0.1"
        elif roll < 0.5: o["log4j"] = "2.14.1"
        elif roll < 0.6: o["secret"] = True
        elif roll < 0.68: o["legacy_topic"] = ["contract.status.v1"]; o["kafka"] = True
        elif roll < 0.75: o["legacy_endpoint"] = True; o["openapi"] = o.get("openapi") or "small"
        elif roll < 0.82: o["java"] = "21"
        elif roll < 0.9: o["snake_direct"] = "1.33"
        else: o["dlq"] = not o.get("dlq")
        o["filler"] = o["filler"] + 1
    elif "axios_direct" in o or "axios_transitive" in o:
        if roll < 0.5: o["axios_direct"] = "^0.27.2"
        else: o["axios_direct"] = "^1.7.2"
    elif "requests" in o:
        o["requests"] = rnd.choice(["2.28.1", "2.32.3"])
    elif "tf_ref" in o:
        o["tf_ref"] = rnd.choice(["v1.9.2", "v2.1.0"])
    o["_touch"] = rnd.random()
    return o


def modernize(st, rnd):
    o = copy.deepcopy(st)
    if rnd.random() < 0.3 and "jackson" in o: o["jackson"] = "2.17.1"
    if rnd.random() < 0.3: o["checkout"] = "v4"
    return o


# ----------------------------------------------------------------------------- git writer
class FastImport:
    def __init__(self):
        self.buf = bytearray()
        self.mark = 0
        self.blob_marks = {}

    def _m(self):
        self.mark += 1
        return self.mark

    def blob(self, data: bytes):
        h = hashlib.sha1(data).hexdigest()
        if h in self.blob_marks:
            return self.blob_marks[h]
        m = self._m()
        self.buf += b"blob\nmark :%d\ndata %d\n" % (m, len(data)) + data + b"\n"
        self.blob_marks[h] = m
        return m

    def commit(self, ref, files, when, msg, parent=None, merge=None):
        marks = {p: self.blob(c if isinstance(c, bytes) else c.encode()) for p, c in files.items()}
        m = self._m()
        msgb = msg.encode()
        self.buf += b"commit %s\nmark :%d\ncommitter Dev <dev@acme.com> %d +0000\ndata %d\n%s\n" % (
            ref.encode(), m, when, len(msgb), msgb)
        if parent:
            self.buf += b"from :%d\n" % parent
        if merge:
            self.buf += b"merge :%d\n" % merge
        self.buf += b"deleteall\n"
        for p, bm in marks.items():
            self.buf += b"M 100644 :%d %s\n" % (bm, p.encode())
        self.buf += b"\n"
        return m

    def reset(self, ref, mark):
        self.buf += b"reset %s\nfrom :%d\n\n" % (ref.encode(), mark)

    def run(self, repo_dir):
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(repo_dir)], check=True)
        subprocess.run(["git", "fast-import", "--quiet"], input=bytes(self.buf), cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "uploadpack.allowFilter", "true"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "uploadpack.allowAnySHA1InWant", "true"], cwd=repo_dir, check=True)
        subprocess.run(["git", "gc", "-q", "--prune=now"], cwd=repo_dir, check=True)


# ----------------------------------------------------------------------------- repo builder
def build_repo(out, name, kind, rnd, meta_extra):
    fi = FastImport()
    if kind in JAVA_KINDS or kind.startswith("maven") or kind.startswith("gradle"):
        st = java_state(rnd, name)
        render = lambda s: render_java_service(s, rnd_r(s), name, kind)
    elif kind == "node":
        st = dict(node=True, axios_direct=rnd.choice([None, "^0.27.2", "^1.7.2", "^1.7.2"]),
                  axios_transitive=rnd.choice([None, None, "0.26.1", "1.6.8"]), docker=rnd.choice(["node:18-alpine", "node:20-alpine"]),
                  checkout=rnd.choice(["v3", "v4"]), lock_pkgs=rnd.choice([40, 120, 3000]))
        render = lambda s: render_node(s, rnd_r(s), name)
    elif kind == "python":
        st = dict(requests=rnd.choice([None, "2.28.1", "2.31.0", "2.32.3"]),
                  req_loc=rnd.choice(["requirements.txt", "requirements/prod.txt", "pyproject.toml"]),
                  checkout=rnd.choice(["v3", "v4"]))
        render = lambda s: render_python(s, rnd_r(s), name)
    elif kind == "terraform":
        st = dict(tf_ref=rnd.choice(["v1.4.0", "v1.9.2", "v2.1.0", "v2.1.0"]), envs=["dev", "qa", "prod"][: rnd.randint(1, 3)],
                  checkout=rnd.choice(["v3", "v4"]))
        if rnd.random() < 0.3: st["tf_ref2"] = rnd.choice(["v1.9.2", "v2.1.0"])
        render = lambda s: render_terraform(s, rnd_r(s), name)
    else:
        raise ValueError(kind)

    # history on main: oldest -> newest
    n_hist = rnd.randint(3, 6)
    states = [older(older(st, rnd), rnd)]
    for _ in range(n_hist - 2):
        states.append(modernize(states[-1], rnd) if rnd.random() < 0.5 else older(st, rnd))
    states.append(st)
    last_main_age = rnd.choice([0.2, 1, 3, 9, 20, 45, 120, 300])  # days
    start = NOW - int(last_main_age * DAY) - 30 * DAY * (len(states) - 1)
    marks = []
    big_blob = rnd.random() < 0.18
    churn = rnd.choice([5, 15, 30, 60, 120])  # extra commits between states (history weight)
    for i, s in enumerate(states):
        t = start + 30 * DAY * i
        files = render(s)
        if big_blob and i == 1:
            files["assets/legacy-dump.bin"] = os.urandom(rnd.randint(6, 14) * 1024 * 1024)
        m = fi.commit("refs/heads/main", files, t, f"main change {i}", marks[-1][0] if marks else None)
        if i < len(states) - 1:
            fillers = [p for p in files if "/domain/Model" in p or p.endswith(".java")] or [p for p in files if isinstance(files[p], str)]
            for c in range(churn // max(1, len(states) - 1)):
                cf = dict(files)
                for p in rnd.sample(fillers, min(len(fillers), rnd.randint(2, 8))):
                    cf[p] = cf[p] + f"// churn {c} {rnd.getrandbits(64):x}\n" * rnd.randint(1, 30)
                m = fi.commit("refs/heads/main", cf, t + (c + 1) * 3600, f"churn {c}", m)
        marks.append((m, s, t))
    index_lag = False
    if last_main_age < 0.5 and rnd.random() < 0.6:
        # very recent default-branch push: code search index still reflects previous commit
        index_lag = True
    main_tip, main_state, _ = marks[-1]

    model = rnd.choices(["gitflow", "streams", "trunk"], [0.6, 0.15, 0.25])[0]
    branches = {}
    if model in ("gitflow", "streams"):
        dev_state = mutate_dev(main_state, rnd)
        dev_age = rnd.choice([0.5, 2, 6, 15, 40, 200])
        dm = fi.commit("refs/heads/develop", render(dev_state), NOW - int(dev_age * DAY), "develop work", main_tip)
        branches["develop"] = (dm, dev_state)
        if model == "streams":
            for k in range(rnd.randint(2, 4)):
                stream = rnd.choice(["payments", "q4", "vendor-x", "fraud", "ui-refresh", "reg-2027"]) + f"{k}"
                ss = mutate_dev(dev_state, rnd)
                age = rnd.choice([1, 4, 12, 35, 160])
                sm = fi.commit(f"refs/heads/develop-{stream}", render(ss), NOW - int(age * DAY), "stream work", dm)
                branches[f"develop-{stream}"] = (sm, ss)
        if rnd.random() < 0.75 and len(marks) >= 3:
            nrel = rnd.randint(1, 3)
            for r in range(nrel):
                base_m, base_s, base_t = marks[-(nrel - r) - 1] if len(marks) > nrel - r else marks[0]
                rel_name = f"release/{1 + r}.{rnd.randint(0, 9)}"
                if rnd.random() < 0.5:
                    hot = copy.deepcopy(base_s); hot["_hotfix"] = r
                    rm = fi.commit(f"refs/heads/{rel_name}", render(hot), base_t + 5 * DAY, "hotfix", base_m)
                    branches[rel_name] = (rm, hot)
                else:
                    fi.reset(f"refs/heads/{rel_name}", base_m)
                    branches[rel_name] = (base_m, base_s)
        feat_base = branches["develop"]
    else:
        feat_base = (main_tip, main_state)
    for f in range(rnd.randint(0, 5)):
        kindf = rnd.choices(["active", "stale", "merged"], [0.55, 0.3, 0.15])[0]
        fname = f"feature/ACME-{rnd.randint(100, 9999)}-{rnd.choice(['retry', 'topic', 'dlq', 'upgrade', 'vendor', 'audit'])}"
        if kindf == "merged":
            fi.reset(f"refs/heads/{fname}", feat_base[0])
            branches[fname] = (feat_base[0], feat_base[1])
            continue
        fs = mutate_dev(feat_base[1], rnd)
        age = rnd.choice([0.3, 2, 6, 14, 25]) if kindf == "active" else rnd.choice([150, 240, 400])
        fm = fi.commit(f"refs/heads/{fname}", render(fs), NOW - int(age * DAY), "feature work", feat_base[0])
        branches[fname] = (fm, fs)

    for k in range(rnd.choice([0, 0, 3, 10, 25, 60])):
        bm, bs, bt = rnd.choice(marks[:-1] or marks)
        old = copy.deepcopy(bs); old["_stale"] = k
        sm = fi.commit(f"refs/heads/feature/OLD-{k:03d}", render(old), bt + 2 * DAY, "old work", bm)
        branches[f"feature/OLD-{k:03d}"] = (sm, old)
    if index_lag:
        # add a fresh commit on main that introduces things, so index (main~1) is stale
        fresh = mutate_dev(main_state, rnd)
        fm = fi.commit("refs/heads/main", render(fresh), NOW - 3 * 3600, "fresh change", main_tip)
    repo_dir = out / "remotes" / f"{name}.git"
    fi.run(repo_dir)
    meta = dict(name=name, kind=kind, branching=model, default_branch="main", index_lag=index_lag,
                big_history=big_blob, **meta_extra)
    return meta


_rr_cache = {}
def rnd_r(s):
    # deterministic per-state RNG so re-rendering the same state yields same files
    key = json.dumps(s, sort_keys=True, default=str)
    return random.Random(hashlib.md5(key.encode()).hexdigest())


def build_lib(out, name, versions):
    fi = FastImport()
    prev = None
    t = NOW - 600 * DAY
    for v in versions:
        prev = fi.commit("refs/heads/main", render_lib(name, v), t, f"release {v}", prev)
        fi.reset(f"refs/tags/v{v}", prev)
        t += 120 * DAY
    repo_dir = out / "remotes" / f"{name}.git"
    fi.run(repo_dir)
    return dict(name=name, kind="internal-lib", branching="trunk", default_branch="main", index_lag=False,
                big_history=False, archived=False, fork=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--repos", type=int, default=120)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    out = Path(a.out)
    (out / "remotes").mkdir(parents=True, exist_ok=True)
    rnd = random.Random(a.seed)
    metas = [build_lib(out, "acme-kafka-starter", list(STARTER_SNAKE)), build_lib(out, "acme-parent", list(PARENT_SNAKE))]
    mix = (["java"] * 66 + ["node"] * 12 + ["python"] * 9 + ["terraform"] * 9)
    t0 = time.time()
    for i in range(a.repos - 2):
        fam = rnd.choice(mix)
        kind = rnd.choice(JAVA_KINDS) if fam == "java" else fam
        name = f"{rnd.choice(['contract','vendor','servicing','verification','dispatch','ledger','portal','risk','docs','pricing'])}-{fam if fam != 'java' else 'svc'}-{i:03d}"
        archived = rnd.random() < 0.05
        fork = (not archived) and rnd.random() < 0.03
        metas.append(build_repo(out, name, kind, random.Random(rnd.random()), dict(archived=archived, fork=fork)))
    (out / "fleet.json").write_text(json.dumps({"now": NOW, "repos": metas}, indent=1))
    print(f"built {len(metas)} repos in {time.time() - t0:.1f}s -> {out}")


if __name__ == "__main__":
    main()

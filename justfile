mod modal
mod terraform
mod platform
mod runners

default:
    @just --list

# Update agentrunner chart and image version across platform, toolchains, and README
bump-agentrunner version:
    #!/usr/bin/env bash
    set -euo pipefail
    ver="{{version}}"
    ver="${ver#v}"
    if [[ ! "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
        echo "Error: invalid version '${ver}' (expected e.g. 0.0.6 or v0.0.6)" >&2
        exit 1
    fi
    AGENTRUNNER_VER="$ver" python3 - <<'PY'
    import glob
    import os
    import re
    import sys

    ver = os.environ["AGENTRUNNER_VER"]
    targets = [
        ("platform/justfile", r'(agentrunner-install version=")[^"]+(")', rf"\g<1>{ver}\g<2>"),
        ("platform/agentrunner/values.yaml", r'(ghcr\.io/wrossmorrow/agentrunner-github-auth:)[^\s"]+', rf"\g<1>{ver}"),
        ("README.md", r"(agentrunner-(?:github-auth|golang):)[^\s)]+", rf"\g<1>{ver}"),
    ]
    for path in sorted(glob.glob("runners/toolchains/*.yaml")):
        targets.append((path, r'(ghcr\.io/wrossmorrow/agentrunner-[a-z0-9-]+:)[^\s"]+', rf"\g<1>{ver}"))

    if not any(path.startswith("runners/toolchains/") for path, _, _ in targets):
        sys.exit("Error: no runners/toolchains/*.yaml files found")

    for path, pattern, repl in targets:
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
        updated, count = re.subn(pattern, repl, original)
        if count == 0:
            sys.exit(f"Error: pattern {pattern!r} matched nothing in {path}")
        if updated != original:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(updated)
        print(f"Updated {path} ({count} match{'es' if count != 1 else ''}) -> {ver}")
    PY
    just platform check
    just runners check

# Bump agentrunner version, validate manifests, and roll out controller + toolchains
upgrade-agentrunner version:
    just bump-agentrunner {{version}}
    just platform agentrunner-install
    just runners apply-toolchains
    kubectl rollout status deployment/agentrunner-controller -n agentrunner-system --timeout=2m

# Update k8s-agents chart and sandbox image version across platform files
bump-k8s-agents version:
    #!/usr/bin/env bash
    set -euo pipefail
    ver="{{version}}"
    ver="${ver#v}"
    if [[ ! "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
        echo "Error: invalid version '${ver}' (expected e.g. 0.0.8 or v0.0.8)" >&2
        exit 1
    fi
    K8S_AGENTS_VER="$ver" python3 - <<'PY'
    import os
    import re
    import sys

    ver = os.environ["K8S_AGENTS_VER"]
    targets = [
        ("platform/justfile", r'(k8s-agents-install version=")[^"]+(")', rf"\g<1>{ver}\g<2>"),
        ("platform/k8s-agents/values.yaml", r'(imageTag:\s*)"?[^\s"]+"?', rf'\g<1>"{ver}"'),
    ]

    for path, pattern, repl in targets:
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
        updated, count = re.subn(pattern, repl, original)
        if count == 0:
            sys.exit(f"Error: pattern {pattern!r} matched nothing in {path}")
        if updated != original:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(updated)
        print(f"Updated {path} ({count} match{'es' if count != 1 else ''}) -> {ver}")
    PY
    just platform check

# Bump k8s-agents version, validate manifests, and roll out session-hub
upgrade-k8s-agents version:
    just bump-k8s-agents {{version}}
    just platform k8s-agents-install


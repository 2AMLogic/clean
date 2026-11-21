#!/bin/bash
# Run the public preview against a disposable project and compare its contents.
set -eu
script="$(cd "$(dirname "$0")/.." && pwd)/cleanup.sh"
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/home" "$fixture/projects/example" "$fixture/bin"
pyc="$fixture/projects/example/stale file.pyc"
dd if=/dev/zero of="$pyc" bs=1024 count=16 2>/dev/null
touch -t 200001010000 "$pyc"
cp -Rp "$fixture/projects" "$fixture/before"
# Limit discovery to OS tools. Never query the user's Docker, npm or gh.
printf '#!/bin/sh\nexit 1\n' > "$fixture/bin/brew"
chmod +x "$fixture/bin/brew"
env HOME="$fixture/home" CLEAN_PROJECT_DIR="$fixture/projects" PATH="$fixture/bin:/usr/bin:/bin" \
    /bin/bash "$script" --check --aggressive > "$fixture/result"
diff -r "$fixture/before" "$fixture/projects"
grep -F "[stray .pyc] $pyc" "$fixture/result"
grep -E 'Potential space savings: [1-9][0-9]*[KMG]' "$fixture/result"

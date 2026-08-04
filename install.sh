#!/bin/sh
# secagent developer install (macOS + Linux, no root). See docs/installation.md for
# the full quickstart -- this script only gets `secagent` (and, optionally, `pi`) onto
# PATH; it does not configure either. After it finishes:
#
#   secagent init --domain <your-suite-domain>
#   secagent login
set -eu
: "${HOME:?HOME must be set}"

# Pinned versions -- bump deliberately, never silently:
SECAGENT_REF="${SECAGENT_REF:-v0.2.1}"
PI_VERSION="${PI_VERSION:-0.83.0}"
# Matches the pi version pi/extensions/secrouter-auth.ts was last verified against
# (see that file's own header) -- bump both together.
SECAGENT_GIT_URL="${SECAGENT_GIT_URL:-https://github.com/secrouter/secagent}"

info() { printf '%s\n' "$*"; }
warn() { printf 'secagent-install: WARNING: %s\n' "$*" >&2; }
die() { printf 'secagent-install: %s\n' "$*" >&2; exit 1; }

# ── 1. OS/arch ───────────────────────────────────────────────────────────────────
os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
    Darwin | Linux) ;;
    *) die "unsupported OS: $os (this installer supports macOS and Linux only)" ;;
esac
info "secagent-install: detected $os/$arch"

# ── 2. Python >=3.11 (detect only -- this script never installs Python itself) ─────
python_bin=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
        continue
    fi
    ver="$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null)" \
        || continue
    major="${ver%%.*}"
    minor="${ver#*.}"
    case "$major" in '' | *[!0-9]*) continue ;; esac
    case "$minor" in '' | *[!0-9]*) continue ;; esac
    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
        python_bin="$candidate"
        break
    fi
done
if [ -z "$python_bin" ]; then
    case "$os" in
        Darwin)
            die "Python 3.11+ not found. Install it, e.g.:
  brew install python@3.11
Then re-run this script." ;;
        *)
            die "Python 3.11+ not found. Install it with your distro's package manager, e.g.:
  Debian/Ubuntu:  sudo apt install python3.11
  Fedora/RHEL:    sudo dnf install python3.11
Then re-run this script." ;;
    esac
fi
info "secagent-install: using $python_bin ($("$python_bin" --version 2>&1))"

# ── 3. uv (installs/runs secagent's own venv) ───────────────────────────────────────
if command -v uv >/dev/null 2>&1; then
    info "secagent-install: uv already installed ($(uv --version))"
elif command -v curl >/dev/null 2>&1; then
    info "secagent-install: installing uv via the official installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
elif command -v wget >/dev/null 2>&1; then
    info "secagent-install: installing uv via the official installer..."
    wget -qO- https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
elif command -v pipx >/dev/null 2>&1; then
    warn "no curl/wget found; falling back to: pipx install uv"
    pipx install uv
    export PATH="$HOME/.local/bin:$PATH"
elif "$python_bin" -m pip --version >/dev/null 2>&1; then
    warn "no curl/wget/pipx found; falling back to: $python_bin -m pip install --user uv"
    "$python_bin" -m pip install --user uv
    export PATH="$HOME/.local/bin:$PATH"
else
    die "could not install uv -- install it yourself: https://docs.astral.sh/uv/"
fi
command -v uv >/dev/null 2>&1 \
    || die "uv install attempted but 'uv' is still not on PATH -- open a new shell and re-run this script"
info "secagent-install: uv ready ($(uv --version))"

# ── 4. secagent, from the pinned ref, onto PATH via `uv tool install` ──────────────
info "secagent-install: installing secagent@$SECAGENT_REF via uv tool install..."
uv tool install --python "$python_bin" --force "git+${SECAGENT_GIT_URL}@${SECAGENT_REF}"
command -v secagent >/dev/null 2>&1 \
    || die "secagent installed but is not on PATH -- open a new shell and re-run this script"
info "secagent-install: secagent ready ($(secagent version 2>&1))"

# ── 5. pi (optional agent runtime) ──────────────────────────────────────────────────
if command -v npm >/dev/null 2>&1; then
    info "secagent-install: installing pi@$PI_VERSION via npm..."
    if npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}"; then
        info "secagent-install: pi installed"
    else
        warn "npm install of pi failed -- continuing without it (pi is optional; see docs/installation.md)"
    fi
else
    warn "npm not found -- skipping pi (optional). Install Node.js, then:
  npm install -g @earendil-works/pi-coding-agent@${PI_VERSION}
See docs/installation.md."
fi

# ── Done ─────────────────────────────────────────────────────────────────────────
info ""
info "secagent-install: done. Next steps:"
info "  1. secagent init --domain <your-suite-domain>"
info "  2. secagent login"
info ""
info "(pi is optional but recommended -- see docs/installation.md for how 'secagent"
info "init' wires it up, and how this differs from a SecDeploy/service install.)"

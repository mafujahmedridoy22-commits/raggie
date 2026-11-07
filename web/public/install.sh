#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Raggie installer.
#
#   curl -fsSL https://github.com/alicangunduz/raggie/releases/latest/download/install.sh | sudo bash
#   curl -fsSL https://github.com/alicangunduz/raggie/releases/latest/download/install.sh | sudo bash -s -- --yes --embeddings local --llm mock
#
# Downloads a pinned, sha256-verified Raggie release into /opt/raggie, asks how
# embeddings and the chat LLM should run and whether a domain gets HTTPS,
# generates every secret and starts the Docker Compose stack (Caddy is the only
# public entry point) from the published images. Re-running it upgrades: .env
# and .env.embeddings are backed up and only gain new keys. Every prompt has a
# flag (and a RAGGIE_<FLAG> env var); --yes never prompts. Run --help for all
# options. `make setup` runs the same wizard inside a repository checkout.
set -euo pipefail

# Pinned by the release workflow when it publishes this file; never `main`.
RAGGIE_DEFAULT_VERSION="0.1.0"
RAGGIE_REPO="${RAGGIE_REPO:-alicangunduz/raggie}"
RAGGIE_IMAGE_REGISTRY="${RAGGIE_IMAGE_REGISTRY:-ghcr.io/alicangunduz}"
RAGGIE_DOWNLOAD_BASE="${RAGGIE_DOWNLOAD_BASE:-https://github.com/${RAGGIE_REPO}/releases/download}"
RAGGIE_ARCHIVE_BASE="${RAGGIE_ARCHIVE_BASE:-https://github.com/${RAGGIE_REPO}/archive}"
RAGGIE_COMPOSE_MIN="2.24.4" # env_file `required: false` and `!reset`

if [[ -t 2 ]]; then I_B=$'\033[1;34m' I_Y=$'\033[1;33m' I_R=$'\033[1;31m' I_O=$'\033[0m'; else I_B="" I_Y="" I_R="" I_O=""; fi
i_log() { printf '%s==>%s %s\n' "$I_B" "$I_O" "$*" >&2; }
i_warn() { printf '%swarn:%s %s\n' "$I_Y" "$I_O" "$*" >&2; }
i_die() {
  printf '%serror:%s %s\n' "$I_R" "$I_O" "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Raggie installer

Source
  --version X.Y.Z            Release to install (default: this installer's release)
  --ref GIT_REF              Development: a branch/tag/commit archive (NOT checksum-verified; builds images)
  --source DIR               Use a local checkout in place (what `make setup` does)
  --install-dir DIR          Install target (default: /opt/raggie; in place with --source)
  --build | --pull           Build images locally / pull published images (default: pull, fallback build)

Embeddings (data stays on the server unless you choose cloud)
  --embeddings local|cloud
  --embeddings-preset local|openai|jina|voyage|mistral|custom
  --embeddings-api-key KEY   --embeddings-base-url URL  --embeddings-model ID  --embedding-dim N
  --embeddings-query-prefix P  --embeddings-passage-prefix P
  --embeddings-request-extra-json J  --embeddings-query-extra-json J  --embeddings-passage-extra-json J
  --reranker-preset local|none|cohere|jina|voyage|tei|cohere-compatible
  --reranker-api-key KEY     --reranker-url URL         --reranker-model ID
  --accept-data-transfer     Confirm the KVKK/GDPR basis for cloud mode (required with --yes)

Chat LLM
  --llm mock|ollama|self-hosted|openai
                             mock = offline demo, no key (default with --yes);
                             ollama = bundled Ollama; self-hosted = your vLLM/Ollama URL
  --ollama-model ID          Bundled Ollama model (default: OLLAMA_MODEL, qwen3:4b-instruct)
  --gpu auto|yes|no          NVIDIA GPU for the bundled Ollama (default: auto = nvidia-smi)
  --llm-base-url URL         --llm-api-key KEY          --llm-model ID

Site (Caddy front door: one origin for the app, widget and chat)
  --locale en|tr             Default UI language (default: en)
  --domain HOST              HTTPS with an automatic Let's Encrypt certificate
                             (DNS must point here; ports 80/443 reachable from the internet)
  --acme-email ADDR          Let's Encrypt contact (optional)
  --http-port N              Front-door HTTP port (default: 80)
  --https-port N             Front-door HTTPS port (default: 443)
  --public-url URL           Public origin override (behind another proxy/CDN)

Behaviour
  --yes                      Never prompt: flags, RAGGIE_* env vars, then defaults
  --reconfigure              On an existing install, ask the embeddings/LLM questions again
  --skip-probe               Do not live-test cloud/LLM endpoints
  --skip-checks              Continue below the RAM/disk minimums or with busy ports
  --no-start                 Configure only; do not pull, build or start
  --dry-run                  Show the planned .env / .env.embeddings changes (masked)
                             without writing or starting anything (requires --source)
  -h, --help

Every flag also reads an env var: --embeddings-api-key -> RAGGIE_EMBEDDINGS_API_KEY.
EOF
}

# ── early options (the full set is validated once the bundle's helpers load) ──
i_early_opt() { # NAME ARGS... -> value of --NAME / --NAME=V
  local name="$1" a
  shift
  while [[ $# -gt 0 ]]; do
    a="$1"
    shift
    case "$a" in
      "--$name") printf '%s' "${1:-}"; return 0 ;;
      "--$name="*) printf '%s' "${a#*=}"; return 0 ;;
    esac
  done
  return 1
}

i_wants_help() {
  local a
  for a in "$@"; do [[ "$a" == -h || "$a" == --help ]] && return 0; done
  return 1
}

# ── preflight ────────────────────────────────────────────────────────────────
i_version_ge() { # A B -> A >= B (dotted numeric)
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" == "$2" ]]
}

# i_preflight CONFIGURE_ONLY [CHECKOUT] — with CONFIGURE_ONLY=1 (--no-start)
# Docker problems only warn: nothing is pulled or started. CHECKOUT=1 (--source)
# is a developer checkout, where macOS is expected.
i_preflight() {
  local configure_only="${1:-0}" checkout="${2:-0}" os arch v
  os="$(uname -s)"
  case "$os" in
    Linux) ;;
    Darwin) [[ "$checkout" == 1 ]] || i_warn "macOS is supported for evaluation only (Docker Desktop/OrbStack); production targets Linux" ;;
    *) i_die "unsupported OS: $os (Linux amd64/arm64 required)" ;;
  esac
  arch="$(uname -m)"
  case "$arch" in
    x86_64 | amd64) RAGGIE_ARCH=amd64 ;;
    aarch64 | arm64) RAGGIE_ARCH=arm64 ;;
    *) i_die "unsupported CPU architecture: $arch (amd64 or arm64)" ;;
  esac
  export RAGGIE_ARCH
  for v in curl tar; do
    command -v "$v" >/dev/null 2>&1 || i_die "missing required command: $v"
  done
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || i_die "need sha256sum or shasum"
  local fail=i_die
  [[ "$configure_only" != 1 ]] || fail=i_warn
  if ! command -v docker >/dev/null 2>&1; then
    "$fail" "Docker is not installed (https://docs.docker.com/engine/install/)"
    return 0
  fi
  v="$(docker compose version --short 2>/dev/null | sed 's/^v//')" || true
  if [[ -z "$v" ]]; then
    "$fail" "the Docker Compose plugin is required (docker compose)"
  elif ! i_version_ge "$v" "$RAGGIE_COMPOSE_MIN"; then
    "$fail" "Docker Compose >= $RAGGIE_COMPOSE_MIN required (found $v)"
  fi
  if ! docker info >/dev/null 2>&1; then
    if [[ "$configure_only" == 1 ]]; then
      i_warn "the Docker daemon is not reachable; configuring only (--no-start), start Docker before 'up'"
    else
      i_die "cannot talk to the Docker daemon (is it running? run as root or a member of the docker group)"
    fi
  fi
  return 0
}

# A non-writable target (default /opt/raggie) needs root.
i_check_install_dir() {
  local parent="$1"
  while [[ ! -d "$parent" ]]; do parent="$(dirname "$parent")"; done
  if [[ ! -w "$parent" && "$(id -u)" -ne 0 ]]; then
    i_die "$parent is not writable: run as root (curl ... | sudo bash) or pass --install-dir under your home"
  fi
}

# i_check_resources local|cloud DIR — mode-dependent minimum / recommended
# (the bundled Ollama adds its model's RAM and disk).
i_check_resources() {
  local mode="$1" dir="$2" mem disk mem_min mem_rec disk_min disk_rec short=""
  if [[ "$mode" == local ]]; then
    mem_min=6144 mem_rec=8192 disk_min=20480 disk_rec=40960
  else
    mem_min=3072 mem_rec=4096 disk_min=10240 disk_rec=20480
  fi
  if [[ "${RAGGIE_LLM:-}" == ollama ]]; then
    mem_min=$((mem_min + 4096)) mem_rec=$((mem_rec + 4096)) disk_min=$((disk_min + 5120)) disk_rec=$((disk_rec + 10240))
    mode="$mode embeddings + bundled Ollama"
  else
    mode="$mode embeddings"
  fi
  mem="$(r_mem_mb)"
  disk="$(r_disk_free_mb "$dir")"
  i_log "host: ${RAGGIE_ARCH:-$(uname -m)}, $((mem / 1024)) GB RAM, $((disk / 1024)) GB free disk; $mode"
  ((mem == 0 || mem >= mem_min)) || short+=" RAM $((mem / 1024)) GB < $((mem_min / 1024)) GB;"
  ((disk >= disk_min)) || short+=" disk $((disk / 1024)) GB < $((disk_min / 1024)) GB;"
  if [[ -n "$short" ]]; then
    if [[ "${RAGGIE_SKIP_CHECKS:-0}" == 1 ]]; then
      i_warn "below the minimum for $mode:$short continuing (--skip-checks)"
    else
      i_die "below the minimum for $mode:$short use cloud embeddings / a remote LLM, a larger host, or --skip-checks"
    fi
  fi
  if ((mem > 0 && mem < mem_rec)); then i_warn "$((mem_rec / 1024)) GB RAM recommended for $mode"; fi
  if ((disk < disk_rec)); then i_warn "$((disk_rec / 1024)) GB free disk recommended (images, documents, vectors)"; fi
  return 0
}

# ── download + verify ────────────────────────────────────────────────────────
i_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# i_verify_sha256 FILE SUMS NAME — FILE must match NAME's line in SUMS.
i_verify_sha256() {
  local file="$1" sums="$2" name="$3" want got
  want="$(awk -v n="$name" '{f = $2; sub(/^\*/, "", f)} f == n {print $1; exit}' "$sums")"
  [[ "$want" =~ ^[0-9a-f]{64}$ ]] || i_die "no sha256 for $name in $(basename "$sums")"
  got="$(i_sha256 "$file")"
  [[ "$got" == "$want" ]] || i_die "sha256 mismatch for $name (expected $want, got $got): refusing to install"
}

# i_check_version_ref VERSION REF — both end up in download URLs and paths.
I_SEMVER_RE='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'
I_REF_RE='^[A-Za-z0-9._/-]+$'
i_check_version_ref() {
  local version="$1" ref="$2"
  [[ "$version" =~ $I_SEMVER_RE && "$version" != *..* ]] ||
    i_die "--version must be a release number like 1.2.3 (got '$version')"
  if [[ -n "$ref" ]]; then
    [[ "$ref" =~ $I_REF_RE && "$ref" != *..* && "$ref" != -* ]] ||
      i_die "--ref must be a branch, tag or commit (letters, digits, . _ / -; no '..'), got '$ref'"
  fi
}

# i_fetch_bundle STAGE VERSION REF — verified release tarball, or a dev --ref archive.
i_fetch_bundle() {
  local stage="$1" version="$2" ref="$3" tarball="$1.download.tar.gz"
  if [[ -n "$ref" ]]; then
    i_warn "--ref $ref: development archive, NOT checksum-verified; images are built locally"
    curl -fsSL --retry 3 -o "$tarball" "${RAGGIE_ARCHIVE_BASE}/${ref}.tar.gz" || i_die "download of ref $ref failed"
  else
    local name="raggie-${version}.tar.gz" base="${RAGGIE_DOWNLOAD_BASE}/v${version}"
    i_log "downloading Raggie v${version}"
    curl -fsSL --retry 3 -o "$tarball" "$base/$name" || i_die "download failed: $base/$name"
    curl -fsSL --retry 3 -o "$stage.SHA256SUMS" "$base/SHA256SUMS" || i_die "download failed: $base/SHA256SUMS"
    i_verify_sha256 "$tarball" "$stage.SHA256SUMS" "$name"
    i_log "sha256 verified ($name)"
  fi
  mkdir -p "$stage"
  tar -xzf "$tarball" -C "$stage" --strip-components=1
  rm -f "$tarball" "$stage.SHA256SUMS"
  [[ -f "$stage/deploy/scripts/lib.sh" ]] || i_die "the downloaded bundle has no deploy/scripts (older than this installer?)"
}

# ── compose wiring ───────────────────────────────────────────────────────────
# Published images instead of local builds (compose >= 2.24 for `!reset`).
i_write_images_override() {
  local file="$1" version="$2" variant="$3" svc img
  {
    echo "# Generated by install.sh: run the published Raggie v${version} images."
    echo "services:"
    for svc in edge control-plane mockllm embeddings ingestion-worker eval-worker scheduler-worker web; do
      case "$svc" in
        embeddings) img="raggie-embeddings:${version}-${variant}" ;;
        *-worker) img="raggie-workers:${version}" ;;
        *) img="raggie-${svc}:${version}" ;;
      esac
      echo "  ${svc}:"
      echo "    image: ${RAGGIE_IMAGE_REGISTRY}/${img}"
      echo "    build: !reset null"
    done
  } >"$file"
}

# i_compose_args DIR WITH_IMAGES — sets I_COMPOSE for this install's files/profiles.
i_compose_args() {
  local dir="$1" with_images="$2"
  I_COMPOSE=(docker compose --project-directory "$dir" -f "$dir/deploy/compose/docker-compose.yml")
  [[ "$with_images" != 1 ]] || I_COMPOSE+=(-f "$dir/.raggie/docker-compose.images.yml")
  case "${RAGGIE_LLM:-}" in
    mock) I_COMPOSE+=(-f "$dir/deploy/compose/docker-compose.demo.yml" --profile core --profile mock) ;;
    ollama)
      I_COMPOSE+=(-f "$dir/deploy/compose/docker-compose.ollama.yml")
      [[ "${RAGGIE_GPU:-0}" != 1 ]] || I_COMPOSE+=(-f "$dir/deploy/compose/docker-compose.gpu.yml")
      I_COMPOSE+=(--profile core)
      ;;
    *) I_COMPOSE+=(--profile core) ;;
  esac
  I_COMPOSE+=(--env-file "$dir/.env")
}

# State for the next run plus a `.raggie/compose` wrapper with the same flags.
i_write_state() {
  local dir="$1" with_images="$2" q
  mkdir -p "$dir/.raggie"
  {
    echo "# Written by install.sh; read on the next run."
    echo "RAGGIE_INSTALLED_VERSION=${RAGGIE_INSTALL_VERSION:-}"
    echo "RAGGIE_LLM_MODE=${RAGGIE_LLM:-}"
    echo "RAGGIE_GPU=${RAGGIE_GPU:-0}"
    echo "RAGGIE_EMBEDDINGS_VARIANT=${RAGGIE_EMBEDDINGS_VARIANT:-}"
    echo "RAGGIE_IMAGES=$([[ "$with_images" == 1 ]] && echo published || echo build)"
  } >"$dir/.raggie/state.env"
  i_compose_args "$dir" "$with_images"
  printf -v q '%q ' "${I_COMPOSE[@]}"
  printf '#!/usr/bin/env bash\n# Generated by install.sh: docker compose for this install.\nexec %s"$@"\n' "$q" >"$dir/.raggie/compose"
  chmod 755 "$dir/.raggie/compose"
}

i_wait_healthy() { # "[curl options] URL"
  local deadline=$(($(date +%s) + ${RAGGIE_WAIT_SECONDS:-600}))
  local -a target
  read -r -a target <<<"$1"
  local start next
  start=$(date +%s)
  next=$((start + 30))
  until curl -fsS "${target[@]}" >/dev/null 2>&1; do
    [[ $(date +%s) -lt $deadline ]] || return 1
    if (($(date +%s) >= next)); then
      i_log "still waiting ($(($(date +%s) - start)) s; progress: .raggie/compose ps)"
      next=$((next + 30))
    fi
    sleep 3
  done
}

# The same check through Caddy (the public path). With a domain the certificate
# may still be on its way, so this only warns.
i_check_front_door() {
  local env="$1" domain http https
  domain="$(r_env_get "$env" RAGGIE_DOMAIN)"
  http="$(r_env_get "$env" HTTP_PORT)"
  https="$(r_env_get "$env" HTTPS_PORT)"
  if [[ -z "$domain" ]]; then
    RAGGIE_WAIT_SECONDS=60 i_wait_healthy "http://127.0.0.1:${http:-80}/healthz" ||
      i_warn "Caddy does not answer on port ${http:-80}; inspect: .raggie/compose logs caddy"
  elif ! RAGGIE_WAIT_SECONDS=90 i_wait_healthy "--resolve ${domain}:${https:-443}:127.0.0.1 https://${domain}:${https:-443}/healthz"; then
    i_warn "https://${domain} is not serving yet (certificate pending?). Check DNS, ports 80/443 from the internet, and: .raggie/compose logs caddy"
  fi
}

i_start() {
  local dir="$1" with_images="$2"
  i_compose_args "$dir" "$with_images"
  if [[ "$with_images" == 1 ]]; then
    i_log "pulling published images"
    if ! "${I_COMPOSE[@]}" pull --quiet; then
      i_warn "image pull failed; building locally instead"
      with_images=0
      i_write_state "$dir" 0
      i_compose_args "$dir" 0
    fi
  fi
  i_log "starting the stack"
  if [[ "$with_images" == 1 ]]; then
    "${I_COMPOSE[@]}" up -d --remove-orphans
  else
    "${I_COMPOSE[@]}" up -d --build --remove-orphans
  fi
}

# ── main ─────────────────────────────────────────────────────────────────────
main() {
  if i_wants_help "$@"; then
    usage
    exit 0
  fi
  local source ref version dir stage in_repo=0 with_images env
  source="$(i_early_opt source "$@" || printf '%s' "${RAGGIE_SOURCE:-}")"
  ref="$(i_early_opt ref "$@" || printf '%s' "${RAGGIE_REF:-}")"
  version="$(i_early_opt version "$@" || printf '%s' "${RAGGIE_VERSION:-$RAGGIE_DEFAULT_VERSION}")"
  version="${version#v}"
  i_check_version_ref "$version" "$ref"

  local configure_only=0 a
  for a in "$@"; do [[ "$a" != --no-start ]] || configure_only=1; done
  [[ "${RAGGIE_NO_START:-0}" != 1 ]] || configure_only=1
  [[ "${RAGGIE_SKIP_PREFLIGHT:-0}" == 1 ]] || i_preflight "$configure_only" "$([[ -n "$source" ]] && echo 1 || echo 0)"

  # --dry-run shows the plan from a checkout; it must not download or touch the
  # install target, so it requires --source.
  if [[ "${RAGGIE_DRY_RUN:-0}" == 1 && -z "$source" ]]; then
    i_die "--dry-run requires --source DIR (a local checkout) so the plan can be shown without downloading or writing"
  fi

  if [[ -n "$source" ]]; then
    in_repo=1
    stage="$(cd "$source" && pwd)"
    dir="$(i_early_opt install-dir "$@" || printf '%s' "${RAGGIE_INSTALL_DIR:-$stage}")"
  else
    dir="$(i_early_opt install-dir "$@" || printf '%s' "${RAGGIE_INSTALL_DIR:-${INSTALL_DIR:-/opt/raggie}}")"
    i_check_install_dir "$dir"
    stage="$(mktemp -d)"
    # shellcheck disable=SC2064 # expand now: the paths are fixed
    trap "rm -rf '$stage' '$stage.download.tar.gz' '$stage.SHA256SUMS'" EXIT
    i_fetch_bundle "$stage" "$version" "$ref"
  fi

  # shellcheck source=../../deploy/scripts/lib.sh
  . "$stage/deploy/scripts/lib.sh"
  # shellcheck source=../../deploy/scripts/configure-env.sh
  . "$stage/deploy/scripts/configure-env.sh"
  # shellcheck source=../../deploy/scripts/configure-embeddings.sh
  . "$stage/deploy/scripts/configure-embeddings.sh"
  # shellcheck source=../../deploy/scripts/configure-llm.sh
  . "$stage/deploy/scripts/configure-llm.sh"
  r_parse_flags "$@"

  if ((in_repo == 0)) && [[ "${RAGGIE_DRY_RUN:-0}" != 1 ]]; then
    mkdir -p "$dir"
    # Overlay the release; user files (.env, .env.embeddings, .raggie/) are not in the bundle.
    cp -R "$stage"/. "$dir"/
  fi
  dir="$(cd "$dir" && pwd)"
  env="$dir/.env"
  export RAGGIE_INSTALL_VERSION="${ref:-$version}"

  raggie_env_init "$dir"
  raggie_embeddings_configure "$dir"
  raggie_llm_configure "$dir"
  if [[ "$(r_env_get "$env" EMBEDDINGS_PROVIDER)" == local ]]; then
    i_check_resources local "$dir"
  else
    i_check_resources cloud "$dir"
  fi
  raggie_env_secrets "$env"
  raggie_env_site "$env"

  # Published images for a pinned release; local builds for --ref, a checkout, or --build.
  with_images=1
  if [[ "${RAGGIE_BUILD:-0}" == 1 || -n "$ref" ]] || { ((in_repo)) && [[ "${RAGGIE_PULL:-0}" != 1 ]]; }; then
    with_images=0
  fi
  if [[ "${RAGGIE_DRY_RUN:-0}" != 1 ]]; then
    mkdir -p "$dir/.raggie"
    i_write_images_override "$dir/.raggie/docker-compose.images.yml" "$version" "${RAGGIE_EMBEDDINGS_VARIANT:-local}"
    i_write_state "$dir" "$with_images"
  fi
  r_print_changes

  if [[ "${RAGGIE_DRY_RUN:-0}" == 1 ]]; then
    r_log "dry-run complete: nothing written or started (plan above)"
    exit 0
  fi
  if [[ "${RAGGIE_NO_START:-0}" == 1 ]]; then
    r_log "configured (--no-start). Start with: $dir/.raggie/compose up -d"
  else
    i_start "$dir" "$with_images"
    i_log "waiting for the web app (the first start can take a few minutes${RAGGIE_LLM:+; ollama pulls its model first})"
    if ! i_wait_healthy "http://127.0.0.1:$(r_env_get "$env" WEB_PORT)/healthz"; then
      i_warn "the web app did not become healthy in time; inspect: $dir/.raggie/compose logs --tail=200"
      exit 1
    fi
    i_check_front_door "$env"
  fi

  local url token
  url="$(r_env_get "$env" PUBLIC_WEB_URL)"
  url="${url:-http://$(raggie_guess_host)}"
  token="$(r_env_get "$env" SETUP_TOKEN)"
  cat <<EOF

Raggie ${RAGGIE_INSTALL_VERSION} is $([[ "${RAGGIE_NO_START:-0}" == 1 ]] && echo configured || echo up) in ${dir}.

  Onboarding:   ${url%/}/onboarding
  Setup token:  ${token}
                (needed once, for the first admin; also stored in ${env})

  Manage:       ${dir}/.raggie/compose ps | logs -f | down
  Reconfigure:  re-run the installer with --reconfigure, or
                deploy/scripts/configure-embeddings.sh / configure-llm.sh
EOF
}

if [[ "${RAGGIE_INSTALL_SOURCED:-0}" != 1 ]]; then
  main "$@"
fi

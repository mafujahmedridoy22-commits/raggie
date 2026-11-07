# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash disable=SC2034 # globals (R_HTTP_*, colors) are read by the sourcing scripts
# Shared helpers for web/public/install.sh, deploy/scripts/configure-*.sh and
# `make setup`. Sourced, never executed.
#
# Settings arrive as RAGGIE_* environment variables. Every `--some-flag VALUE`
# maps to RAGGIE_SOME_FLAG (see r_parse_flags), so each prompt also has a flag
# and an env var, and RAGGIE_YES=1 (--yes) never prompts.

# ── output ────────────────────────────────────────────────────────────────────
if [[ -t 2 ]]; then
  R_BLUE=$'\033[1;34m' R_YELLOW=$'\033[1;33m' R_RED=$'\033[1;31m' R_DIM=$'\033[2m' R_OFF=$'\033[0m'
else
  R_BLUE="" R_YELLOW="" R_RED="" R_DIM="" R_OFF=""
fi
r_log() { printf '%s==>%s %s\n' "$R_BLUE" "$R_OFF" "$*" >&2; }
r_warn() { printf '%swarn:%s %s\n' "$R_YELLOW" "$R_OFF" "$*" >&2; }
r_die() {
  printf '%serror:%s %s\n' "$R_RED" "$R_OFF" "$*" >&2
  exit 1
}
r_note() { printf '%s%s%s\n' "$R_DIM" "$*" "$R_OFF" >&2; }

# ── flags ─────────────────────────────────────────────────────────────────────
# Flags without a value. Everything else listed in R_VALUE_FLAGS takes one.
R_BOOL_FLAGS=" yes dry-run reconfigure accept-data-transfer skip-probe build pull skip-checks no-start "
R_VALUE_FLAGS=" version ref source install-dir locale public-url domain acme-email http-port https-port
  embeddings embeddings-preset embeddings-api-key embeddings-base-url embeddings-model
  embedding-dim embeddings-query-prefix embeddings-passage-prefix
  embeddings-request-extra-json embeddings-query-extra-json embeddings-passage-extra-json
  reranker-preset reranker-api-key reranker-url reranker-model
  llm llm-base-url llm-api-key llm-model ollama-model gpu "
R_VALUE_FLAGS="$(printf '%s' "$R_VALUE_FLAGS" | tr '\n' ' ')"

r_flag_var() { # some-flag -> RAGGIE_SOME_FLAG
  local name="${1//-/_}"
  printf 'RAGGIE_%s' "$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')"
}

# r_parse_flags ARGS... — export RAGGIE_* for every known flag; die on unknown
# ones. Accepts `--flag value` and `--flag=value`. -h/--help sets RAGGIE_HELP=1.
r_parse_flags() {
  local arg name value
  while [[ $# -gt 0 ]]; do
    arg="$1"
    shift
    case "$arg" in
      -h | --help) export RAGGIE_HELP=1; continue ;;
      --*=*) name="${arg%%=*}"; name="${name#--}"; value="${arg#*=}" ;;
      --*) name="${arg#--}"; value="" ;;
      *) r_die "unexpected argument: $arg (see --help)" ;;
    esac
    if [[ "$R_BOOL_FLAGS" == *" $name "* ]]; then
      [[ "$arg" != *=* ]] || r_die "--$name takes no value"
      export "$(r_flag_var "$name")=1"
    elif [[ "$R_VALUE_FLAGS" == *" $name "* ]]; then
      if [[ "$arg" != *=* ]]; then
        [[ $# -gt 0 ]] || r_die "--$name needs a value"
        value="$1"
        shift
      fi
      export "$(r_flag_var "$name")=$value"
    else
      r_die "unknown option: --$name (see --help)"
    fi
  done
}

# ── prompts (always from the terminal, so `curl ... | bash` works) ───────────
: "${RAGGIE_TTY:=/dev/tty}"

r_can_prompt() {
  [[ "${RAGGIE_YES:-0}" != 1 ]] && { : <"$RAGGIE_TTY"; } 2>/dev/null
}

r_say() { # print to the terminal (prompts, menus)
  if r_can_prompt; then printf '%s\n' "$*" >"$RAGGIE_TTY"; else printf '%s\n' "$*" >&2; fi
}

# r_ask VAR LABEL DEFAULT [FLAG] — VAR keeps a preset value; otherwise prompt,
# or take DEFAULT under --yes. An empty result with no default dies naming FLAG.
r_ask() {
  local var="$1" label="$2" default="$3" flag="${4:-}" answer
  if [[ -n "${!var:-}" ]]; then return 0; fi
  if r_can_prompt; then
    printf '%s%s: ' "$label" "${default:+ [$default]}" >"$RAGGIE_TTY"
    IFS= read -r answer <"$RAGGIE_TTY" || answer=""
    answer="${answer:-$default}"
  else
    answer="$default"
  fi
  [[ -n "$answer" || -z "$flag" ]] || r_die "$label is required (pass $flag)"
  printf -v "$var" '%s' "$answer"
}

# r_ask_secret VAR LABEL FLAG [optional] — hidden input; never echoed or logged.
r_ask_secret() {
  local var="$1" label="$2" flag="$3" optional="${4:-}" answer=""
  if [[ -n "${!var:-}" ]]; then return 0; fi
  if r_can_prompt; then
    printf '%s: ' "$label" >"$RAGGIE_TTY"
    IFS= read -rs answer <"$RAGGIE_TTY" || answer=""
    printf '\n' >"$RAGGIE_TTY"
  fi
  [[ -n "$answer" || -n "$optional" ]] || r_die "$label is required (pass $flag or its env var)"
  printf -v "$var" '%s' "$answer"
}

# r_choose VAR LABEL DEFAULT OPTION... — numbered menu; "name|description"
# options; accepts the number or the name.
r_choose() {
  local var="$1" label="$2" default="$3" answer i name
  shift 3
  local -a options=("$@")
  if [[ -n "${!var:-}" ]]; then
    for i in "${options[@]}"; do [[ "${i%%|*}" == "${!var}" ]] && return 0; done
    r_die "invalid value '${!var}' for $label (one of: $(r_option_names "${options[@]}"))"
  fi
  if ! r_can_prompt; then
    printf -v "$var" '%s' "$default"
    return 0
  fi
  while :; do
    printf '%s\n' "$label" >"$RAGGIE_TTY"
    for i in "${!options[@]}"; do
      name="${options[$i]%%|*}"
      printf '  %d) %s%s\n' "$((i + 1))" "${options[$i]#*|}" "$([[ "$name" == "$default" ]] && echo ' [default]')" >"$RAGGIE_TTY"
    done
    printf '> ' >"$RAGGIE_TTY"
    IFS= read -r answer <"$RAGGIE_TTY" || answer=""
    answer="${answer:-$default}"
    if [[ "$answer" =~ ^[0-9]+$ ]] && ((answer >= 1 && answer <= ${#options[@]})); then
      answer="${options[$((answer - 1))]%%|*}"
    fi
    for i in "${options[@]}"; do
      if [[ "${i%%|*}" == "$answer" ]]; then
        printf -v "$var" '%s' "$answer"
        return 0
      fi
    done
    printf 'Please pick 1-%d.\n' "${#options[@]}" >"$RAGGIE_TTY"
  done
}

r_option_names() {
  local o out=""
  for o in "$@"; do out+="${o%%|*} "; done
  printf '%s' "${out% }"
}

# r_confirm LABEL — yes/no question; --yes answers yes.
r_confirm() {
  local answer
  r_can_prompt || return 0
  printf '%s [y/N]: ' "$1" >"$RAGGIE_TTY"
  IFS= read -r answer <"$RAGGIE_TTY" || answer=""
  [[ "$answer" =~ ^([yY]|[yY][eE][sS]|[eE]|[eE][vV][eE][tT])$ ]]
}

# ── env files ────────────────────────────────────────────────────────────────
# Changes made in this run, printed by r_print_changes. Secret values are masked.
R_CHANGES=()
R_SECRET_KEYS=" POSTGRES_PASSWORD DATABASE_URL REDIS_PASSWORD REDIS_URL QDRANT_API_KEY
  MINIO_ROOT_PASSWORD JWT_SECRET WIDGET_TOKEN_SECRET INTERNAL_API_SECRET SETUP_TOKEN
  INTEGRATION_ENC_KEY METRICS_TOKEN LLMAAS_API_KEY EMBEDDINGS_UPSTREAM_API_KEY
  RERANKER_UPSTREAM_API_KEY BRAVE_API_KEY OLLAMA_API_KEY "
R_SECRET_KEYS="$(printf '%s' "$R_SECRET_KEYS" | tr '\n' ' ')"

r_mask() {
  local v="$1"
  if [[ -z "$v" ]]; then printf '(empty)'; elif ((${#v} <= 8)); then printf '****'; else printf '%s…(%d chars)' "${v:0:4}" "${#v}"; fi
}

r_show() { # KEY VALUE -> printable value
  if [[ "$R_SECRET_KEYS" == *" $1 "* ]]; then r_mask "$2"; else printf '%s' "${2:-(empty)}"; fi
}

# r_env_get FILE KEY — value of the last KEY= line (inline comment and quotes
# removed); empty when absent.
r_env_get() {
  [[ -f "$1" ]] || return 0
  K="$2" awk '
    BEGIN { k = ENVIRON["K"] "="; v = "" }
    index($0, k) == 1 {
      v = substr($0, length(k) + 1)
      if (v ~ /^'"'"'/) { sub(/^'"'"'/, "", v); sub(/'"'"'[ \t]*(#.*)?$/, "", v) }
      else if (v ~ /^"/) { sub(/^"/, "", v); sub(/"[ \t]*(#.*)?$/, "", v) }
      else { sub(/[ \t]+#.*$/, "", v); sub(/[ \t]+$/, "", v) }
    }
    END { printf "%s", v }
  ' "$1"
}

r_env_has() { [[ -f "$1" ]] && grep -q "^$2=" "$1"; }

# Values go in unquoted when they only use safe characters, single-quoted
# otherwise (compose reads single-quoted values literally). `"` is not safe: a
# value starting with it would read back as a double-quoted value, so JSON is
# single-quoted.
R_SAFE_VALUE='^[][A-Za-z0-9_./:@%+=,{}~-]*$'
r_env_quote() {
  local v="$1"
  if [[ "$v" =~ $R_SAFE_VALUE ]]; then
    printf '%s' "$v"
  elif [[ "$v" != *"'"* && "$v" != *$'\n'* ]]; then
    printf "'%s'" "$v"
  else
    r_die "value for this setting contains a quote or newline, which .env files cannot hold safely"
  fi
}

# r_env_set FILE KEY VALUE — replace KEY's value in place (keeping an inline
# comment) or append it. Records the change when the value differs.
r_env_set() {
  local file="$1" key="$2" value="$3" old quoted tmp
  old="$(r_env_get "$file" "$key")"
  if r_env_has "$file" "$key" && [[ "$old" == "$value" ]]; then return 0; fi
  quoted="$(r_env_quote "$value")" || r_die "cannot write $key to $(basename "$file")"
  R_CHANGES+=("$(basename "$file"): $key $(r_show "$key" "$old") -> $(r_show "$key" "$value")")
  if [[ "${RAGGIE_DRY_RUN:-0}" == 1 ]]; then return 0; fi
  if r_env_has "$file" "$key"; then
    tmp="$(mktemp "${file}.XXXXXX")"
    K="$key" V="$quoted" awk '
      BEGIN { k = ENVIRON["K"] "="; v = ENVIRON["V"] }
      index($0, k) == 1 {
        rest = substr($0, length(k) + 1); comment = ""
        if (rest !~ /^["'"'']/ && match(rest, /[ \t]+#.*$/)) comment = substr(rest, RSTART)
        print k v comment; next
      }
      { print }
    ' "$file" >"$tmp"
    chmod --reference="$file" "$tmp" 2>/dev/null || chmod "$(r_file_mode "$file")" "$tmp"
    mv "$tmp" "$file"
  else
    [[ ! -s "$file" || "$(tail -c1 "$file")" == "" ]] || printf '\n' >>"$file"
    printf '%s=%s\n' "$key" "$quoted" >>"$file"
  fi
}

r_file_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }

# r_env_backup FILE — timestamped copy next to FILE (mode kept); prints its path.
r_env_backup() {
  local backup
  backup="$1.bak.$(date +%Y%m%d%H%M%S)"
  [[ ! -e "$backup" ]] || backup="$backup.$$"
  if [[ "${RAGGIE_DRY_RUN:-0}" == 1 ]]; then
    printf '%s' "$backup"
    return 0
  fi
  cp -p "$1" "$backup"
  printf '%s' "$backup"
}

# r_env_add_missing FILE EXAMPLE — append every key EXAMPLE defines and FILE
# lacks, with EXAMPLE's default and the comment block above it.
# shellcheck disable=SC2094 # reads EXAMPLE, appends to FILE (different files)
r_env_add_missing() {
  local file="$1" example="$2" added=0 key line block=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^#|^$ ]]; then
      [[ -z "$line" ]] && block="" || block+="$line"$'\n'
      continue
    fi
    key="${line%%=*}"
    if [[ "$key" =~ ^[A-Z0-9_]+$ ]] && ! r_env_has "$file" "$key"; then
      R_CHANGES+=("$(basename "$file"): $key added (default $(r_show "$key" "$(r_env_get "$example" "$key")"))")
      if [[ "${RAGGIE_DRY_RUN:-0}" != 1 ]]; then
        if ((added == 0)); then
          printf '\n# ── Added by the Raggie installer on %s (new settings, defaults) ──\n' "$(date +%Y-%m-%d)" >>"$file"
        fi
        printf '%s%s\n' "$block" "$line" >>"$file"
        added=$((added + 1))
      fi
    fi
    block=""
  done <"$example"
  return 0
}

r_print_changes() {
  local c
  if ((${#R_CHANGES[@]} == 0)); then
    r_log "configuration unchanged"
    return 0
  fi
  r_log "configuration changes (${#R_CHANGES[@]}):"
  for c in "${R_CHANGES[@]}"; do printf '    %s\n' "$c" >&2; done
}

# ── secrets ──────────────────────────────────────────────────────────────────
r_rand_hex() { # BYTES
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
  fi
}

r_rand_b64url() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'
  else
    head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'
  fi
}

r_is_placeholder() { [[ -z "$1" || "$1" == *change-me* || "$1" == raggie || "$1" == raggie-minio ]]; }

# ── HTTP (curl only; credentials never on the command line) ─────────────────
# r_http METHOD URL [BEARER] [BODY_FILE] — sets R_HTTP_STATUS (000 on transport
# failure) and R_HTTP_BODY. Call it directly, not in a pipeline (a pipeline
# would run it in a subshell and lose both variables).
# The headers (bearer included) reach curl on its stdin (`-H @-`): never argv,
# never a file that an interrupted run could leave behind.
r_http() {
  local method="$1" url="$2" bearer="${3:-}" body="${4:-}" out
  local -a args=(-sS -m "${RAGGIE_HTTP_TIMEOUT:-20}" -X "$method" -H @- -w '\n%{http_code}')
  [[ -z "$body" ]] || args+=(-H 'Content-Type: application/json' --data-binary "@$body")
  out="$(
    {
      printf 'Accept: application/json\n'
      [[ -z "$bearer" ]] || printf 'Authorization: Bearer %s\n' "$bearer"
    } | curl "${args[@]}" "$url" 2>/dev/null
  )" || out=$'\n000'
  R_HTTP_STATUS="${out##*$'\n'}"
  R_HTTP_BODY="${out%$'\n'*}"
  [[ "$R_HTTP_STATUS" =~ ^[0-9]{3}$ ]] || R_HTTP_STATUS=000
}

r_have_python() { command -v python3 >/dev/null 2>&1; }

# r_json_valid TEXT — 0 when TEXT is a JSON object (python3 required; without it
# only the braces are checked).
r_json_object() {
  [[ -z "$1" ]] && return 0
  if r_have_python; then
    python3 -c 'import json,sys; sys.exit(0 if isinstance(json.loads(sys.argv[1]), dict) else 1)' "$1" 2>/dev/null
  else
    [[ "$1" == "{"*"}" ]]
  fi
}

# ── host facts ───────────────────────────────────────────────────────────────
r_mem_mb() { # total RAM in MiB (0 when unknown)
  local kb bytes
  if [[ -r /proc/meminfo ]]; then
    kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    printf '%s' "$((${kb:-0} / 1024))"
  elif bytes="$(sysctl -n hw.memsize 2>/dev/null)"; then
    printf '%s' "$((bytes / 1024 / 1024))"
  else
    printf 0
  fi
}

r_disk_free_mb() { # free MiB on the filesystem holding DIR (nearest existing parent)
  local dir="$1"
  while [[ ! -d "$dir" ]]; do dir="$(dirname "$dir")"; done
  df -Pk "$dir" | awk 'NR == 2 {print int($4 / 1024)}'
}

# r_usage FILE — print FILE's header comment (after the shebang/SPDX lines).
r_usage() {
  awk 'NR > 1 && /^# SPDX/ {next} NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$1"
}

r_port_in_use() { # PORT — something already listens on it locally
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

r_resolves() { # HOST — has a DNS answer
  if command -v getent >/dev/null 2>&1; then
    getent hosts "$1" >/dev/null 2>&1
  elif r_have_python; then
    python3 -c 'import socket,sys; socket.getaddrinfo(sys.argv[1], None)' "$1" >/dev/null 2>&1
  else
    return 0
  fi
}

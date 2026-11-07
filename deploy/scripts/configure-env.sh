#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Base .env for a Raggie install: create it from .env.example (fresh install) or
# upgrade it in place (backup + append new keys), generate every secret, and set
# locale and public URLs. Sourced by web/public/install.sh; runnable alone:
#
#   deploy/scripts/configure-env.sh [--locale en|tr] [--domain HOST]
#       [--acme-email ADDR] [--http-port N] [--https-port N] [--public-url URL] [--yes]
#
# Never overwrites an existing value except: empty secrets are generated,
# placeholder JWT/widget secrets are replaced, and flags passed explicitly win.

# raggie_env_init DIR — sets RAGGIE_SETUP_MODE=fresh|upgrade and prepares .env.
raggie_env_init() {
  local dir="$1" env="$1/.env" example="$1/.env.example" backup
  [[ -f "$example" ]] || r_die "$example not found"
  if [[ -f "$env" ]]; then
    RAGGIE_SETUP_MODE=upgrade
    backup="$(r_env_backup "$env")"
    r_log "existing .env found: upgrade mode (backup: $backup)"
    r_env_add_missing "$env" "$example"
  else
    RAGGIE_SETUP_MODE=fresh
    if [[ "${RAGGIE_DRY_RUN:-0}" == 1 ]]; then
      r_log "would create $env from .env.example (--dry-run)"
    else
      (umask 077 && cp "$example" "$env")
      chmod 600 "$env"
      r_log "created $env from .env.example"
    fi
  fi
  if [[ -f "$dir/.env.embeddings" ]]; then
    backup="$(r_env_backup "$dir/.env.embeddings")"
    r_note "    .env.embeddings backup: $backup"
  fi
  export RAGGIE_SETUP_MODE
}

# raggie_env_secrets ENV — fresh: generate all. upgrade: fill empty ones only;
# a data-store password already in use is never changed.
raggie_env_secrets() {
  local env="$1" key cur
  local fresh=0
  [[ "${RAGGIE_SETUP_MODE:-fresh}" == fresh ]] && fresh=1

  # Safe to (re)generate whenever empty or a placeholder: nothing persisted
  # depends on them beyond sessions/tokens.
  for key in JWT_SECRET WIDGET_TOKEN_SECRET INTERNAL_API_SECRET SETUP_TOKEN QDRANT_API_KEY; do
    cur="$(r_env_get "$env" "$key")"
    if ((fresh)) || r_is_placeholder "$cur"; then
      r_env_set "$env" "$key" "$(r_rand_hex 32)"
    fi
  done
  cur="$(r_env_get "$env" INTEGRATION_ENC_KEY)"
  # An existing key encrypts stored integration credentials: only ever set it once.
  if [[ -z "$cur" ]]; then
    r_env_set "$env" INTEGRATION_ENC_KEY "$(r_rand_b64url)"
  fi

  # Data-store credentials are baked into existing volumes: generate on a
  # fresh install only; on upgrade, warn instead of breaking the data.
  if ((fresh)); then
    local pg redis
    pg="$(r_rand_hex 24)"
    redis="$(r_rand_hex 24)"
    r_env_set "$env" POSTGRES_PASSWORD "$pg"
    r_env_set "$env" DATABASE_URL "postgresql://$(r_env_get "$env" POSTGRES_USER):${pg}@postgres:5432/$(r_env_get "$env" POSTGRES_DB)"
    r_env_set "$env" MINIO_ROOT_PASSWORD "$(r_rand_hex 24)"
    r_env_set "$env" REDIS_PASSWORD "$redis"
    r_env_set "$env" REDIS_URL "redis://:${redis}@redis:6379/0"
  else
    for key in POSTGRES_PASSWORD MINIO_ROOT_PASSWORD; do
      if r_is_placeholder "$(r_env_get "$env" "$key")"; then
        r_warn "$key is a default/placeholder; it is already used by the existing data volume, so it is left as is. Rotate it manually (docs/production.md) before RAGGIE_ENV=production."
      fi
    done
    if [[ -z "$(r_env_get "$env" REDIS_PASSWORD)" ]]; then
      r_warn "REDIS_PASSWORD is empty (Valkey without auth); left unchanged on upgrade. Set REDIS_PASSWORD and REDIS_URL together to enable it."
    fi
  fi
}

# raggie_env_site ENV — RAGGIE_ENV, locale and the public origin. Caddy is the
# only public entry point: web and the public edge paths share ONE origin,
# https://<RAGGIE_DOMAIN> (automatic Let's Encrypt) or plain http://<host>.
raggie_env_site() {
  local env="$1" fresh=0 approved=0 domain http https origin host secure=false
  [[ "${RAGGIE_SETUP_MODE:-fresh}" == fresh ]] && fresh=1
  if ((fresh)) || [[ "${RAGGIE_RECONFIGURE:-0}" == 1 || -n "${RAGGIE_DOMAIN+x}${RAGGIE_PUBLIC_URL:-}${RAGGIE_HTTP_PORT:-}${RAGGIE_HTTPS_PORT:-}" ]]; then
    approved=1
  fi
  if ((fresh)); then
    r_env_set "$env" RAGGIE_ENV production
    r_env_set "$env" COMPOSE_PROFILES core
  fi
  if [[ -n "${RAGGIE_LOCALE:-}" ]] || ((fresh)); then
    local locale="${RAGGIE_LOCALE:-en}"
    [[ "$locale" == en || "$locale" == tr ]] || r_die "--locale must be en or tr"
    r_env_set "$env" NEXT_PUBLIC_DEFAULT_LOCALE "$locale"
  fi

  # Domain and front-door ports.
  if [[ -n "${RAGGIE_DOMAIN+x}" ]]; then
    domain="$RAGGIE_DOMAIN"
  elif ((fresh)) || [[ "${RAGGIE_RECONFIGURE:-0}" == 1 ]]; then
    domain=""
    r_ask domain "Public domain for HTTPS, e.g. chat.example.com (empty = plain HTTP on this server's address)" "$(r_env_get "$env" RAGGIE_DOMAIN)"
  else
    domain="$(r_env_get "$env" RAGGIE_DOMAIN)"
  fi
  domain="${domain#http*://}"
  domain="${domain%%/*}"
  http="${RAGGIE_HTTP_PORT:-$(r_env_get "$env" HTTP_PORT)}"
  https="${RAGGIE_HTTPS_PORT:-$(r_env_get "$env" HTTPS_PORT)}"
  http="${http:-80}" https="${https:-443}"
  if ((approved)); then
    raggie_check_port http "$http" --http-port
    http="$R_PORT"
    raggie_check_port https "$https" --https-port
    https="$R_PORT"
    r_env_set "$env" RAGGIE_DOMAIN "$domain"
    r_env_set "$env" HTTP_PORT "$http"
    r_env_set "$env" HTTPS_PORT "$https"
    if [[ -n "${RAGGIE_ACME_EMAIL:-}" ]]; then r_env_set "$env" ACME_EMAIL "$RAGGIE_ACME_EMAIL"; fi
  fi

  # The public origin.
  if [[ -n "${RAGGIE_PUBLIC_URL:-}" ]]; then
    origin="${RAGGIE_PUBLIC_URL%/}"
  elif [[ -n "$domain" ]]; then
    origin="https://${domain}"
    [[ "$https" == 443 ]] || origin+=":$https"
  else
    host="$(raggie_url_host "$(r_env_get "$env" PUBLIC_WEB_URL)")"
    host="${host:-$(raggie_guess_host)}"
    origin="http://${host}"
    [[ "$http" == 80 ]] || origin+=":$http"
  fi
  [[ "$origin" == https://* ]] && secure=true

  if ((!approved)); then
    local old_web old_widget
    old_web="$(r_env_get "$env" PUBLIC_WEB_URL)"
    old_widget="$(r_env_get "$env" WIDGET_EMBED_BASE_URL)"
    if [[ "$old_widget" =~ :(8080|$(r_env_get "$env" EDGE_HOST_PORT))/?$ || "$old_web" =~ :3000/?$ ]]; then
      r_warn "web (:3000) and edge (:8080) are no longer public: Caddy serves both at ${origin}."
      r_warn "PUBLIC_WEB_URL=${old_web:-(empty)} / WIDGET_EMBED_BASE_URL=${old_widget} still point at them (existing widget snippets too)."
      if r_can_prompt && r_confirm "Point both at ${origin}?"; then
        approved=1
      else
        r_warn "left unchanged; re-run with --reconfigure to switch"
      fi
    fi
  fi
  if ((approved)); then
    r_env_set "$env" PUBLIC_WEB_URL "$origin"
    r_env_set "$env" WIDGET_EMBED_BASE_URL "$origin"
    # Plain HTTP (no domain, e.g. a LAN IP): Secure cookies would break login.
    r_env_set "$env" COOKIE_SECURE "$secure"
    r_env_set "$env" ENABLE_HSTS "$secure"
  fi

  if [[ -n "$domain" ]]; then
    r_resolves "$domain" || r_warn "$domain does not resolve yet: point its DNS A/AAAA record at this server before starting, or Let's Encrypt fails"
    r_note "    HTTPS for $domain: Let's Encrypt (HTTP-01) needs ports 80 and 443 reachable from the internet."
    if [[ "$http" != 80 || "$https" != 443 ]]; then
      r_warn "front door on ports $http/$https: forward public 80/443 to them, or certificate issuance fails"
    fi
  fi
}

# raggie_check_port NAME PORT FLAG — sets R_PORT to a free port (asks for
# another one interactively; dies under --yes unless --skip-checks).
raggie_check_port() {
  local name="$1" port="$2" flag="$3"
  if ! [[ "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    r_die "$flag must be a TCP port (got '$port')"
  fi
  while r_port_in_use "$port"; do
    if [[ "${RAGGIE_SKIP_CHECKS:-0}" == 1 ]]; then
      r_warn "$name port $port is already in use (continuing: --skip-checks)"
      break
    fi
    if ! r_can_prompt; then
      r_die "$name port $port is already in use; free it or pass $flag N"
    fi
    r_warn "$name port $port is already in use"
    port=""
    r_ask port "Another $name port for the front door" "" "$flag"
    [[ "$port" =~ ^[0-9]+$ ]] || r_die "$flag must be a TCP port"
  done
  R_PORT="$port"
}

raggie_url_host() { # URL -> host (no scheme, port or path)
  local u="${1#*://}"
  u="${u%%/*}"
  printf '%s' "${u%:*}"
}

raggie_guess_host() {
  local h=""
  if command -v ip >/dev/null 2>&1; then
    h="$(ip route get 1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
  fi
  if [[ -z "$h" ]] && command -v hostname >/dev/null 2>&1; then
    h="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  printf '%s' "${h:-localhost}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  # shellcheck source=deploy/scripts/lib.sh
  . "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
  r_parse_flags "$@"
  if [[ "${RAGGIE_HELP:-0}" == 1 ]]; then r_usage "$0"; exit 0; fi
  root="${RAGGIE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  raggie_env_init "$root"
  raggie_env_secrets "$root/.env"
  raggie_env_site "$root/.env"
  r_print_changes
fi

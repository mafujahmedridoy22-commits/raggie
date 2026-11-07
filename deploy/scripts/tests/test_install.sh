# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash
# web/public/install.sh helpers that run without Docker or network.

load_installer() {
  # shellcheck source=../../../web/public/install.sh
  RAGGIE_INSTALL_SOURCED=1 . "$REPO_ROOT/web/public/install.sh"
}

test_release_bundle_sha256_is_verified() {
  load_installer
  printf 'bundle' >"$T/raggie-1.2.3.tar.gz"
  printf '%s  raggie-1.2.3.tar.gz\n%s  install.sh\n' "$(i_sha256 "$T/raggie-1.2.3.tar.gz")" "$(printf '0%.0s' {1..64})" >"$T/SHA256SUMS"
  i_verify_sha256 "$T/raggie-1.2.3.tar.gz" "$T/SHA256SUMS" raggie-1.2.3.tar.gz
  printf 'tampered' >"$T/raggie-1.2.3.tar.gz"
  expect_fail i_verify_sha256 "$T/raggie-1.2.3.tar.gz" "$T/SHA256SUMS" raggie-1.2.3.tar.gz
  assert_contains "$FAIL_OUT" "sha256 mismatch"
  expect_fail i_verify_sha256 "$T/raggie-1.2.3.tar.gz" "$T/SHA256SUMS" raggie-9.9.9.tar.gz
  assert_contains "$FAIL_OUT" "no sha256 for raggie-9.9.9.tar.gz"
}

test_default_version_is_pinned_not_main() {
  load_installer
  assert_match "$RAGGIE_DEFAULT_VERSION" '^[0-9]+\.[0-9]+\.[0-9]+'
  assert_not_contains "$(cat "$REPO_ROOT/web/public/install.sh")" "refs/heads/main"
}

test_images_override_pins_release_and_embeddings_variant() {
  load_installer
  i_write_images_override "$T/images.yml" 1.2.3 slim
  local f
  f="$(cat "$T/images.yml")"
  assert_contains "$f" "image: ghcr.io/alicangunduz/raggie-edge:1.2.3"
  assert_contains "$f" "image: ghcr.io/alicangunduz/raggie-embeddings:1.2.3-slim"
  assert_contains "$f" "image: ghcr.io/alicangunduz/raggie-workers:1.2.3"
  assert_eq "$(grep -c 'build: !reset null' "$T/images.yml")" 8
  assert_not_contains "$f" ":latest"
}

test_compose_wrapper_layers_demo_overlay_for_mock() {
  load_installer
  mkdir -p "$T/deploy/compose"
  RAGGIE_LLM=mock i_write_state "$T" 1
  local w
  w="$(cat "$T/.raggie/compose")"
  assert_contains "$w" "docker-compose.demo.yml"
  assert_contains "$w" "--profile mock"
  assert_contains "$w" ".raggie/docker-compose.images.yml"
  assert_contains "$(cat "$T/.raggie/state.env")" "RAGGIE_IMAGES=published"
  RAGGIE_LLM=openai i_write_state "$T" 0
  w="$(cat "$T/.raggie/compose")"
  assert_not_contains "$w" "--profile mock"
  assert_not_contains "$w" "docker-compose.images.yml"
  assert_not_contains "$w" "docker-compose.ollama.yml"
  RAGGIE_LLM=ollama RAGGIE_GPU=1 i_write_state "$T" 1
  w="$(cat "$T/.raggie/compose")"
  assert_contains "$w" "docker-compose.ollama.yml"
  assert_contains "$w" "docker-compose.gpu.yml"
  assert_not_contains "$w" "--profile mock"
  RAGGIE_LLM=ollama RAGGIE_GPU=0 i_write_state "$T" 1
  assert_not_contains "$(cat "$T/.raggie/compose")" "docker-compose.gpu.yml"
}

test_resource_check_is_mode_dependent() {
  load_installer
  r_mem_mb() { echo 5120; }      # 5 GB
  r_disk_free_mb() { echo 51200; }
  i_check_resources cloud "$T" 2>/dev/null
  expect_fail i_check_resources local "$T"
  assert_contains "$FAIL_OUT" "use cloud embeddings"
  RAGGIE_SKIP_CHECKS=1 i_check_resources local "$T" 2>/dev/null
  # The bundled Ollama model needs its own ~4 GB on top.
  RAGGIE_LLM=ollama expect_fail i_check_resources cloud "$T"
  assert_contains "$FAIL_OUT" "Ollama"
}

test_help_lists_every_flag_the_parser_accepts() {
  load_installer
  local help flag
  help="$(usage)"
  for flag in $R_BOOL_FLAGS $R_VALUE_FLAGS; do
    assert_contains "$help" "--$flag" "--help is missing --$flag"
  done
}

# The whole wizard in checkout mode, configure-only (no Docker): first run,
# then a re-run that must be an upgrade.
test_main_source_mode_no_start_end_to_end() {
  local repo="$T/repo" out out2 token
  mkdir -p "$repo/deploy/compose"
  cp "$T/.env.example" "$T/.env.embeddings.example" "$repo/"
  cp -R "$SCRIPTS_DIR" "$repo/deploy/scripts"
  local -a args=(--source "$repo" --yes --no-start --skip-checks --embeddings local --llm mock
    --public-url http://h.example --locale tr)
  out="$(RAGGIE_SKIP_PREFLIGHT=1 bash "$REPO_ROOT/web/public/install.sh" "${args[@]}" 2>&1)"
  token="$(r_env_get "$repo/.env" SETUP_TOKEN)"
  assert_match "$token" '^[0-9a-f]{64}$'
  assert_contains "$out" "Onboarding:   http://h.example/onboarding"
  assert_contains "$out" "Setup token:  $token"
  assert_eq "$(r_env_get "$repo/.env" NEXT_PUBLIC_DEFAULT_LOCALE)" tr
  assert_contains "$(cat "$repo/.raggie/compose")" "--profile mock"
  assert_contains "$(cat "$repo/.raggie/state.env")" "RAGGIE_IMAGES=build"
  out2="$(RAGGIE_SKIP_PREFLIGHT=1 bash "$REPO_ROOT/web/public/install.sh" --source "$repo" --yes --no-start --skip-checks 2>&1)"
  assert_contains "$out2" "upgrade mode"
  assert_contains "$out2" "configuration unchanged"
  assert_eq "$(r_env_get "$repo/.env" SETUP_TOKEN)" "$token" "re-run rotated the setup token"
  # Unknown flags are rejected before anything is written.
  expect_fail env RAGGIE_SKIP_PREFLIGHT=1 bash "$REPO_ROOT/web/public/install.sh" --source "$repo" --yes --embedings cloud
  assert_contains "$FAIL_OUT" "unknown option: --embedings"
}

# `--no-start` only writes configuration: a stopped Docker daemon is a warning.
test_configure_only_preflight_does_not_need_a_running_docker() {
  load_installer
  mkdir -p "$T/bin"
  # shellcheck disable=SC2016 # the fake's own $1/$2
  printf '%s\n' '#!/bin/sh' 'case "$1 $2" in "compose version") echo 2.30.0 ;; *) exit 1 ;; esac' >"$T/bin/docker"
  chmod +x "$T/bin/docker"
  PATH="$T/bin:$PATH"
  expect_fail i_preflight 0
  assert_contains "$FAIL_OUT" "cannot talk to the Docker daemon"
  local out
  out="$(i_preflight 1 2>&1)" || fail_test "configure-only preflight failed: $out"
  assert_contains "$out" "Docker daemon is not reachable"
}

test_version_and_ref_are_validated_before_use() {
  load_installer
  i_check_version_ref 1.2.3 ""
  i_check_version_ref 1.2.3-rc.1 ""
  i_check_version_ref 1.2.3 feature/my-branch_v2.0
  i_check_version_ref 1.2.3 0123abcdef
  local v
  for v in "1.2" "latest" "1.2.3/../../x" "1.2.3;rm" "" "1.2.3 "; do
    expect_fail i_check_version_ref "$v" ""
    assert_contains "$FAIL_OUT" "--version"
  done
  for v in "../main" "main/../../x" "a b" "x;y" "-main" 'v$(id)' "a..b"; do
    expect_fail i_check_version_ref 1.2.3 "$v"
    assert_contains "$FAIL_OUT" "--ref"
  done
}

# ── --dry-run (issue #23) ───────────────────────────────────────────────────

# r_env_set records the planned change but writes nothing under --dry-run.
test_dry_run_r_env_set_does_not_write() {
  load_installer
  . "$SCRIPTS_DIR/lib.sh"
  printf 'A=old\n' >"$T/.env"
  RAGGIE_DRY_RUN=1 r_env_set "$T/.env" A new
  assert_eq "$(r_env_get "$T/.env" A)" old "dry-run modified the file"
  [[ "${R_CHANGES[*]}" == *"A old -> new"* ]] || fail_test "dry-run did not record the change"
}

# r_env_backup copies nothing under --dry-run.
test_dry_run_r_env_backup_copies_nothing() {
  load_installer
  . "$SCRIPTS_DIR/lib.sh"
  printf 'x=1\n' >"$T/.env"
  RAGGIE_DRY_RUN=1 r_env_backup "$T/.env" >/dev/null
  assert_eq "$(ls "$T") " ".env " "dry-run created a backup file"
}

# The full wizard under --dry-run prints planned changes and writes nothing.
test_main_source_mode_dry_run_writes_nothing() {
  local repo="$T/repo" out
  mkdir -p "$repo/deploy/compose"
  cp "$T/.env.example" "$T/.env.embeddings.example" "$repo/"
  cp -R "$SCRIPTS_DIR" "$repo/deploy/scripts"
  out="$(RAGGIE_SKIP_PREFLIGHT=1 bash "$REPO_ROOT/web/public/install.sh" --source "$repo" --yes --dry-run --skip-checks --embeddings local --llm mock --public-url http://h.example 2>&1)"
  assert_contains "$out" "configuration changes"
  assert_not_exists "$repo/.env"
  assert_not_exists "$repo/.env.embeddings"
  assert_not_exists "$repo/.raggie/docker-compose.images.yml"
  assert_not_exists "$repo/.raggie/compose"
  assert_not_contains "$out" "Onboarding:"
}

assert_not_exists() { [[ ! -e "$1" ]] || fail_test "$1 unexpectedly exists"; }

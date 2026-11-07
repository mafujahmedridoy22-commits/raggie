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

# ── distro-specific missing-requirement hints (issue #24) ───────────────────

# i_distro_pkg_mgr maps each distro ID to its package manager.
test_distro_pkg_mgr_matches_os_release() {
  load_installer
  local d
  for d in "debian:apt-get" "ubuntu:apt-get" "rhel:dnf" "rocky:dnf" "alma:dnf" "centos:dnf" "fedora:dnf" "suse:zypper" "opensuse-leap:zypper" "sles:zypper"; do
    local id want
    IFS=: read -r id want <<<"$d"
    printf 'ID=%s\n' "$id" >"$T/os-release"
    assert_eq "$(RAGGIE_OS_RELEASE="$T/os-release" i_distro_pkg_mgr)" "$want" "pkg mgr for $id"
  done
}

# Unknown or missing os-release means no hint.
test_distro_pkg_mgr_unknown_is_empty() {
  load_installer
  printf 'ID=customdistro\n' >"$T/os-release"
  assert_eq "$(RAGGIE_OS_RELEASE="$T/os-release" i_distro_pkg_mgr; echo rc=$?)" "rc=1" "unknown distro must fail"
  assert_eq "$(RAGGIE_OS_RELEASE="$T/missing" i_distro_pkg_mgr; echo rc=$?)" "rc=1" "missing os-release must fail"
}

# The hint name the right install command for the tool on that distro.
test_install_hint_uses_distro_package_manager() {
  load_installer
  printf 'ID=ubuntu\n' >"$T/os-release"
  assert_eq "$(RAGGIE_OS_RELEASE="$T/os-release" i_install_hint docker-ce)" "sudo apt-get install -y docker-ce"
  assert_eq "$(RAGGIE_OS_RELEASE="$T/os-release" i_install_hint curl)" "sudo apt-get install -y curl"
  printf 'ID=fedora\n' >"$T/os-release"
  assert_eq "$(RAGGIE_OS_RELEASE="$T/os-release" i_install_hint docker-compose-plugin)" "sudo dnf install -y docker-compose-plugin"
  printf 'ID=opensuse-leap\n' >"$T/os-release"
  assert_eq "$(RAGGIE_OS_RELEASE="$T/os-release" i_install_hint tar)" "sudo zypper install -y tar"
}

# i_preflight reports the distro install command when Docker is missing.
test_preflight_hints_install_command_when_docker_missing() {
  load_installer
  mkdir -p "$T/etc" "$T/bin"
  printf 'ID=ubuntu\n' >"$T/etc/os-release"
  # Fake PATH: uname + curl + tar + sha256sum exist, docker does not.
  printf '%s\n' '#!/bin/sh' 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac' >"$T/bin/uname"
  printf '%s\n' '#!/bin/sh' 'exit 0' >"$T/bin/curl"
  printf '%s\n' '#!/bin/sh' 'exit 0' >"$T/bin/tar"
  printf '%s\n' '#!/bin/sh' 'exit 0' >"$T/bin/sha256sum"
  chmod +x "$T/bin"/*
  # A docker shim that only answers to `compose version`; docker itself is
  # present, so the missing-tool branch is about the compose plugin.
  printf '%s\n' '#!/bin/sh' 'case "$1 $2" in "compose version") exit 0 ;; *) exit 1 ;; esac' >"$T/bin/docker"
  chmod +x "$T/bin/docker"
  # Verify the docker-missing branch directly: hide docker from PATH.
  ( PATH="$T/bin2" ; mkdir -p "$T/bin2" ; for c in uname curl tar sha256sum; do ln -s "$T/bin/$c" "$T/bin2/$c"; done
    RAGGIE_OS_RELEASE="$T/etc/os-release" expect_fail env PATH="$T/bin2" i_preflight 0 )
  assert_contains "$FAIL_OUT" "apt-get install -y docker-ce"
}

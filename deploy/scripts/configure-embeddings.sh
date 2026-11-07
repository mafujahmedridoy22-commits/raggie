#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Embedding / reranker mode for a Raggie install (plan Faz 2.4). Shared by
# web/public/install.sh and `make setup`; runnable alone to switch modes:
#
#   deploy/scripts/configure-embeddings.sh [--embeddings local|cloud]
#       [--embeddings-preset local|openai|jina|voyage|mistral|custom]
#       [--embeddings-api-key KEY] [--embeddings-base-url URL]
#       [--embeddings-model ID] [--embedding-dim N]
#       [--embeddings-query-prefix P] [--embeddings-passage-prefix P]
#       [--embeddings-request-extra-json J] [--embeddings-query-extra-json J]
#       [--embeddings-passage-extra-json J]
#       [--reranker-preset local|none|cohere|jina|voyage|tei|cohere-compatible]
#       [--reranker-api-key KEY] [--reranker-url URL] [--reranker-model ID]
#       [--accept-data-transfer] [--skip-probe] [--yes]
#
# Settings go to .env; provider keys ONLY to .env.embeddings (mode 600), which
# compose hands to the embeddings service alone. Cloud presets are probed live
# (dimension, query/passage asymmetry, rerank) before anything is written.

RAGGIE_E5_MODEL="intfloat/multilingual-e5-large"
RAGGIE_LOCAL_RERANKER="Alibaba-NLP/gte-multilingual-reranker-base"

# raggie_embeddings_preset NAME — sets E_BASE E_MODEL E_DIM E_REQ E_QRY E_PSG
# E_RERANK (default reranker preset) for a dense preset.
raggie_embeddings_preset() {
  E_BASE="" E_MODEL="" E_DIM=1024 E_REQ="" E_QRY="" E_PSG="" E_RERANK=none
  case "$1" in
    local) E_MODEL="$RAGGIE_E5_MODEL" E_RERANK=local ;;
    openai) E_BASE="https://api.openai.com/v1" E_MODEL="text-embedding-3-large" E_REQ='{"dimensions":1024}' E_RERANK=local ;;
    jina) E_BASE="https://api.jina.ai/v1" E_MODEL="jina-embeddings-v3" E_REQ='{"dimensions":1024}'
      E_QRY='{"task":"retrieval.query"}' E_PSG='{"task":"retrieval.passage"}' E_RERANK=jina ;;
    voyage) E_BASE="https://api.voyageai.com/v1" E_MODEL="voyage-3.5"
      E_QRY='{"input_type":"query"}' E_PSG='{"input_type":"document"}' E_RERANK=voyage ;;
    mistral) E_BASE="https://api.mistral.ai/v1" E_MODEL="mistral-embed" E_RERANK=local ;;
    custom) E_DIM="" E_RERANK=none ;;
    *) r_die "unknown embeddings preset: $1" ;;
  esac
}

# raggie_reranker_preset NAME — sets R_PROVIDER R_URL R_MODEL (R_URL empty =
# ask the user for it).
raggie_reranker_preset() {
  R_PROVIDER="$1" R_URL="" R_MODEL=""
  case "$1" in
    local) R_MODEL="$RAGGIE_LOCAL_RERANKER" ;;
    none) ;;
    cohere) R_URL="https://api.cohere.com/v2/rerank" R_MODEL="rerank-v3.5" ;;
    jina) R_PROVIDER=cohere R_URL="https://api.jina.ai/v1/rerank" R_MODEL="jina-reranker-v2-base-multilingual" ;;
    voyage) R_URL="https://api.voyageai.com/v1/rerank" R_MODEL="rerank-2.5" ;;
    tei) ;;
    cohere-compatible) R_PROVIDER=cohere ;;
    *) r_die "unknown reranker preset: $1" ;;
  esac
}

raggie_reranker_is_cloud() { [[ "$1" != local && "$1" != none ]]; }

# raggie_embeddings_ensure_secret ENV — the gateway requires the internal bearer
# once an upstream (cloud dense or a remote reranker) sits behind it. Generate
# INTERNAL_API_SECRET when it is empty/placeholder; returns 0 only when it did
# (every service must then be restarted to pick it up).
raggie_embeddings_ensure_secret() {
  local env="$1" remote=0
  [[ "$(r_env_get "$env" EMBEDDINGS_PROVIDER)" == openai_compatible ]] && remote=1
  if [[ "$(r_env_get "$env" RERANKER_ENABLED)" != false ]]; then
    case "$(r_env_get "$env" RERANKER_PROVIDER)" in cohere | voyage | tei) remote=1 ;; esac
  fi
  ((remote)) || return 1
  r_is_placeholder "$(r_env_get "$env" INTERNAL_API_SECRET)" || return 1
  r_env_set "$env" INTERNAL_API_SECRET "$(r_rand_hex 32)"
}

# Typed KVKK/GDPR consent before any text leaves the server.
raggie_data_transfer_consent() {
  local what="$1" answer=""
  if [[ "${RAGGIE_ACCEPT_DATA_TRANSFER:-0}" == 1 ]]; then return 0; fi
  if ! r_can_prompt; then
    r_die "$what sends document text and questions to a third party. Pass --accept-data-transfer (RAGGIE_ACCEPT_DATA_TRANSFER=1) to confirm you have a legal basis / DPA (KVKK/GDPR)."
  fi
  r_say ""
  r_say "⚠  $what: document text and user questions are sent to a third-party provider."
  r_say "   KVKK/GDPR: you need a legal basis / data-processing agreement for that transfer."
  r_say "   Belge metinleri ve sorular üçüncü taraf bir sağlayıcıya gönderilir (KVKK/GDPR)."
  while :; do
    printf '   Type "accept" or "kabul" to continue (anything else aborts): ' >"$RAGGIE_TTY"
    IFS= read -r answer <"$RAGGIE_TTY" || answer=""
    case "$answer" in
      accept | kabul) RAGGIE_ACCEPT_DATA_TRANSFER=1; return 0 ;;
      *) r_die "cloud mode not confirmed; nothing was changed" ;;
    esac
  done
}

# ── live probe (curl + python3) ──────────────────────────────────────────────
R_PROBE_PY='
import json, math, sys

cmd = sys.argv[1]

def merged(*objs):
    out = {}
    for o in objs:
        if o:
            out.update(json.loads(o))
    return out

if cmd == "embed-body":          # model req_extra kind_extra text...
    body = merged(sys.argv[3], sys.argv[4])
    body.update({"model": sys.argv[2], "input": sys.argv[5:]})
    print(json.dumps(body))
elif cmd == "embed-check":       # dim query_file passage_file
    dim = int(sys.argv[2])
    def vecs(path):
        data = json.load(open(path))["data"]
        return [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]
    q = vecs(sys.argv[3])[0]
    rel, unrel = vecs(sys.argv[4])[:2]
    for v in (q, rel, unrel):
        if len(v) != dim:
            print(f"dimension {len(v)} returned, {dim} configured"); sys.exit(2)
    def cos(a, b):
        na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(x * x for x in b))
        return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0
    cr, cu = cos(q, rel), cos(q, unrel)
    print(f"dim {dim}, cos(query, relevant)={cr:.3f} > cos(query, unrelated)={cu:.3f}")
    sys.exit(0 if cr > cu else 3)
elif cmd == "rerank-body":       # dialect model query doc...
    dialect, model, query, docs = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]
    if dialect == "tei":
        print(json.dumps({"query": query, "texts": docs, "truncate": True}))
    else:
        key = "top_k" if dialect == "voyage" else "top_n"
        print(json.dumps({"model": model, "query": query, "documents": docs, key: len(docs)}))
elif cmd == "rerank-check":      # dialect file
    payload = json.load(open(sys.argv[3]))
    if sys.argv[2] == "tei":
        rows, key = payload, "score"
    elif sys.argv[2] == "voyage":
        rows, key = payload["data"], "relevance_score"
    else:
        rows, key = payload["results"], "relevance_score"
    scores = {int(r["index"]): float(r[key]) for r in rows}
    print(f"rerank relevant={scores[0]:.3f} unrelated={scores[1]:.3f}")
    sys.exit(0 if scores[0] > scores[1] else 3)
'

R_PROBE_QUERY="How many vacation days do employees get per year?"
R_PROBE_RELEVANT="Employees get 20 paid vacation days per year."
R_PROBE_UNRELATED="The cafeteria serves lentil soup on Fridays."

r_probe_py() { python3 -c "$R_PROBE_PY" "$@"; }

# raggie_embeddings_probe — embed one query and two passages through the cloud
# endpoint; returns 1 (with a message) if anything is off.
raggie_embeddings_probe() {
  local body qf pf msg rc=0
  body="$(mktemp)" qf="$(mktemp)" pf="$(mktemp)"
  r_probe_py embed-body "$E_MODEL" "$E_REQ" "$E_QRY" "${E_QPREFIX:-}$R_PROBE_QUERY" >"$body"
  r_http POST "$E_BASE/embeddings" "$E_KEY" "$body"
  if [[ "$R_HTTP_STATUS" != 200 ]]; then
    r_warn "embeddings probe: POST $E_BASE/embeddings -> HTTP $R_HTTP_STATUS ${R_HTTP_BODY:0:200}"
    rm -f "$body" "$qf" "$pf"
    return 1
  fi
  printf '%s' "$R_HTTP_BODY" >"$qf"
  r_probe_py embed-body "$E_MODEL" "$E_REQ" "$E_PSG" "${E_PPREFIX:-}$R_PROBE_RELEVANT" "${E_PPREFIX:-}$R_PROBE_UNRELATED" >"$body"
  r_http POST "$E_BASE/embeddings" "$E_KEY" "$body"
  if [[ "$R_HTTP_STATUS" != 200 ]]; then
    r_warn "embeddings probe (passages): HTTP $R_HTTP_STATUS ${R_HTTP_BODY:0:200}"
    rm -f "$body" "$qf" "$pf"
    return 1
  fi
  printf '%s' "$R_HTTP_BODY" >"$pf"
  msg="$(r_probe_py embed-check "$E_DIM" "$qf" "$pf" 2>&1)" || rc=$?
  rm -f "$body" "$qf" "$pf"
  case "$rc" in
    0) r_log "embeddings probe ok: $msg" ;;
    3) r_warn "embeddings probe: $msg — the query/passage settings (input_type/task, prefixes) look wrong"; return 1 ;;
    *) r_warn "embeddings probe: ${msg:-unexpected response}"; return 1 ;;
  esac
}

raggie_reranker_probe() {
  local body f msg rc=0 dialect="$R_PROVIDER"
  body="$(mktemp)" f="$(mktemp)"
  r_probe_py rerank-body "$dialect" "$R_MODEL" "$R_PROBE_QUERY" "$R_PROBE_RELEVANT" "$R_PROBE_UNRELATED" >"$body"
  r_http POST "$R_URL" "$R_KEY" "$body"
  if [[ "$R_HTTP_STATUS" != 200 ]]; then
    r_warn "reranker probe: POST $R_URL -> HTTP $R_HTTP_STATUS ${R_HTTP_BODY:0:200}"
    rm -f "$body" "$f"
    return 1
  fi
  printf '%s' "$R_HTTP_BODY" >"$f"
  msg="$(r_probe_py rerank-check "$dialect" "$f" 2>&1)" || rc=$?
  rm -f "$body" "$f"
  case "$rc" in
    0) r_log "reranker probe ok: $msg" ;;
    3) r_warn "reranker probe: $msg (relevant document not ranked first)"; return 1 ;;
    *) r_warn "reranker probe: ${msg:-unexpected response}"; return 1 ;;
  esac
}

# Run a probe; on failure ask to retry/continue, or die under --yes.
raggie_probe_or_decide() {
  local fn="$1"
  if [[ "${RAGGIE_SKIP_PROBE:-0}" == 1 ]]; then
    r_warn "live probe skipped (--skip-probe)"
    return 0
  fi
  if ! r_have_python; then
    r_warn "python3 not found: skipping the live probe; check the embeddings service logs after start-up"
    return 0
  fi
  "$fn" && return 0
  if r_can_prompt && r_confirm "Probe failed. Write this configuration anyway?"; then
    return 0
  fi
  r_die "probe failed; nothing was changed (fix the settings, or pass --skip-probe)"
}

# ── main flow ────────────────────────────────────────────────────────────────
# raggie_embeddings_configure ROOT — interactive/flag-driven; writes .env and
# .env.embeddings and exports RAGGIE_EMBEDDINGS_VARIANT (local|slim).
raggie_embeddings_configure() {
  local root="$1" env="$1/.env" keys="$1/.env.embeddings" mem_mb provider_old model_old dim_old
  provider_old="$(r_env_get "$env" EMBEDDINGS_PROVIDER)"
  model_old="$(r_env_get "$env" EMBEDDINGS_MODEL)"
  dim_old="$(r_env_get "$env" EMBEDDING_DIM)"

  if [[ "${RAGGIE_SETUP_MODE:-}" == upgrade && "${RAGGIE_RECONFIGURE:-0}" != 1 && -z "${RAGGIE_EMBEDDINGS:-}${RAGGIE_EMBEDDINGS_PRESET:-}" ]]; then
    r_log "embeddings: keeping ${provider_old:-local} / ${model_old:-$RAGGIE_E5_MODEL} (--reconfigure to change)"
    raggie_embeddings_variant "$(r_env_get "$env" EMBEDDINGS_BAKE)"
    return 0
  fi

  mem_mb="$(r_mem_mb)"
  if [[ -z "${RAGGIE_EMBEDDINGS:-}" && -n "${RAGGIE_EMBEDDINGS_PRESET:-}" ]]; then
    [[ "$RAGGIE_EMBEDDINGS_PRESET" == local ]] && RAGGIE_EMBEDDINGS=local || RAGGIE_EMBEDDINGS=cloud
  fi
  if ((mem_mb > 0 && mem_mb < 7680)); then
    r_warn "this host has $((mem_mb / 1024)) GB RAM; LOCAL embeddings need ~4 GB on top of the stack (8 GB+ recommended). CLOUD mode is lighter."
  fi
  r_choose RAGGIE_EMBEDDINGS "Embedding mode / Gömme modu:" local \
    "local|LOCAL — data never leaves this server. ~4 GB extra RAM." \
    "cloud|CLOUD — lighter/faster. Document text & questions go to a third party (KVKK/GDPR)."

  local preset
  if [[ "$RAGGIE_EMBEDDINGS" == local ]]; then
    preset=local
    [[ -z "${RAGGIE_EMBEDDINGS_PRESET:-}" || "$RAGGIE_EMBEDDINGS_PRESET" == local ]] ||
      r_die "--embeddings local cannot use preset $RAGGIE_EMBEDDINGS_PRESET"
  else
    raggie_data_transfer_consent "CLOUD embeddings"
    RAGGIE_EMBEDDINGS_PRESET="${RAGGIE_EMBEDDINGS_PRESET:-}"
    if [[ -z "$RAGGIE_EMBEDDINGS_PRESET" ]] && ! r_can_prompt; then
      r_die "--embeddings cloud needs --embeddings-preset (openai|jina|voyage|mistral|custom)"
    fi
    r_choose RAGGIE_EMBEDDINGS_PRESET "Cloud embedding provider:" openai \
      "openai|OpenAI text-embedding-3-large (1024-d)" \
      "jina|Jina jina-embeddings-v3 (1024-d)" \
      "voyage|Voyage voyage-3.5 (1024-d)" \
      "mistral|Mistral mistral-embed (1024-d)" \
      "custom|Custom OpenAI-compatible endpoint (TEI / vLLM / Ollama / other)"
    preset="$RAGGIE_EMBEDDINGS_PRESET"
    [[ "$preset" != local ]] || r_die "preset local needs --embeddings local"
  fi
  raggie_embeddings_preset "$preset"

  # Explicit flags override preset values.
  E_BASE="${RAGGIE_EMBEDDINGS_BASE_URL:-$E_BASE}"
  E_MODEL="${RAGGIE_EMBEDDINGS_MODEL:-$E_MODEL}"
  E_DIM="${RAGGIE_EMBEDDING_DIM:-$E_DIM}"
  E_REQ="${RAGGIE_EMBEDDINGS_REQUEST_EXTRA_JSON-$E_REQ}"
  E_QRY="${RAGGIE_EMBEDDINGS_QUERY_EXTRA_JSON-$E_QRY}"
  E_PSG="${RAGGIE_EMBEDDINGS_PASSAGE_EXTRA_JSON-$E_PSG}"
  E_QPREFIX="${RAGGIE_EMBEDDINGS_QUERY_PREFIX:-}"
  E_PPREFIX="${RAGGIE_EMBEDDINGS_PASSAGE_PREFIX:-}"
  E_KEY="${RAGGIE_EMBEDDINGS_API_KEY:-}"
  if [[ "$preset" != local ]]; then
    r_ask E_BASE "Embeddings base URL (OpenAI-compatible, the gateway POSTs {base}/embeddings)" "" --embeddings-base-url
    r_ask E_MODEL "Embedding model id" "" --embeddings-model
    r_ask E_DIM "Embedding dimension" "" --embedding-dim
    E_BASE="${E_BASE%/}"
    if [[ "$preset" == custom ]]; then
      r_ask_secret E_KEY "Embeddings API key (empty if the endpoint needs none)" --embeddings-api-key optional
    else
      r_ask_secret E_KEY "$preset API key" --embeddings-api-key
    fi
  fi
  [[ "$E_DIM" =~ ^[1-9][0-9]*$ ]] || r_die "embedding dimension must be a positive integer (got '$E_DIM')"
  local j
  for j in "$E_REQ" "$E_QRY" "$E_PSG"; do
    r_json_object "$j" || r_die "extra JSON must be a JSON object: $j"
  done

  # Reranker.
  local -a rr_options=("local|Local cross-encoder (gte-multilingual-reranker-base, CPU)" "none|No reranking")
  if [[ "$RAGGIE_EMBEDDINGS" == cloud ]]; then
    rr_options+=("cohere|Cohere rerank-v3.5" "jina|Jina jina-reranker-v2-base-multilingual"
      "voyage|Voyage rerank-2.5" "tei|Self-hosted TEI /rerank" "cohere-compatible|Other Cohere-compatible /rerank")
  elif [[ -n "${RAGGIE_RERANKER_PRESET:-}" ]] && raggie_reranker_is_cloud "$RAGGIE_RERANKER_PRESET"; then
    raggie_data_transfer_consent "A cloud reranker"
    rr_options+=("$RAGGIE_RERANKER_PRESET|$RAGGIE_RERANKER_PRESET")
  fi
  r_choose RAGGIE_RERANKER_PRESET "Reranker:" "$E_RERANK" "${rr_options[@]}"
  raggie_reranker_preset "$RAGGIE_RERANKER_PRESET"
  R_URL="${RAGGIE_RERANKER_URL:-$R_URL}"
  R_MODEL="${RAGGIE_RERANKER_MODEL:-$R_MODEL}"
  R_KEY="${RAGGIE_RERANKER_API_KEY:-}"
  if raggie_reranker_is_cloud "$RAGGIE_RERANKER_PRESET"; then
    r_ask R_URL "Reranker endpoint URL (full /rerank URL)" "" --reranker-url
    [[ "$R_PROVIDER" == tei ]] || r_ask R_MODEL "Reranker model id" "" --reranker-model
    # Same vendor for dense and rerank: reuse the key unless one was given.
    if [[ -z "$R_KEY" && "$RAGGIE_RERANKER_PRESET" == "$preset" ]]; then R_KEY="$E_KEY"; fi
    if [[ "$R_PROVIDER" == tei || "$RAGGIE_RERANKER_PRESET" == cohere-compatible ]]; then
      r_ask_secret R_KEY "Reranker API key (empty if none)" --reranker-api-key optional
    else
      r_ask_secret R_KEY "Reranker API key" --reranker-api-key
    fi
  fi

  # Probe before writing anything.
  if [[ "$preset" != local ]]; then
    raggie_probe_or_decide raggie_embeddings_probe
    r_note "    Cost: cloud embedding is billed per token. Rough volume: ~250 tokens per document page"
    r_note "    (10 000 pages ≈ 2.5M tokens, re-embedded on every model change) plus one short query per chat turn."
  fi
  if raggie_reranker_is_cloud "$RAGGIE_RERANKER_PRESET"; then
    raggie_probe_or_decide raggie_reranker_probe
  fi

  # A different dense profile means a new vector space.
  if [[ "${RAGGIE_SETUP_MODE:-}" == upgrade ]]; then
    local provider_new=local
    [[ "$preset" == local ]] || provider_new=openai_compatible
    if [[ "$provider_new" != "${provider_old:-local}" || "$E_MODEL" != "${model_old:-$RAGGIE_E5_MODEL}" || "$E_DIM" != "${dim_old:-1024}" ]]; then
      r_warn "the dense embedding model changes (${model_old:-?} -> $E_MODEL): existing documents must be re-embedded into a new collection."
      r_warn "after start-up: Admin → AI → Embeddings → Re-embed (or EMBEDDINGS_FINGERPRINT_POLICY=auto_reindex with EMBEDDINGS_REEMBED_CONFIRM, docs/embeddings.md)."
      if r_can_prompt && ! r_confirm "Continue with the new embedding model?"; then
        r_die "aborted; nothing was changed"
      fi
    fi
  fi

  raggie_embeddings_write "$env" "$keys" "$preset"
}

# raggie_embeddings_write ENV KEYS PRESET — persist E_* / R_* settings.
raggie_embeddings_write() {
  local env="$1" keys="$2" preset="$3" bake
  if [[ "$preset" == local ]]; then
    r_env_set "$env" EMBEDDINGS_PROVIDER local
    r_env_set "$env" EMBEDDINGS_MODEL "$E_MODEL"
    r_env_set "$env" EMBEDDING_DIM "$E_DIM"
    r_env_set "$env" EMBEDDINGS_UPSTREAM_BASE_URL ""
    bake="dense,sparse"
  else
    r_env_set "$env" EMBEDDINGS_PROVIDER openai_compatible
    r_env_set "$env" EMBEDDINGS_MODEL "$E_MODEL"
    r_env_set "$env" EMBEDDING_DIM "$E_DIM"
    r_env_set "$env" EMBEDDINGS_UPSTREAM_BASE_URL "$E_BASE"
    bake="sparse"
  fi
  r_env_set "$env" EMBEDDINGS_REQUEST_EXTRA_JSON "$E_REQ"
  r_env_set "$env" EMBEDDINGS_QUERY_EXTRA_JSON "$E_QRY"
  r_env_set "$env" EMBEDDINGS_PASSAGE_EXTRA_JSON "$E_PSG"
  [[ -z "$E_QPREFIX" ]] || r_env_set "$env" EMBEDDINGS_QUERY_PREFIX "$E_QPREFIX"
  [[ -z "$E_PPREFIX" ]] || r_env_set "$env" EMBEDDINGS_PASSAGE_PREFIX "$E_PPREFIX"

  case "$RAGGIE_RERANKER_PRESET" in
    none)
      r_env_set "$env" RERANKER_ENABLED false
      r_env_set "$env" RERANKER_PROVIDER none
      r_env_set "$env" RERANK_ENABLED false
      r_env_set "$env" RERANKER_UPSTREAM_URL ""
      ;;
    local)
      r_env_set "$env" RERANKER_ENABLED true
      r_env_set "$env" RERANKER_PROVIDER local
      r_env_set "$env" RERANKER_MODEL "$R_MODEL"
      r_env_set "$env" RERANKER_UPSTREAM_URL ""
      r_env_set "$env" RERANK_ENABLED true
      bake="$bake,rerank"
      ;;
    *)
      r_env_set "$env" RERANKER_ENABLED true
      r_env_set "$env" RERANKER_PROVIDER "$R_PROVIDER"
      [[ -z "$R_MODEL" ]] || r_env_set "$env" RERANKER_MODEL "$R_MODEL"
      r_env_set "$env" RERANKER_UPSTREAM_URL "$R_URL"
      r_env_set "$env" RERANK_ENABLED true
      ;;
  esac
  r_env_set "$env" EMBEDDINGS_BAKE "$bake"

  # Keys: only in .env.embeddings, never in the shared .env.
  if [[ ! -f "$keys" ]]; then
    if [[ "${R_DRY_RUN:-0}" == 1 ]]; then
      r_log "dry-run: would create $(basename "$keys")"
    else
      (umask 077 && : >"$keys")
      if [[ -f "$(dirname "$env")/.env.embeddings.example" ]]; then
        cat "$(dirname "$env")/.env.embeddings.example" >"$keys"
      fi
      chmod 600 "$keys"
    fi
  elif [[ "${R_DRY_RUN:-0}" != 1 ]]; then
    chmod 600 "$keys"
  fi
  r_env_set "$keys" EMBEDDINGS_UPSTREAM_API_KEY "${E_KEY:-}"
  if raggie_reranker_is_cloud "$RAGGIE_RERANKER_PRESET"; then
    r_env_set "$keys" RERANKER_UPSTREAM_API_KEY "${R_KEY:-}"
  else
    r_env_set "$keys" RERANKER_UPSTREAM_API_KEY ""
  fi
  local leaked
  for leaked in EMBEDDINGS_UPSTREAM_API_KEY RERANKER_UPSTREAM_API_KEY; do
    if [[ -n "$(r_env_get "$env" "$leaked")" ]]; then
      r_warn "$leaked is set in .env, which every container reads; move it to .env.embeddings"
    fi
  done
  raggie_embeddings_variant "$bake"
  r_log "embeddings: $(r_env_get "$env" EMBEDDINGS_PROVIDER) $E_MODEL (${E_DIM}-d), reranker $RAGGIE_RERANKER_PRESET"
}

# Prebuilt image variant: -local bakes the dense/rerank ONNX models, -slim only BM25.
raggie_embeddings_variant() {
  case "${1:-dense,sparse,rerank}" in
    *dense* | *rerank*) RAGGIE_EMBEDDINGS_VARIANT=local ;;
    *) RAGGIE_EMBEDDINGS_VARIANT=slim ;;
  esac
  export RAGGIE_EMBEDDINGS_VARIANT
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  # shellcheck source=deploy/scripts/lib.sh
  . "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
  r_parse_flags "$@"
  if [[ "${RAGGIE_HELP:-0}" == 1 ]]; then r_usage "$0"; exit 0; fi
  root="${RAGGIE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  [[ -f "$root/.env" ]] || r_die "$root/.env not found; run the installer (or make setup) first"
  RAGGIE_SETUP_MODE=upgrade RAGGIE_RECONFIGURE=1
  r_env_backup "$root/.env" >/dev/null
  [[ ! -f "$root/.env.embeddings" ]] || r_env_backup "$root/.env.embeddings" >/dev/null
  raggie_embeddings_configure "$root"
  if raggie_embeddings_ensure_secret "$root/.env"; then
    r_print_changes
    r_log "INTERNAL_API_SECRET was generated: apply with: docker compose up -d --build (every service)"
  else
    r_print_changes
    r_log "apply with: docker compose up -d --build embeddings (or re-run the installer)"
  fi
fi

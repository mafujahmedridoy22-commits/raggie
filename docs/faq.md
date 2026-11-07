# FAQ

### What is Raggie?

A self-hosted RAG (retrieval-augmented generation) platform. You upload
documents, and Raggie gives you a private chat assistant for your teams, plus an
embeddable support widget for your website. Answers cite the documents they
come from, and an experimental LLM judge scores completed answers for
faithfulness to those sources.

### What does it cost? What is the license?

Raggie is free and open source under the Apache License 2.0. You pay only for
your own infrastructure and for whatever LLM provider you choose (or nothing,
if you self-host the model).

### Can it run fully offline or air-gapped?

Yes, with:

- a self-hosted LLM behind an OpenAI-compatible API;
- the default **local** embeddings and reranker, with model files baked into the image or pre-downloaded;
- web search and MCP left off (the default).

For demos without any model, run `make up-demo`: it starts a mock LLM that
answers deterministically, with citations. For real answers without any API key,
`make up-ollama` runs a bundled Ollama with `qwen3:4b-instruct` (Apache-2.0) on
the same host. See [data-handling.md](data-handling.md#fully-on-premises-configuration).

### Which LLMs can I use?

Endpoints that speak the OpenAI-compatible `/v1/chat/completions` API, such as
a hosted provider, vLLM, Ollama, TGI or LiteLLM. Automated tests run against the
bundled mock LLM, and `make up-ollama` wires in Ollama; the others use the same
API but are not tested by the project.

- **One model:** set `LLMAAS_BASE_URL`, `LLMAAS_API_KEY` and `LLMAAS_CHAT_MODEL`.
- **Several models or providers:** use `LLM_MODEL_PROFILES_JSON` to declare each one and its capabilities (tools, vision, reasoning).

See [llm-providers.md](llm-providers.md). Ollama is used through its
OpenAI-compatible endpoint; `make up-ollama` (or `make up-ollama-gpu`) starts a
bundled one and wires the stack to it.

### Do I need a GPU?

Not for Raggie itself. Embeddings, sparse vectors and reranking run on CPU
(ONNX). Whether the LLM needs a GPU depends on the model and where it runs. A
hosted provider needs nothing local. The bundled Ollama default
(`qwen3:4b-instruct`) is small enough to answer on a CPU-only host; larger
models want `make up-ollama-gpu`. See the hardware table in the
[README](../README.md#hardware).

### Which address do I open? How do I get HTTPS?

The bundled Caddy is the single front door: open `http://localhost` (port 80,
or `HTTP_PORT`). The web app and the edge only listen on `127.0.0.1` for
debugging. On a server, set `RAGGIE_DOMAIN` (and optionally `ACME_EMAIL`) in
`.env` and Caddy serves HTTPS with an automatic Let's Encrypt certificate. The
widget is served from the same origin. See [production.md](production.md).

### Which file types are supported?

PDF, DOCX, XLSX, PPTX, TXT and Markdown, plus images (PNG, JPEG, TIFF, WebP,
BMP) via Tesseract OCR (Turkish + English). Scanned PDF pages with little or no
extractable text are OCR'd automatically. The web app uploads in 16 MiB parts,
retrying any part that fails, up to 10 GiB by default. An interrupted upload
cannot yet be resumed after a page reload.

### Which languages are supported?

The web UI is available in Turkish and English. Answers follow the language of
the question. Retrieval uses a multilingual embedding model
(`intfloat/multilingual-e5-large`).

### How do I switch embedding models? Do I need to re-embed?

Yes, switching the embedding model requires re-embedding your documents. Vectors
from different models are not comparable, so each embedding profile (provider,
model, dimension, prefixes and extra JSON; URLs and keys are excluded) has a
**fingerprint** and its own Qdrant collection — the default local e5 profile uses
`raggie_chunks_d1024`, any other profile uses `raggie_chunks_d{dim}_{fingerprint}`.
Two models with the same dimension never share a collection.

When the gateway's fingerprint differs from the one your documents were embedded
with, the control plane follows `EMBEDDINGS_FINGERPRINT_POLICY`:

| Policy | Behaviour |
|---|---|
| `block_ingest` (default) | The app stays up, new uploads are refused, and admins are told to re-embed. |
| `refuse_boot` | The control plane will not start until the mismatch is resolved. |
| `auto_reindex` | Re-embeds automatically, but only if `EMBEDDINGS_REEMBED_CONFIRM` is set to the **new** fingerprint (a deliberate confirmation). |

Re-embedding writes into the new collection; the old collection is left in place
until you delete it, so you can roll back by switching the profile back. Documents
not yet re-embedded are not searchable under the new profile, so plan a
maintenance window for large corpora. Zero-downtime migration between profiles is
planned for a later release. See
[embeddings.md](embeddings.md#changing-models-and-re-embedding) for the full
details, including how to switch the reranker alone (which never requires
re-embedding).

### How do citations and the "hallucination detector" work?

The model is given numbered sources and asked to cite them as `[1]`, `[2]`.
The UI links each citation to the source excerpt. After each answer, an
evaluation worker asks the LLM to judge whether the answer is supported by the
retrieved context (faithfulness), and checks citation validity and coverage.
Conversations below the thresholds show up under **Admin → Flagged**. This
detector is **experimental**. The judge is itself an LLM, so treat the score as
a signal, not proof: its precision and recall have not been measured against
labelled data yet. Partial answers are not scored, under heavy load only a
sample is scored, and if the judge keeps failing an answer is kept with citation
scores only.

### Is the website widget safe to put on a public site?

The widget can only chat with the single `EXTERNAL` workspace it was issued
for.

- **Approved sites only:** it gets a token only on origins you allow-list for that workspace.
- **Separate token:** it uses a different token audience and signing secret from the admin API, so it can never reach admin endpoints (every route is covered by tests).
- **Fewer features:** memory, personalization and MCP tools are never used for widget chats. Web search is available to the widget only if you turn on `WIDGET_WEB_SEARCH_ENABLED` (off by default) and enable web search for that workspace.

See [security-model.md](security-model.md#tb2--widget-visitor--edge).

### Is SSO supported?

Partly. OIDC login and SCIM provisioning exist as **experimental, API-only**
features (`OIDC_ENABLED`, `SCIM_ENABLED`, both off by default). The login page
does not have an SSO button yet.

### What is MCP support?

The edge can call tools on an MCP server. Today that means the bundled
`mcp-atlassian` server, giving read-only Jira access. It is off by default, and
write tools are filtered out unless you set `MCP_ALLOW_WRITE_TOOLS=true`. The
Jira credential is configured once per deployment and shared by all
organizations. Per-org credentials are planned.

### Can I run it on Kubernetes / in high availability?

Both are labelled **community preview**:

- **Kustomize manifests** (`deploy/k8s`, dev and prod overlays): multiple replicas for the stateless services, PodDisruptionBudgets, NetworkPolicies, and autoscaling in the prod overlay (needs KEDA). The datastores in these manifests are single-instance.
- **A Helm chart** (`deploy/helm/raggie`) with `values-single.yaml` and `values-ha.yaml`. The HA profile uses CloudNativePG for PostgreSQL, Valkey Sentinel and a 3-node Qdrant cluster.

Neither has been validated by chaos or disaster-recovery drills yet. Database
migrations are safe to run from several replicas at once (they take an advisory
lock).

### Does any of my data leave the server?

Only to the services you configure. The LLM provider always receives prompts
(question, history, retrieved chunks). Web search, cloud embeddings and MCP send
data out only if you enable them. Raggie's own services send no telemetry:
there is no analytics SDK, Next.js and Hugging Face telemetry are disabled, and
the update check runs only if you set `RELEASE_MANIFEST_URL`. The bundled
Qdrant's anonymous usage reporting is turned off
(`QDRANT__TELEMETRY_DISABLED=true`) in Docker Compose, the Kubernetes manifests
and both Helm profiles. The full list is in
[data-handling.md](data-handling.md).

### I forgot the admin password. How do I reset it?

Another administrator in the same organization can issue a temporary password
from **Admin → Organization → People → reset password**, for local accounts
that do not outrank them. A `SUPER_ADMIN` can reset anyone in the organization
except a platform admin. A `WORKSPACE_ADMIN` can reset `STAFF` and other
`WORKSPACE_ADMIN` accounts, but not a `SUPER_ADMIN`. Only a platform admin can
reset a platform admin.
There is no command-line recovery tool yet (planned). If the only platform
admin is locked out, you need direct database access to recover.

### Are the performance numbers in the docs measured?

Not yet. Login time, retrieval latency, ingest speed and concurrent streams
are design targets. The harnesses that will measure them are in
[`bench/`](../bench/) (`make bench-a2`, `bench-a3`, `bench-a5`, `eval`), with
probes and load scripts in `deploy/` and the methodology in
[benchmarks/](benchmarks/README.md). No results have been published yet; until
then, treat any number as a target, not a measurement.

### How can I contribute?

See [CONTRIBUTING.md](../CONTRIBUTING.md). Commits need a DCO sign-off
(`git commit -s`), and new UI strings go into both the Turkish and English
message files.

### How do I report a security issue?

Privately, through GitHub's private vulnerability reporting. Details are in
[SECURITY.md](../SECURITY.md). Please don't open a public issue.

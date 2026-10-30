# tfscout

**AI-assisted plan review and drift detection for Terraform / OpenTofu.**
Runs entirely in your CI: no SaaS, no server, and your plans never leave the runner unredacted.
**tfscout never touches your cloud.** It only reads the plan file your pipeline already produces.

📘 **[Setup guide](docs/setup.md)**: what you need, AWS OIDC in 10 minutes, Jenkins/GitLab, troubleshooting.

- 🔎 **`review`** reads `terraform plan` in a pull request and posts a single, auto-updated comment with what
  changes, and what can break: data loss, open security groups, IAM wildcards, disabled encryption, forced
  replacements, plus anything the AI notices.
- 🧭 **`drift`** reads `terraform plan -refresh-only` on a schedule, classifies every drifted resource as
  `noise`, `manual-change` or `risky`, explains it and suggests how to codify or revert it. Results go to one
  GitHub issue per root module, which is updated when drift changes and closed when it is resolved.

```
plan ──► redact secrets ──► deterministic rules ──► LLM (optional) ──► PR comment / issue / JSON
```

Deterministic rules are the baseline you can trust. The LLM adds a plain-English summary and extra findings,
and can escalate but **never downgrade** a risky item. With `provider: none` tfscout works without any AI.

## Example PR comment

> ### 🔎 tfscout plan review — `prod`
> ➕ create **2** · ✏️ update **2** · ♻️ replace **1**
>
> - Recreates `orders-db` in another AZ
> - Grants CI full admin
>
> | | Severity | Resource | Finding |
> |---|---|---|---|
> | 🔴 | critical | `aws_db_instance.main` | Stateful resource will be REPLACED |
> | 🔴 | critical | `aws_iam_policy.ci` | IAM policy allows Action "*" |
> | 🔴 | critical | `aws_security_group.web` | Ingress opened to the internet (22/tcp) |
> | 🟠 | high | `aws_db_instance.main` | Encryption disabled (`storage_encrypted`) |
> | 🟠 | high | `aws_db_instance.main` | AZ move forces DB recreation 🤖 |

## Quick start (GitHub Action)

Already running `terraform plan` in GitHub Actions? Add one step. No new cloud credentials are needed.
Otherwise see the [setup guide](docs/setup.md).

### Pull request review

```yaml
permissions:
  contents: read
  pull-requests: write
  id-token: write   # for cloud OIDC

steps:
  - uses: actions/checkout@v7
  - uses: hashicorp/setup-terraform@v4
    with: { terraform_wrapper: false }
  # ... configure cloud credentials ...
  - run: terraform init -input=false && terraform plan -input=false -lock=false -out=tfplan
    working-directory: envs/prod
  - uses: mikonoid/tfscout@v0
    with:
      mode: review
      working-directory: envs/prod
      plan: tfplan
      provider: gemini
      api-key: ${{ secrets.GEMINI_API_KEY }}
      fail-on: critical   # optional merge gate
```

### Scheduled drift detection

```yaml
on:
  schedule: [{ cron: "0 6 * * 1-5" }]
permissions:
  contents: read
  issues: write
  id-token: write

steps:
  # ... checkout, setup-terraform, credentials, init ...
  - run: terraform plan -refresh-only -input=false -lock=false -out=drift.tfplan
    working-directory: envs/prod
  - uses: mikonoid/tfscout@v0
    with:
      mode: drift
      working-directory: envs/prod
      plan: drift.tfplan
      provider: gemini
      api-key: ${{ secrets.GEMINI_API_KEY }}
```

Full workflows with a matrix over several directories: [examples/review.yml](examples/review.yml),
[examples/drift.yml](examples/drift.yml).
One-time AWS OIDC role for CI without stored keys: [examples/aws-oidc](examples/aws-oidc/main.tf).

### Inputs

| Input | Default | Description |
|---|---|---|
| `mode` | — | `review` or `drift` |
| `plan` | — | Binary plan (`-out`) or `terraform show -json` output, relative to `working-directory` |
| `working-directory` | `.` | Root module directory |
| `name` | working-directory | Label used in titles; keeps comments/issues per module separate |
| `provider` | `none` | `none`, `gemini`, `groq`, `openrouter`, `openai`, `ollama`, `custom` |
| `model` / `base-url` | preset | Override model (comma-separated = fallback chain on 503/429/timeout/404) / endpoint |
| `timeout` | `90` | Seconds to wait for the LLM per attempt |
| `reasoning-effort` | preset | `minimal`/`low`/`medium`/`high` for thinking models (gemini: `low`) |
| `api-key` | — | LLM API key (use a secret) |
| `comment` | `true` | Post PR comment (review) or sync the drift issue (drift) |
| `fail-on` | `none` | review: fail when a finding is at least this severity |
| `fail-on-drift` | `false` | drift: fail when actionable drift exists |
| `terraform-bin` | `terraform` | Use `tofu` for OpenTofu |

Outputs: `max-severity`, `findings-count`, `drift-detected`, `fingerprint`, `report-file`.
The report is also added to the job summary.

## LLM providers

tfscout speaks the OpenAI-compatible `/chat/completions` API, so you can use any of these:

| Provider | Cost | Key |
|---|---|---|
| `gemini` (fallback chain `gemini-flash-lite-latest` → `gemini-3.5-flash` → `gemini-flash-latest`) | free tier | [AI Studio](https://aistudio.google.com/apikey) → `GEMINI_API_KEY` |
| `groq` | free tier | `GROQ_API_KEY` |
| `openrouter` | free models available | `OPENROUTER_API_KEY` |
| `openai` | paid | `OPENAI_API_KEY` |
| `ollama` | free, local/self-hosted runner | none |
| `custom` | — | any OpenAI-compatible endpoint (Azure AI Foundry, vLLM, LM Studio…) |

## Security & privacy

- `terraform show -json` prints sensitive values **in clear text**. Before anything is sent to an LLM, tfscout
  applies Terraform's own `*_sensitive` masks and also redacts values by key name (`password`, `token`,
  `private_key`…) and by pattern (AWS keys, PEM keys, GitHub/Slack/OpenAI tokens, JWTs, DB URLs with
  credentials).
- Only redacted attribute diffs are sent, never the full state.
- `provider: none` sends nothing anywhere.
- Use read-only cloud credentials for planning. tfscout never runs `apply`.

## CLI

```bash
pip install git+https://github.com/mikonoid/tfscout   # PyPI package coming soon

terraform plan -out=tfplan
tfscout review tfplan                                  # rules only
GEMINI_API_KEY=... tfscout review tfplan --provider gemini
tfscout review plan.json --format json --fail-on high  # exit 3 on high/critical

terraform plan -refresh-only -out=drift.tfplan
tfscout drift drift.tfplan --provider ollama --exit-code   # exit 2 on actionable drift
```

Works with GitLab CI, Jenkins and locally too. Requires Python ≥ 3.10 and has no third-party dependencies.

## Roadmap

- [ ] CloudTrail attribution for drift ("who changed it and when")
- [ ] Cost delta via Infracost
- [ ] Auto-generated PR that codifies a manual change
- [ ] Custom org rules (`.tfscout.yml`: required tags, allowed regions, naming)
- [ ] GCP/Azure rule coverage

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## License

MIT

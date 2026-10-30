# tfscout

Plan review and drift detection for Terraform and OpenTofu, with optional LLM explanations.

tfscout reads the plan file your pipeline already produces and reports what can go wrong. It runs inside
your CI: there is no hosted service, it does not need cloud credentials of its own, and secrets are
redacted before anything is sent to an LLM.

It has two modes:

- `review` runs on pull requests. It posts one comment (updated on every push) listing the changes and the
  risks: resources holding data that will be replaced or deleted (and the attribute that forces it),
  ingress opened to the internet, IAM wildcards, encryption or deletion protection being turned off.
- `drift` runs on a schedule against `terraform plan -refresh-only`. Each drifted resource is classified as
  `risky`, `manual-change` or `noise`. Results go to a GitHub issue per root module, which is updated when
  the drift changes and closed once it is gone.

The checks are deterministic rules. An LLM is optional: it adds a short summary and may raise additional
findings or escalate severity, but it cannot downgrade what the rules found. Without an LLM
(`provider: none`) you get the rule-based report.

See it on a real repository: [tfscout-demo](https://github.com/mikonoid/tfscout-demo)
([example PR comment](https://github.com/mikonoid/tfscout-demo/pull/2)). Setup instructions, including
AWS OIDC and non-GitHub CI, are in the [setup guide](https://github.com/mikonoid/tfscout/blob/main/docs/setup.md).

## Quick start (GitHub Action)

If you already run `terraform plan` in GitHub Actions, add one step after it. Otherwise start with the
[setup guide](https://github.com/mikonoid/tfscout/blob/main/docs/setup.md).

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

Full workflows with a matrix over several directories: [examples/review.yml](https://github.com/mikonoid/tfscout/blob/main/examples/review.yml),
[examples/drift.yml](https://github.com/mikonoid/tfscout/blob/main/examples/drift.yml).
One-time AWS OIDC role for CI without stored keys: [examples/aws-oidc](https://github.com/mikonoid/tfscout/blob/main/examples/aws-oidc/main.tf).

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

## Security and privacy

- `terraform show -json` prints sensitive values in clear text. Before anything is sent to an LLM, tfscout
  applies Terraform's own `*_sensitive` masks and also redacts values by key name (`password`, `token`,
  `private_key`…) and by pattern (AWS keys, PEM keys, GitHub/Slack/OpenAI tokens, JWTs, DB URLs with
  credentials).
- Only redacted attribute diffs are sent, never the full state.
- `provider: none` sends nothing anywhere.
- Use read-only cloud credentials for planning. tfscout never runs `apply`.

## CLI

```bash
pipx install tfscout          # or: pip install tfscout

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

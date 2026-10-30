# Setting up tfscout

This guide takes you from zero to a tfscout comment on your next pull request, in about 10 minutes.

## How it works (and what it needs)

```
your CI runner
  1. checkout
  2. cloud credentials        ← you already have this if you run `terraform plan` in CI
  3. terraform init + plan     → plan file
  4. tfscout                   ← reads ONLY the plan file
       ├─ redacts secrets, runs rules
       ├─ (optional) sends the redacted diff to an LLM
       └─ posts a PR comment / drift issue via GITHUB_TOKEN
```

tfscout does not access your cloud. Cloud access is needed only by Terraform to produce the plan, so
tfscout reuses whatever your pipeline already has.

| What | Needed by | Required? |
|---|---|---|
| Cloud read access (AWS/GCP/Azure) | `terraform plan` | yes, but you likely have it already |
| Terraform state access | `terraform plan` | yes, same as above |
| `GITHUB_TOKEN` | tfscout → comments/issues | provided automatically by GitHub Actions |
| LLM API key | tfscout → AI summary | no, without it you get rules-only reports |

Pick your situation:

- [A. You already run `terraform plan` in GitHub Actions](#a-you-already-run-terraform-plan-in-github-actions) (2 minutes)
- [B. Terraform is not in GitHub Actions yet](#b-terraform-is-not-in-github-actions-yet) (10 minutes)
- [C. You use another CI (Jenkins, GitLab, Atlantis) or run locally](#c-other-ci-or-local)

---

## A. You already run `terraform plan` in GitHub Actions

Add one step after your plan step and the permissions it needs:

```yaml
permissions:
  contents: read
  pull-requests: write   # tfscout: PR comment
  id-token: write        # your existing OIDC login, if any

steps:
  # ... your existing checkout / credentials / init ...
  - run: terraform plan -input=false -out=tfplan
    working-directory: envs/prod

  - uses: mikonoid/tfscout@v0
    with:
      mode: review
      working-directory: envs/prod
      plan: tfplan
```

That's it: rules-only review, no extra secrets. To enable the AI summary, see [LLM setup](#llm-setup).

---

## B. Terraform is not in GitHub Actions yet

### 1. Let GitHub Actions log in to AWS (one time, no stored keys)

GitHub Actions can assume an AWS role with short-lived credentials via OIDC, so no
`AWS_ACCESS_KEY_ID` secrets are needed. An admin applies this once:

```bash
cd examples/aws-oidc
terraform init
terraform apply -var github_repo=my-org/infrastructure
# → outputs role_arn = arn:aws:iam::123456789012:role/terraform-plan-readonly
```

[examples/aws-oidc](../examples/aws-oidc/main.tf) creates the GitHub OIDC provider and a read-only role
(`ReadOnlyAccess`) that only workflows of your repository can assume. Notes:

- If the OIDC provider already exists in the account, add `-var create_oidc_provider=false`.
- If your state bucket uses a customer-managed KMS key, add `-var state_kms_key_arn=...`.
- `ReadOnlyAccess` cannot read secret values. If your plan refreshes resources such as
  `aws_secretsmanager_secret_version`, grant those extra read permissions to the role.

GCP and Azure have the same mechanism ("Workload Identity Federation"); see
[google-github-actions/auth](https://github.com/google-github-actions/auth) and
[azure/login](https://github.com/Azure/login).

### 2. Add the workflows

Copy into `.github/workflows/` and replace the role ARN and directories:

- [examples/review.yml](../examples/review.yml): review on every pull request
- [examples/drift.yml](../examples/drift.yml): nightly drift detection with issues

Both use `terraform plan -lock=false`, so the read-only role never needs to write the state lock.

---

## C. Other CI or local

tfscout is a plain Python CLI (≥ 3.10, no dependencies). Run it wherever you already have a plan:

```bash
pipx install tfscout          # or: pip install tfscout

terraform plan -out=tfplan
tfscout review tfplan                            # Markdown to stdout
tfscout review tfplan --format json -o tfscout.json
tfscout review tfplan --fail-on critical         # exit 3 → fail the build

terraform plan -refresh-only -out=drift.tfplan
tfscout drift drift.tfplan --exit-code           # exit 2 when actionable drift exists
```

You can also comment on GitHub from Jenkins or another CI. Export these and pass `--github-comment --pr N`
(or `--github-issue` for drift):

```bash
export GITHUB_TOKEN=...              # token with pull-requests:write / issues:write
export GITHUB_REPOSITORY=my-org/infrastructure
export GITHUB_API_URL=https://github.example.com/api/v3   # GitHub Enterprise Server only
tfscout review tfplan --github-comment --pr "$CHANGE_ID"
```

For GitLab, save the Markdown (`-o report.md`) and post it as an MR note with your usual tooling.

---

## LLM setup

AI is optional. It adds a plain-English summary and findings the rules don't cover. It can escalate but
never downgrade a rule finding.

1. Get a key, e.g. a free Gemini key at <https://aistudio.google.com/apikey>.
2. Add it as a repository secret, e.g. `GEMINI_API_KEY`.
3. Pass it to the action:

```yaml
  - uses: mikonoid/tfscout@v0
    with:
      mode: review
      plan: tfplan
      provider: gemini
      api-key: ${{ secrets.GEMINI_API_KEY }}
```

Other providers: `openai`, `groq`, `openrouter`, `ollama`, or `custom` with `base-url` for any
OpenAI-compatible endpoint (Azure AI Foundry, vLLM, LM Studio…). `model` accepts a comma-separated fallback
chain that is used when a model is overloaded or times out.

### What is sent to the LLM

- Only attribute-level diffs of changed resources: resource addresses, types and attribute values.
- Secrets are redacted first, using Terraform's own sensitive markers plus key-name and value-pattern
  heuristics.
- Never the state file, never credentials.

Resource names, ARNs and topology still leave your network, and free tiers of some providers may use
requests to improve their models. For company infrastructure, use `provider: none`, a self-hosted model
(`ollama`/`custom`), or a provider your organisation has approved.

---

## Permissions reference

| Mode | `GITHUB_TOKEN` permissions |
|---|---|
| `review` with `comment: true` | `pull-requests: write` |
| `drift` with `comment: true` | `issues: write` |
| `comment: false` | none (report is still written to the job summary and `report-file` output) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Resource not accessible by integration` (HTTP 403) | Add the `permissions:` block above. PRs from forks get a read-only token: use `comment: "false"` for them. |
| `--github-comment needs a pull_request event or --pr` | The review step ran on `push`; run it on `pull_request`, or pass `--pr`. |
| `terraform show -json failed` / `not found` | Run tfscout in the same job after `terraform init`, with the same `working-directory`. For OpenTofu set `terraform-bin: tofu`. |
| `AccessDenied` during `terraform plan` | The plan role lacks a read permission (e.g. `kms:Decrypt`, `secretsmanager:GetSecretValue`). This is Terraform, not tfscout. |
| `AI analysis skipped: ... 503 / no response` | The model is overloaded. Use a fallback chain, e.g. `model: gemini-flash-lite-latest,gemini-3.5-flash`, or raise `timeout`. The report still contains rule findings. |
| `HTTP 404 ... model` | The model was retired. The error lists the available models; set `model`. |
| Drift issue is never created | `drift` needs a plan made with `-refresh-only`, `issues: write`, and `comment: true`. |
| Several root modules overwrite each other's comment | Give each one a unique `name` (defaults to `working-directory`). |

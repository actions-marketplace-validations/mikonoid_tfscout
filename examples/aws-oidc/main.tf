# One-time setup that lets GitHub Actions run `terraform plan` in AWS without stored keys.
# Apply it once with admin credentials:
#   terraform init && terraform apply -var github_repo=my-org/infrastructure
#
# Creates:
#   - the GitHub OIDC identity provider (skip with create_oidc_provider = false if it already exists)
#   - a read-only role for `terraform plan` / drift detection, assumable only from the given repository

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.81"
    }
  }
}

variable "github_repo" {
  description = "Repository allowed to assume the role, e.g. \"my-org/infrastructure\""
  type        = string
}

variable "github_sub_prefix" {
  description = <<-EOT
    Repositories with immutable OIDC subjects get a `sub` like "repo:owner@123/repo@456:...".
    Copy sub_claim_prefix from `gh api repos/OWNER/REPO/actions/oidc/customization/sub` if it differs
    from "repo:OWNER/REPO". Both formats are trusted.
  EOT
  type        = string
  default     = ""
}

variable "role_name" {
  type    = string
  default = "terraform-plan-readonly"
}

variable "create_oidc_provider" {
  description = "Set to false if token.actions.githubusercontent.com is already registered in this account"
  type        = bool
  default     = true
}

variable "state_kms_key_arn" {
  description = "KMS key that encrypts the Terraform state bucket (only for customer-managed keys)"
  type        = string
  default     = ""
}

data "aws_caller_identity" "current" {}

locals {
  sub_prefixes = distinct(compact(["repo:${var.github_repo}", var.github_sub_prefix]))
  oidc_host    = "token.actions.githubusercontent.com"
  oidc_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : (
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/${local.oidc_host}"
  )
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Any branch, PR or schedule of this repository; the role is read-only.
    condition {
      test     = "StringLike"
      variable = "${local.oidc_host}:sub"
      values   = [for p in local.sub_prefixes : "${p}:*"]
    }
  }
}

resource "aws_iam_role" "plan" {
  name                 = var.role_name
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "readonly" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# ReadOnlyAccess does not include kms:Decrypt, which a CMK-encrypted state bucket needs.
resource "aws_iam_role_policy" "state_kms" {
  count = var.state_kms_key_arn == "" ? 0 : 1
  name  = "terraform-state-kms"
  role  = aws_iam_role.plan.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.state_kms_key_arn }]
  })
}

output "role_arn" {
  description = "Use as role-to-assume in aws-actions/configure-aws-credentials"
  value       = aws_iam_role.plan.arn
}

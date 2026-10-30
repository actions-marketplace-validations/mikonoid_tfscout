"""Deterministic rules. They form the trustworthy baseline; the LLM only adds context on top."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from tfscout.diff import AttrDiff, diff
from tfscout.plan import Change, Resource

SEVERITIES = ["info", "low", "medium", "high", "critical"]


def sev_rank(s: str) -> int:
    return SEVERITIES.index(s) if s in SEVERITIES else 0


@dataclass
class Finding:
    severity: str
    category: str
    resource: str
    title: str
    detail: str = ""
    recommendation: str = ""
    source: str = "rule"


STATEFUL_TYPES = {
    "aws_db_instance",
    "aws_rds_cluster",
    "aws_rds_cluster_instance",
    "aws_dynamodb_table",
    "aws_s3_bucket",
    "aws_ebs_volume",
    "aws_efs_file_system",
    "aws_elasticache_cluster",
    "aws_elasticache_replication_group",
    "aws_opensearch_domain",
    "aws_elasticsearch_domain",
    "aws_docdb_cluster",
    "aws_neptune_cluster",
    "aws_redshift_cluster",
    "aws_msk_cluster",
    "aws_kms_key",
    "aws_secretsmanager_secret",
    "aws_ecr_repository",
    "aws_cloudwatch_log_group",
    "aws_kinesis_stream",
    "aws_sqs_queue",
    "aws_backup_vault",
    "aws_fsx_lustre_file_system",
    "google_sql_database_instance",
    "google_storage_bucket",
    "google_bigquery_dataset",
    "google_compute_disk",
    "google_kms_crypto_key",
    "google_spanner_instance",
    "azurerm_storage_account",
    "azurerm_mssql_database",
    "azurerm_postgresql_flexible_server",
    "azurerm_cosmosdb_account",
    "azurerm_key_vault",
    "azurerm_managed_disk",
    "kubernetes_persistent_volume_claim",
    "kubernetes_persistent_volume",
    "kubernetes_namespace",
}

ALL_V4 = "0.0.0.0/0"
ALL_V6 = "::/0"
ADMIN_PORTS = {22, 3389, 5432, 3306, 1433, 6379, 27017, 9200}

IAM_POLICY_TYPES = {
    "aws_iam_policy",
    "aws_iam_role_policy",
    "aws_iam_user_policy",
    "aws_iam_group_policy",
    "aws_s3_bucket_policy",
    "aws_kms_key",
    "aws_sqs_queue_policy",
    "aws_sns_topic_policy",
}

ENCRYPTION_KEYS = re.compile(r"(^|\.)(encrypted|storage_encrypted|encryption_enabled|enable_encryption)$")


# --------------------------------------------------------------------------- review


def _open_ingress_rules(c: Change, state: Any) -> set[tuple[int, int, str]]:
    """Return (from_port, to_port, protocol) tuples open to the world."""
    if not isinstance(state, dict):
        return set()
    rules: list[dict[str, Any]] = []
    if c.type == "aws_security_group":
        rules = [r for r in state.get("ingress") or [] if isinstance(r, dict)]
    elif c.type == "aws_security_group_rule" and state.get("type") == "ingress":
        rules = [state]
    elif c.type == "aws_vpc_security_group_ingress_rule":
        rules = [
            {
                "cidr_blocks": [state.get("cidr_ipv4")],
                "ipv6_cidr_blocks": [state.get("cidr_ipv6")],
                "from_port": state.get("from_port"),
                "to_port": state.get("to_port"),
                "protocol": state.get("ip_protocol"),
            }
        ]
    out = set()
    for r in rules:
        cidrs = (r.get("cidr_blocks") or []) + (r.get("ipv6_cidr_blocks") or [])
        if ALL_V4 in cidrs or ALL_V6 in cidrs:
            fp = r.get("from_port")
            tp = r.get("to_port")
            out.add(
                (int(fp) if fp is not None else 0, int(tp) if tp is not None else 65535, str(r.get("protocol") or "-1"))
            )
    return out


def _check_open_ingress(c: Change) -> list[Finding]:
    new = _open_ingress_rules(c, c.after) - _open_ingress_rules(c, c.before)
    findings = []
    for fp, tp, proto in sorted(new):
        all_ports = proto in ("-1", "all") or (fp <= 0 and tp >= 65535)
        admin = any(fp <= p <= tp for p in ADMIN_PORTS)
        ports = "all ports" if all_ports else (str(fp) if fp == tp else f"{fp}-{tp}")
        findings.append(
            Finding(
                severity="critical" if (all_ports or admin) else "high",
                category="security",
                resource=c.address,
                title=f"Ingress opened to the internet ({ports}/{proto})",
                detail="A rule allowing 0.0.0.0/0 or ::/0 is being added.",
                recommendation="Restrict the CIDR to known ranges or put the service behind a load balancer/VPN.",
            )
        )
    return findings


def _policy_docs(c: Change) -> list[dict[str, Any]]:
    if not isinstance(c.after, dict):
        return []
    raw = c.after.get("policy")
    if not isinstance(raw, str):
        return []
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [doc] if isinstance(doc, dict) else []


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else [v]


def _check_iam_wildcards(c: Change) -> list[Finding]:
    if c.type not in IAM_POLICY_TYPES:
        return []
    findings = []
    for doc in _policy_docs(c):
        for st in _as_list(doc.get("Statement") or []):
            if not isinstance(st, dict) or st.get("Effect") != "Allow":
                continue
            actions = [str(a) for a in _as_list(st.get("Action") or [])]
            resources = [str(r) for r in _as_list(st.get("Resource") or [])]
            principals = st.get("Principal")
            public = principals == "*" or (isinstance(principals, dict) and "*" in _as_list(principals.get("AWS")))
            if public and not st.get("Condition"):
                findings.append(
                    Finding(
                        "critical",
                        "security",
                        c.address,
                        "Policy grants access to any principal (*)",
                        recommendation="Scope Principal to specific accounts/roles or add a Condition.",
                    )
                )
            if "*" in actions:
                sev = "critical" if "*" in resources else "high"
                findings.append(
                    Finding(
                        sev,
                        "security",
                        c.address,
                        'IAM policy allows Action "*"',
                        recommendation="Grant only the actions the workload needs.",
                    )
                )
            elif "*" in resources and any(a.endswith(":*") for a in actions):
                findings.append(
                    Finding(
                        "medium",
                        "security",
                        c.address,
                        "Service-wide wildcard actions on all resources",
                        detail=", ".join(a for a in actions if a.endswith(":*")),
                        recommendation="Narrow actions and resources.",
                    )
                )
    return findings


def _check_attr_flips(c: Change, diffs: list[AttrDiff]) -> list[Finding]:
    findings = []
    for d in diffs:
        leaf = d.path.rsplit(".", 1)[-1]
        if ENCRYPTION_KEYS.search(d.path) and d.after is False:
            findings.append(Finding("high", "security", c.address, f"Encryption disabled (`{d.path}`)"))
        elif leaf == "publicly_accessible" and d.after is True:
            findings.append(Finding("high", "security", c.address, "Resource becomes publicly accessible"))
        elif leaf == "deletion_protection" and d.before is True and d.after is False:
            findings.append(Finding("medium", "data-loss", c.address, "Deletion protection disabled"))
        elif leaf == "skip_final_snapshot" and d.after is True and c.type in STATEFUL_TYPES:
            findings.append(Finding("medium", "data-loss", c.address, "Final snapshot will be skipped"))
        elif (
            c.type == "aws_s3_bucket_public_access_block"
            and leaf.startswith(("block_public", "ignore_public", "restrict_public"))
            and d.after is False
        ):
            findings.append(Finding("high", "security", c.address, f"S3 public access guard disabled (`{leaf}`)"))
    return findings


def _check_lifecycle(c: Change) -> list[Finding]:
    stateful = c.type in STATEFUL_TYPES
    if c.action == "replace":
        forced = f" Forced by: {', '.join(f'`{p}`' for p in c.replace_paths)}." if c.replace_paths else ""
        if stateful:
            return [
                Finding(
                    "critical",
                    "data-loss",
                    c.address,
                    "Stateful resource will be REPLACED",
                    detail="Replacement destroys the existing resource; data may be lost." + forced,
                    recommendation="Avoid changing the forcing attribute; consider "
                    "`lifecycle { prevent_destroy = true }`, snapshots, or `moved` blocks.",
                )
            ]
        return [
            Finding(
                "low", "availability", c.address, "Resource will be replaced (possible downtime)", detail=forced.strip()
            )
        ]
    if c.action == "delete":
        if stateful:
            return [
                Finding(
                    "critical",
                    "data-loss",
                    c.address,
                    "Stateful resource will be DELETED",
                    recommendation="Make sure a backup exists or use a `removed` block to "
                    "forget it without destroying.",
                )
            ]
        return [Finding("medium", "availability", c.address, "Resource will be deleted")]
    return []


def review_rules(changes: list[Change]) -> list[Finding]:
    findings: list[Finding] = []
    for c in changes:
        diffs = diff(c.before, c.after, c.after_unknown)
        findings += _check_lifecycle(c)
        findings += _check_open_ingress(c)
        findings += _check_iam_wildcards(c)
        findings += _check_attr_flips(c, diffs)
    findings.sort(key=lambda f: (-sev_rank(f.severity), f.resource))
    return findings


# --------------------------------------------------------------------------- drift

DRIFT_CLASSES = ["noise", "manual-change", "risky"]

NOISE_PATHS = [
    re.compile(r"^tags_all(\.|$)"),
    re.compile(r"(^|\.)last_modified(_date|_time)?$"),
    re.compile(r"(^|\.)(etag|version_id|latest_version|last_updated)$"),
]
NOISE_BY_TYPE = {
    "aws_ecs_service": {"desired_count"},
    "aws_autoscaling_group": {"desired_capacity"},
    "aws_appautoscaling_target": {"min_capacity", "max_capacity"},
    "aws_eks_node_group": {"scaling_config[0].desired_size"},
    "aws_lambda_function": {"last_modified", "source_code_hash", "qualified_arn", "version"},
    "aws_launch_template": {"latest_version", "default_version"},
    # Counters of attachments managed by separate resources; the attachment itself is what matters.
    "aws_iam_policy": {"attachment_count"},
}
RISKY_TYPES = {
    "aws_security_group",
    "aws_security_group_rule",
    "aws_vpc_security_group_ingress_rule",
    "aws_vpc_security_group_egress_rule",
    "aws_network_acl",
    "aws_network_acl_rule",
    "aws_s3_bucket_public_access_block",
    "aws_s3_bucket_policy",
    "aws_s3_bucket_acl",
    "aws_kms_key",
    "aws_cloudtrail",
    "aws_guardduty_detector",
    "aws_config_configuration_recorder",
    "aws_wafv2_web_acl",
    "aws_iam_role",
    "aws_iam_role_policy",
    "aws_iam_policy",
    "aws_iam_user",
    "aws_iam_user_policy",
    "aws_iam_role_policy_attachment",
    "aws_iam_user_policy_attachment",
    "aws_iam_group_policy",
    "aws_iam_access_key",
    "aws_iam_openid_connect_provider",
    "google_project_iam_member",
    "google_project_iam_binding",
    "google_compute_firewall",
    "azurerm_network_security_rule",
    "azurerm_network_security_group",
    "azurerm_role_assignment",
    "kubernetes_cluster_role_binding",
    "kubernetes_role_binding",
}
RISKY_PATHS = re.compile(
    r"(^|\.)(policy|assume_role_policy|ingress|egress|cidr_blocks|publicly_accessible|encrypted|"
    r"storage_encrypted|kms_key_id|deletion_protection|iam_instance_profile|security_groups|"
    r"vpc_security_group_ids|logging|enable_logging|is_multi_region_trail)(\[|\.|$)"
)


@dataclass
class DriftItem:
    resource: str
    type: str
    action: str
    classification: str
    diffs: list[AttrDiff] = field(default_factory=list)
    reason: str = ""
    explanation: str = ""
    recommendation: str = ""
    # Classification proven from state (not a guess); the LLM must not override it.
    locked: bool = False


def _is_noise_path(c: Change, path: str) -> bool:
    if any(p.search(path) for p in NOISE_PATHS):
        return True
    return path in NOISE_BY_TYPE.get(c.type, set())


@dataclass
class DriftContext:
    """What the rest of the configuration manages, to tell real drift from Terraform's own side effects."""

    role_policy_arns: dict[str, set[str]] = field(default_factory=dict)
    role_inline_policies: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_state(cls, resources: list[Resource]) -> DriftContext:
        ctx = cls()
        for r in resources:
            v = r.values
            if r.type == "aws_iam_role_policy_attachment":
                ctx.role_policy_arns.setdefault(str(v.get("role")), set()).add(str(v.get("policy_arn")))
            elif r.type == "aws_iam_policy_attachment":
                for role in v.get("roles") or []:
                    ctx.role_policy_arns.setdefault(str(role), set()).add(str(v.get("policy_arn")))
            elif r.type == "aws_iam_role_policy":
                ctx.role_inline_policies.setdefault(str(v.get("role")), set()).add(str(v.get("name")))
        return ctx


def _explained_paths(c: Change, ctx: DriftContext) -> set[str]:
    """Paths whose drift is caused by other Terraform-managed resources (e.g. a policy attachment
    created after the role, so the role's recorded state doesn't list it yet)."""
    if c.type != "aws_iam_role" or not isinstance(c.after, dict):
        return set()
    before = c.before if isinstance(c.before, dict) else {}
    role = str(c.after.get("name"))
    explained = set()
    added = set(c.after.get("managed_policy_arns") or []) - set(before.get("managed_policy_arns") or [])
    removed = set(before.get("managed_policy_arns") or []) - set(c.after.get("managed_policy_arns") or [])
    if added and not removed and added <= ctx.role_policy_arns.get(role, set()):
        explained.add("managed_policy_arns")

    def names(v: Any) -> set[str]:
        return {str(p.get("name")) for p in v or [] if isinstance(p, dict)}

    new_inline = names(c.after.get("inline_policy")) - names(before.get("inline_policy"))
    if new_inline and new_inline <= ctx.role_inline_policies.get(role, set()):
        explained.add("inline_policy")
    return explained


def classify_drift(c: Change, ctx: DriftContext | None = None) -> DriftItem:
    if c.action == "delete":
        return DriftItem(c.address, c.type, c.action, "risky", reason="Resource was deleted outside Terraform")
    diffs = diff(c.before, c.after)
    explained = _explained_paths(c, ctx or DriftContext())
    signal = [
        d
        for d in diffs
        if not _is_noise_path(c, d.path) and not any(d.path == p or d.path.startswith(p + "[") for p in explained)
    ]
    if not signal:
        if explained:
            return DriftItem(
                c.address,
                c.type,
                c.action,
                "noise",
                diffs,
                reason="Side effect of resources Terraform manages (e.g. a policy attachment), not a manual change",
                locked=True,
            )
        return DriftItem(c.address, c.type, c.action, "noise", diffs, reason="Only volatile attributes changed")
    if c.type in RISKY_TYPES or any(RISKY_PATHS.search(d.path) for d in signal):
        return DriftItem(
            c.address,
            c.type,
            c.action,
            "risky",
            signal,
            reason="Security-relevant resource or attribute changed outside Terraform",
        )
    return DriftItem(
        c.address, c.type, c.action, "manual-change", signal, reason="Attributes changed outside Terraform"
    )


def drift_rules(drift: list[Change], prior_resources: list[Resource] | None = None) -> list[DriftItem]:
    ctx = DriftContext.from_state(prior_resources or [])
    order = {k: i for i, k in enumerate(reversed(DRIFT_CLASSES))}
    items = [classify_drift(c, ctx) for c in drift]
    # Drop entries with no visible difference (e.g. null vs empty normalisation by the provider).
    items = [i for i in items if i.diffs or i.action != "update"]
    return sorted(items, key=lambda i: (order[i.classification], i.resource))

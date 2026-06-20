import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DevOpsWorkflowContractTests(unittest.TestCase):
    def read(self, path: str) -> str:
        return (ROOT / path).read_text()

    def test_no_staging_workflow_is_defined(self) -> None:
        self.assertFalse((ROOT / ".github/workflows/deploy-staging.yml").exists())

    def test_deploy_workflows_validate_inputs_and_verify_real_deploys(self) -> None:
        deploy_workflow = self.read(".github/workflows/deploy.yml")
        admin_workflow = self.read(".github/workflows/deploy-admin.yml")

        for workflow in (deploy_workflow, admin_workflow):
            self.assertIn("DEPLOY_ENVIRONMENT: production", workflow)
            self.assertIn("scripts/validate-devops-env.sh", workflow)
            self.assertIn("scripts/validate-gcp-deploy-inputs.sh", workflow)
            self.assertIn("scripts/verify-cloud-run-deploy.sh", workflow)
            self.assertIn("GOOGLE_CLOUD_PROJECT=${PROJECT_ID}", workflow)
            self.assertIn("DELIVERY_TTL_DAYS=${DELIVERY_TTL_DAYS}", workflow)

        self.assertIn('REQUIRE_DOCKER_LOGIN: "true"', deploy_workflow)
        self.assertIn('VERIFY_PUBLIC_SERVICE: "true"', deploy_workflow)
        self.assertLess(
            deploy_workflow.index("scripts/validate-gcp-deploy-inputs.sh"),
            deploy_workflow.index("Build and push Docker image"),
        )
        self.assertIn('REQUIRE_DOCKER_LOGIN: "false"', admin_workflow)
        self.assertIn('VERIFY_PUBLIC_SERVICE: "false"', admin_workflow)

    def test_ci_runs_for_devops_changes(self) -> None:
        workflow = self.read(".github/workflows/ci.yml")

        self.assertIn("pull_request:", workflow)
        self.assertIn("branches-ignore:", workflow)
        self.assertNotIn("paths:", workflow)

    def test_github_security_configuration_exists(self) -> None:
        dependabot = self.read(".github/dependabot.yml")
        codeql = self.read(".github/workflows/codeql.yml")
        security_policy = self.read("SECURITY.md")
        pr_template = self.read(".github/pull_request_template.md")

        self.assertIn('package-ecosystem: "uv"', dependabot)
        self.assertIn('package-ecosystem: "docker"', dependabot)
        self.assertIn('package-ecosystem: "github-actions"', dependabot)
        self.assertIn("security-events: write", codeql)
        self.assertIn("security-extended,security-and-quality", codeql)
        self.assertIn("Do not open public issues", security_policy)
        self.assertIn("No secrets, tokens, or private identifiers added", pr_template)

    def test_docker_build_context_excludes_github_auth_credentials(self) -> None:
        dockerignore = self.read(".dockerignore")

        self.assertIn("gha-creds-*.json", dockerignore)

    def test_workflows_use_current_action_major_versions(self) -> None:
        workflow_text = "\n".join(
            path.read_text() for path in (ROOT / ".github/workflows").glob("*.yml")
        )

        for deprecated_pin in (
            "actions/checkout@v4",
            "actions/setup-python@v5",
            "astral-sh/setup-uv@v5",
            "docker/login-action@v3",
            "docker/setup-buildx-action@v3",
            "docker/build-push-action@v6",
            "google-github-actions/auth@v2",
            "google-github-actions/setup-gcloud@v2",
        ):
            self.assertNotIn(deprecated_pin, workflow_text)

        for current_pin in (
            "actions/checkout@v7",
            "actions/setup-python@v6",
            "astral-sh/setup-uv@v8.2.0",
            "docker/login-action@v4",
            "docker/setup-buildx-action@v4",
            "docker/build-push-action@v7",
            "google-github-actions/auth@v3",
            "google-github-actions/setup-gcloud@v3",
        ):
            self.assertIn(current_pin, workflow_text)

    def test_required_setup_failures_are_not_silently_skipped(self) -> None:
        ttl_script = self.read("scripts/configure-firestore-ttl.sh")
        alerts_script = self.read("scripts/configure-alerts.sh")

        self.assertNotIn("TTL setup skipped", ttl_script)
        self.assertNotIn("alert policy setup skipped", alerts_script)
        self.assertIn("exit 1", ttl_script)
        self.assertIn("exit 1", alerts_script)
        self.assertIn("${SERVICE:-aestheticlab-calendar-telegram}", alerts_script)


class DevOpsValidationScriptTests(unittest.TestCase):
    def valid_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "DEPLOY_ENVIRONMENT": "production",
                "PROJECT_ID": "nail-lab-449417",
                "REGION": "europe-west1",
                "SERVICE": "aestheticlab-calendar-telegram",
                "ADMIN_SERVICE": "aestheticlab-calendar-telegram-admin",
                "RUNTIME_SERVICE_ACCOUNT": (
                    "runtime@nail-lab-449417.iam.gserviceaccount.com"
                ),
                "DOCKER_IMAGE": "example/calendar-telegram",
                "DOCKERHUB_USERNAME": "docker-user",
                "DOCKERHUB_TOKEN": "docker-token",
                "REQUIRE_DOCKER_LOGIN": "true",
                "IMAGE_TAG": "a" * 40,
                "GCP_WORKLOAD_ID_PROVIDER": (
                    "projects/123/locations/global/workloadIdentityPools/pool/providers/github"
                ),
                "GCP_SERVICE_ACCOUNT_GITHUB": (
                    "github@nail-lab-449417.iam.gserviceaccount.com"
                ),
                "TELEGRAM_CHAT_ID": "-1001234567890",
                "WEBHOOK_URL": "https://example.com/webhook",
                "STATE_COLLECTION_PREFIX": "calendar_telegram",
                "RENEWAL_LEAD_MINUTES": "1440",
                "DELIVERY_TTL_DAYS": "30",
                "TELEGRAM_TOKEN_SECRET_NAME": "TELEGRAM_TOKEN",
                "CALENDAR_IDS_SECRET_NAME": "CALENDAR_IDS",
                "SCHEDULER_JOB": "aestheticlab-calendar-telegram-renew",
                "SCHEDULER_SERVICE_ACCOUNT_ID": "calendar-telegram-renewer",
            }
        )
        return env

    def run_validator(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "scripts/validate-devops-env.sh"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_valid_production_inputs_pass(self) -> None:
        result = self.run_validator(self.valid_env())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("validation passed", result.stdout)

    def test_rejects_staging_environment_assumption(self) -> None:
        env = self.valid_env()
        env["DEPLOY_ENVIRONMENT"] = "staging"

        result = self.run_validator(env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEPLOY_ENVIRONMENT must be production", result.stderr)

    def test_rejects_missing_docker_credentials_when_push_is_required(self) -> None:
        env = self.valid_env()
        env["DOCKERHUB_TOKEN"] = ""

        result = self.run_validator(env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set DOCKERHUB_TOKEN", result.stderr)

    def test_rejects_mutable_image_tag(self) -> None:
        env = self.valid_env()
        env["IMAGE_TAG"] = "latest"

        result = self.run_validator(env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latest is not allowed", result.stderr)


if __name__ == "__main__":
    unittest.main()

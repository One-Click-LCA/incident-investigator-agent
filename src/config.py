import os
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.environ.get("AWS_REGION", "eu-west-1")
AWS_PROFILE = os.environ.get("AWS_PROFILE")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
SECRET_NAME = os.environ.get("SECRET_NAME", "ocl-devops-ai-automation")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
GITHUB_ORG = os.environ.get("GITHUB_ORG", "One-Click-LCA")

# Optional explicit cluster override from env
ECS_CLUSTER = os.environ.get("ECS_CLUSTER", "")

# Module-level singleton — populated by bootstrap, read by all tools and agents
_secret: dict = {}
_service_name: str = ""
_env: str = ""
_minutes: int = 30
_cluster_arn: str = ""
_actual_service_name: str = ""
_github_token: str = ""


def set_runtime_context(
    secret: dict,
    service: str,
    env: str,
    minutes: int,
    cluster_arn: str = "",
    actual_service_name: str = "",
) -> None:
    global _secret, _service_name, _env, _minutes, _cluster_arn, _actual_service_name, _github_token
    _secret = secret
    _service_name = service
    _env = env
    _minutes = minutes
    _cluster_arn = cluster_arn
    _actual_service_name = actual_service_name or service
    _github_token = secret.get("GITHUB_TOKEN", "")


def get_secret() -> dict:
    return _secret


def get_env() -> str:
    return _env


def get_service() -> str:
    return _service_name


def get_minutes() -> int:
    return _minutes


def get_cluster_arn() -> str:
    return _cluster_arn


def get_actual_service_name() -> str:
    return _actual_service_name


def get_github_token() -> str:
    return _github_token


def get_github_org() -> str:
    return GITHUB_ORG

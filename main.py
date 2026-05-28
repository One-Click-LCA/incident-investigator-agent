#!/usr/bin/env python3
"""
Incident Investigator Agent — CLI entry point.

Usage:
  python main.py --service supply-chain-service --env preprod
  python main.py --service api-gateway --env prod --reason "502s spiking" --minutes 60
  python main.py --service usage-service --env preprod --repo-path ~/code/usage-service
  python main.py --service billing-service --env staging --focus mongo
"""

import argparse
import sys
from dotenv import load_dotenv

load_dotenv()


VALID_FOCUS = {"all", "ecs", "logs", "mongo", "redis", "rds", "github"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LangGraph-based incident investigator for ECS services",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--service", required=True, help="ECS service name (e.g. supply-chain-service)")
    parser.add_argument("--env", required=True, help="Environment name — any ECS cluster suffix (e.g. preprod, prod, config, mfg, betie)")
    parser.add_argument("--reason", default="", help="Incident description / observed symptoms")
    parser.add_argument("--minutes", type=int, default=30, help="Investigation window in minutes (default: 30)")
    parser.add_argument("--focus", default="all", choices=sorted(VALID_FOCUS), help="Focus area (default: all)")
    parser.add_argument("--repo-path", default=None, help="Local path to service git repo (enables code change analysis)")
    parser.add_argument("--output-dir", default="./output", help="Directory to save RCA output (default: ./output)")
    return parser.parse_args()


def main():
    args = parse_args()

    # Set output dir in config before any tools import it
    import os
    os.environ["OUTPUT_DIR"] = args.output_dir

    from src.bootstrap import bootstrap
    from src.agent import run_investigation

    print(f"\n{'='*60}")
    print(f"  Incident Investigator Agent")
    print(f"  Service: {args.service}  |  Env: {args.env}  |  Window: {args.minutes}m")
    print(f"{'='*60}\n")

    # Phase 1: Bootstrap (deterministic — credentials, ECS discovery, dep detection)
    try:
        ctx = bootstrap(service=args.service, env=args.env, minutes=args.minutes)
    except Exception as e:
        print(f"\n[fatal] Bootstrap failed: {e}")
        print("Check: AWS_PROFILE is set, service name is correct, you have ECS describe permissions.")
        sys.exit(1)

    print()

    # Phase 2: DeepAgent orchestrator loop
    result = run_investigation(
        service_context=ctx["service_context"],
        dependencies=ctx["dependencies"],
        framework=ctx["framework"],
        incident_description=args.reason,
        env=args.env,
        minutes=args.minutes,
        focus_area=args.focus,
        base_service_name=ctx.get("base_service_name", args.service),
        has_github=ctx.get("has_github", False),
    )

    print("\n" + "="*60)
    print("  Investigation Complete")
    print("="*60)
    if result.get("summary"):
        print(f"\n{result['summary']}")


if __name__ == "__main__":
    main()

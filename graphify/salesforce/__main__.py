"""python -m graphify.salesforce PROJECT [--output graph.json]"""
import argparse
import json
from pathlib import Path

from .engine import build_graph, scan_project

def main():
    parser = argparse.ArgumentParser(description="Build a deterministic Salesforce dependency graph")
    parser.add_argument("project", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.project.is_dir():
        parser.error("project must be an existing directory")
    result = build_graph(scan_project(args.project))
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        print(json.dumps(result["stats"]))
    else:
        print(output, end="")


if __name__ == "__main__":
    main()

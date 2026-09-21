"""Deterministic Salesforce syntax and metadata graph extraction.

The public API accepts source snapshots, so callers own retrieval, tenant scope,
storage, and authorization. No Salesforce credentials or model calls are used.
"""
from .engine import build_graph, scan_project
from .model import Source, node_id

__all__ = ["Source", "build_graph", "scan_project", "node_id"]

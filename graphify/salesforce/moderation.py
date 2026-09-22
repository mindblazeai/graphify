"""Documented moderation targets are not aliases for ordinary object fields.

Contract: Salesforce Metadata API, ModerationRule/ModeratedEntityField, pinned
sf-skills revision c217b703b3e5a3c279f1a510d8703161b14bd0a5. RawBody and
RawCommentBody exist only in this API. Preserve them as content selectors on
the verified entity, never as invented CustomField declarations or Body aliases.
"""
from __future__ import annotations

import re

from .experience import site_reference

CONTENT_SELECTORS = frozenset({("FeedItem", "RawBody"), ("FeedComment", "RawCommentBody")})
SELECTOR_NAMES = frozenset(field.casefold() for _, field in CONTENT_SELECTORS)
MAX_TARGETS = 1000
MAX_CRITERIA = 256


def parse_moderation(facts, root, *, issue, scalar, ref, children, field_ref):
    owner = facts.source.component_id
    for tag, values, required in (
        ("action", {"Block", "Review", "Replace", "Flag", "FreezeAndNotify"}, True),
        ("type", {"Content", "Rate"}, False),
        ("timePeriod", {"Short", "Medium"}, False),
    ):
        item = scalar(root, tag, required=required or bool(children(root, tag)))
        if item and item.text.strip() not in values:
            issue("moderation_value_unsupported", item, property=tag)

    scalar(root, "masterLabel", required=True)
    for tag in ("description", "userMessage", "fullName"):
        scalar(root, tag)
    active = scalar(root, "active", required=True)
    if active:
        value = active.text.strip()
        if value not in {"true", "false", "1", "0"}:
            issue("moderation_value_unsupported", active, property="active")
        else:
            # Configuration only, not evidence that moderation ran on a record.
            facts.nodes[owner]["moderation_active"] = value in {"true", "1"}
    for tag in ("actionLimit", "notifyLimit"):
        item = scalar(root, tag, required=bool(children(root, tag)))
        if item:
            value = item.text.strip()
            if not re.fullmatch(r"[+-]?[0-9]{1,10}", value) or not -2147483648 <= int(value) <= 2147483647:
                issue("moderation_value_unsupported", item, property=tag)

    def entries(tag, limit):
        values = children(root, tag)
        if len(values) > limit:
            issue("moderation_entry_limit", root, property=tag, limit=limit)
            return []
        return values

    # This is repeatable in the WSDL, not a singleton scalar.
    for item in entries("userCriteria", MAX_CRITERIA):
        if item.children or not item.text.strip():
            issue("metadata_reference_value_missing", item, property="userCriteria")
        else:
            ref(item, "UserCriteria", identity_contract="moderation_criterion")

    for entry in entries("entitiesAndFields", MAX_TARGETS):
        entity = scalar(entry, "entityName", required=True)
        obj = entity.text.strip() if entity else ""
        ref(entity, "CustomObject", "references_object", identity_contract="moderation_entity")
        item = scalar(entry, "fieldName")
        keyword = scalar(entry, "keywordList")
        ref(keyword, "KeywordList", identity_contract="moderation_keyword_list")
        if item:
            field = item.text.strip()
            if field.casefold() in SELECTOR_NAMES:
                if (obj, field) in CONTENT_SELECTORS:
                    ref(item, "CustomObject", "moderates_content", name=obj,
                        metadata_selector=field,
                        selector_contract="MetadataAPI.ModeratedEntityField",
                        identity_contract="moderation_entity")
                else:
                    issue("moderation_selector_context_unsupported", item, entity=obj, selector=field)
            else:
                # Normal field slots still require the independently declared
                # field. Labels, lookalikes and unknown names cannot close gaps.
                field_ref(item, obj, identity_contract="moderation_field")
    site_reference(facts, root, issue=issue,
                   ref=lambda *args, **attrs: ref(*args, identity_contract="moderation_site", **attrs))

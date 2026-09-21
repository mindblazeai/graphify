"""Type-scoped declarative references, not guesses from arbitrary XML strings.

Contracts: Salesforce Metadata API guide v68, PathAssistant, AnimationRule,
Role, Queue, ServiceChannel, TopicsForObjects, Profile* policies,
LeadConvertSettings and ModerationRule. Unknown XML stays explicitly partial.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .model import Facts
from .experience import SHAPES as EXPERIENCE_SHAPES, parse_experience, shape_path, site_reference

if TYPE_CHECKING:
    from .metadata import Element


# Each key is a path below the metadata root; values are its permitted children.
# Labels, help text, picklist values and arbitrary new properties are not refs.
SHAPES = {
    "PathAssistant": {
        "": "active entityName fieldName masterLabel pathAssistantSteps recordTypeName",
        "pathAssistantSteps": "fieldNames info picklistValueName",
    },
    "AnimationRule": {"": "animationFrequency developerName isActive masterLabel recordTypeContext recordTypeName sobjectType targetField targetFieldChangeToValues"},
    "Role": {"": "caseAccessLevel contactAccessLevel description mayForecastManagerShare name opportunityAccessLevel parentRole"},
    "Queue": {
        "": "doesIncludeBosses doesSendEmailToMembers email name queueMembers queueRoutingConfig queueSobject",
        "queueMembers": "publicGroups roleAndSubordinates roleAndSubordinatesInternal roles users queueRoleAndSubordinates",
        "queueMembers/publicGroups": "publicGroup",
        "queueMembers/roleAndSubordinates": "roleAndSubordinate",
        "queueMembers/roleAndSubordinatesInternal": "roleAndSubordinateInternal",
        "queueMembers/queueRoleAndSubordinates": "queueRoleAndSubordinate",
        "queueMembers/roles": "role", "queueMembers/users": "user",
        "queueSobject": "sobjectType",
    },
    "ServiceChannel": {
        "": "acwExtensionDuration afterConvoMaxTime afterConvoWorkMaxTime capacityModel doesCheckCapOnOwnerChange doesCheckCapOnStatusChange doesMinimizeWidgetOnAccept hasAcwExtensionEnabled hasAfterConvoWorkTimer hasAutoAcceptEnabled interactionComponent isInterruptible label maxExtensions relatedEntityType secondaryRoutingPriorityField serviceChannelStatusFieldMappings serviceChannelFieldPriorities statusField",
        "serviceChannelStatusFieldMappings": "priority value type",
        "serviceChannelFieldPriorities": "priority value type",
    },
    "TopicsForObjects": {"": "enableTopics entityApiName"},
    "ProfilePasswordPolicy": {"": "forgotPasswordRedirect lockoutInterval maxLoginAttempts minimumPasswordLength minimumPasswordLifetime obscure passwordComplexity passwordExpiration passwordHistory passwordQuestion profile"},
    "ProfileSessionSetting": {"": "externalCommunityUserIdentityVerif forceLogout profile requiredSessionLevel sessionPersistence sessionTimeout sessionTimeoutWarning"},
    "LeadConvertSettings": {
        "": "allowOwnerChange objectMapping opportunityCreationOptions",
        "objectMapping": "inputObject outputObject mappingFields",
        "objectMapping/mappingFields": "inputField outputField",
    },
    "ModerationRule": {
        "": "action actionLimit active description entitiesAndFields masterLabel notifyLimit userCriteria userMessage type timePeriod",
        "entitiesAndFields": "entityName fieldName keywordList",
    },
}
SHAPES.update(EXPERIENCE_SHAPES)
SHAPES = {kind: {path: frozenset(tags.split()) | ({"fullName"} if not path else set())
                 for path, tags in shape.items()} for kind, shape in SHAPES.items()}


def parse_declarative(facts: Facts, root: Element, kind: str) -> None:
    """Extract supported reference slots; leave all binding to the scoped graph."""
    facts.level = "semantic"
    owner = facts.source.component_id

    def issue(code, n=root, **details):
        facts.level = "partial"
        facts.issue(code, n.line, metadata_type=kind, **details)

    if root.tag != kind:
        issue("metadata_root_type_mismatch", actual=root.tag)
        return

    unknown = set()

    def validate(n, path=""):
        allowed = SHAPES[kind].get(shape_path(kind, path), ())
        for child in n.children:
            child_path = path + "/" + child.tag if path else child.tag
            if child.tag not in allowed:
                if child_path not in unknown:
                    unknown.add(child_path)
                    issue("metadata_xml_property_unsupported", child, xml_path=child_path)
            else:
                validate(child, child_path)
    validate(root)

    def children(n, tag):
        return [c for c in n.children if c.tag == tag]

    def scalar(n, tag, required=False):
        matches = children(n, tag)
        if len(matches) > 1:
            issue("metadata_reference_ambiguous_scalar", n, property=tag)
            return None
        if not matches or not matches[0].text.strip() or matches[0].children:
            if required or (matches and matches[0].children):
                issue("metadata_reference_value_missing", n, property=tag)
            return None
        return matches[0]

    def ref(n, target_kind, relation="references", name=None, source=owner, **attrs):
        if n is not None:
            facts.ref(source, target_kind, name or n.text.strip(), relation, n.line, **attrs)

    def object_context(n, tag):
        value = scalar(n, tag, required=True)
        ref(value, "CustomObject", "references_object")
        return value.text.strip() if value else ""

    def field_ref(n, obj, relation="references_field", source=owner, **attrs):
        if n is None:
            return
        if not obj:
            issue("metadata_reference_context_missing", n, property=n.tag)
            return
        name = n.text.strip()
        if not name.casefold().startswith(obj.casefold() + "."):
            name = obj + "." + name
        ref(n, "FieldPath", relation, name, source, **attrs)

    def record_type(n, obj):
        if n is None:
            return
        if n.text.strip().casefold() == "__master__":
            facts.nodes[owner]["record_type_scope"] = "master"
        elif obj:
            name = n.text.strip()
            ref(n, "RecordType", name=name if name.casefold().startswith(obj.casefold() + ".") else obj + "." + name)
        else:
            issue("metadata_reference_context_missing", n, property=n.tag)

    if kind in EXPERIENCE_SHAPES:
        parse_experience(facts, root, kind, issue=issue, scalar=scalar, ref=ref, children=children)

    elif kind == "PathAssistant":
        obj = object_context(root, "entityName")
        field_ref(scalar(root, "fieldName", required=True), obj)
        record_type(scalar(root, "recordTypeName", required=True), obj)
        for step in children(root, "pathAssistantSteps"):
            for item in children(step, "fieldNames"):
                if item.text.strip() and not item.children:
                    field_ref(item, obj)

    elif kind == "AnimationRule":
        obj = object_context(root, "sobjectType")
        field_ref(scalar(root, "targetField", required=True), obj)
        context = scalar(root, "recordTypeContext", required=True)
        mode = context.text.strip().casefold() if context else ""
        if mode in {"all", "master"}:
            facts.nodes[owner]["record_type_scope"] = mode
        elif mode == "custom":
            target = scalar(root, "recordTypeName", required=True)
            if target and target.text.strip().casefold() == "__master__":
                issue("animation_record_type_context_conflict", target)
            else:
                record_type(target, obj)
        elif context:
            issue("animation_record_type_context_unsupported", context)

    elif kind == "Role":
        ref(scalar(root, "parentRole"), "Role", "parent_role")
        # These are conditional record-access settings, NOT effective grants.
        for tag, obj in [("caseAccessLevel", "Case"), ("contactAccessLevel", "Contact"),
                         ("opportunityAccessLevel", "Opportunity")]:
            level = scalar(root, tag)
            if level:
                ref(level, "CustomObject", "configures_access", obj,
                    access_level=level.text.strip(), access_context="accounts_owned_by_user")

    elif kind == "Queue":
        ref(scalar(root, "queueRoutingConfig"), "QueueRoutingConfig")
        for entry in children(root, "queueSobject"):
            object_context(entry, "sobjectType")
        for members in children(root, "queueMembers"):
            for group in members.children:
                mapping = {
                    "publicGroups": ("publicGroup", "Group", "direct"),
                    "roles": ("role", "Role", "direct"),
                    "roleAndSubordinates": ("roleAndSubordinate", "Role", "all_subordinates"),
                    "queueRoleAndSubordinates": ("queueRoleAndSubordinate", "Role", "all_subordinates"),
                    "roleAndSubordinatesInternal": ("roleAndSubordinateInternal", "Role", "internal_subordinates"),
                    "users": ("user", "SalesforceUser", "direct"),
                }.get(group.tag)
                if not mapping:
                    continue
                tag, target_kind, membership = mapping
                for item in children(group, tag):
                    if not item.text.strip() or item.children:
                        issue("metadata_reference_value_missing", item, property=tag)
                        continue
                    ref(item, target_kind, "includes_members", membership_scope=membership)
                    if target_kind == "SalesforceUser":
                        issue("metadata_user_record_not_indexed", item)

    elif kind == "ServiceChannel":
        obj = object_context(root, "relatedEntityType")
        priority = scalar(root, "secondaryRoutingPriorityField")
        if priority:
            field_ref(priority, obj, field_name_or_id=priority.text.strip(), field_object=obj)
        field_ref(scalar(root, "statusField"), obj)
        interaction = scalar(root, "interactionComponent")
        if interaction:
            # The API says console component, not ApexPage vs LWC vs Aura.
            ref(interaction, "SalesforceConsoleComponent")
            issue("service_channel_console_component_unresolved", interaction)

    elif kind == "TopicsForObjects":
        # A disabled setting still configures its explicitly named object.
        object_context(root, "entityApiName")

    elif kind in {"ProfilePasswordPolicy", "ProfileSessionSetting"}:
        ref(scalar(root, "profile", required=True), "Profile", "configures")

    elif kind == "LeadConvertSettings":
        for mapping in children(root, "objectMapping"):
            input_obj = object_context(mapping, "inputObject")
            output_obj = object_context(mapping, "outputObject")
            for pair in children(mapping, "mappingFields"):
                source = scalar(pair, "inputField", required=True)
                target = scalar(pair, "outputField", required=True)
                pair_owner = owner
                if source and target and input_obj and output_obj:
                    # Separate mappings prevent a proven path through unrelated
                    # sibling field pairs, like separate Apex methods do.
                    name = f"{facts.source.full_name}:{input_obj}.{source.text.strip()}->{output_obj}.{target.text.strip()}"
                    pair_owner = facts.declare("LeadConversionMapping", name, pair.line)
                    facts.ref(owner, "LeadConversionMapping", name, "contains", pair.line)
                field_ref(source, input_obj, "reads", pair_owner)
                field_ref(target, output_obj, "writes", pair_owner)

    elif kind == "ModerationRule":
        ref(scalar(root, "userCriteria"), "UserCriteria")
        for entry in children(root, "entitiesAndFields"):
            obj = object_context(entry, "entityName")
            item = scalar(entry, "fieldName")
            field_ref(item, obj)
            ref(scalar(entry, "keywordList"), "KeywordList")
            if item and (obj.casefold(), item.text.strip().casefold()) in {
                ("feeditem", "rawbody"), ("feedcomment", "rawcommentbody")
            }:
                # Explicit API-only field names are not aliases for Body.
                issue("moderation_metadata_only_field", item)
        site_reference(facts, root, issue=issue, ref=ref)

from app.core.enums import MEMBER_ROLES, Role
from app.core.errors import conflict, not_found, unprocessable
from app.models import Workflow, WorkflowAssignment, WorkflowStage
from app.repositories.repos import CategoryRepo, MemberRepo, WorkflowRepo
from app.services import audit

APPROVER_ROLES = {Role.REVIEWER, Role.DOCUMENT_MANAGER, Role.ORG_ADMIN}  # auditors are read-only


def resolve_stage_reviewers(db, org_id: int, stage, owner_id: int | None = None) -> list[int]:
    """Direct assignments + every active member holding an assigned role; the document owner never reviews
    their own document (separation of duties)."""
    members = MemberRepo(db)
    ids: list[int] = []
    for a in stage.assignments:
        if a.user_id is not None:
            m = members.get_pair(org_id, a.user_id)
            if m and m.is_active and m.user.is_active and m.role in APPROVER_ROLES:
                ids.append(a.user_id)
        else:
            ids += [m.user_id for m in members.active_users_with_roles(org_id, [a.role])]
    return [i for i in dict.fromkeys(ids) if i != owner_id]


def workflow_to_dict(w: Workflow) -> dict:
    return {
        "id": w.id, "organization_id": w.organization_id, "name": w.name, "description": w.description,
        "category_ids": list(w.category_ids or []), "rejection_policy": w.rejection_policy,
        "is_active": w.is_active, "version": w.version,
        "stages": [{
            "stage_order": s.stage_order, "name": s.name, "required_approvals": s.required_approvals,
            "require_all": s.require_all, "escalate_after_hours": s.escalate_after_hours,
            "escalate_to_user_id": s.escalate_to_user_id,
            "reviewer_ids": [a.user_id for a in s.assignments if a.user_id is not None],
            "reviewer_roles": [a.role for a in s.assignments if a.role is not None]} for s in w.stages]}


class WorkflowService:
    def __init__(self, db):
        self.db, self.repo = db, WorkflowRepo(db)

    def _validate(self, ctx, category_ids, stages):
        cats = CategoryRepo(self.db)
        for cid in category_ids or []:
            if cats.get_in_org(ctx.org_id, cid) is None:
                raise unprocessable("CATEGORY_NOT_FOUND", f"Category {cid} does not exist in this organization.")
        members = MemberRepo(self.db)
        for i, st in enumerate(stages or [], start=1):
            if not st.reviewer_ids and not st.reviewer_roles:
                raise unprocessable("INVALID_WORKFLOW", f"Stage {i} needs at least one reviewer or reviewer role.")
            for role in st.reviewer_roles:
                if role not in MEMBER_ROLES or role not in APPROVER_ROLES:
                    raise unprocessable("INVALID_WORKFLOW", f"Role '{role}' cannot approve documents.")
            for uid in [*st.reviewer_ids, *([st.escalate_to_user_id] if st.escalate_to_user_id else [])]:
                m = members.get_pair(ctx.org_id, uid)
                if m is None or not m.is_active:
                    raise unprocessable("INVALID_WORKFLOW", f"User {uid} is not an active member of this organization.")
            for uid in st.reviewer_ids:
                if members.get_pair(ctx.org_id, uid).role not in APPROVER_ROLES:
                    raise unprocessable("INVALID_WORKFLOW", f"User {uid} has a role that cannot approve documents.")

    def _build_stages(self, stages) -> list[WorkflowStage]:
        out = []
        for i, st in enumerate(stages, start=1):
            stage = WorkflowStage(stage_order=i, name=st.name, required_approvals=st.required_approvals,
                                  require_all=st.require_all, escalate_after_hours=st.escalate_after_hours,
                                  escalate_to_user_id=st.escalate_to_user_id)
            stage.assignments = ([WorkflowAssignment(user_id=u) for u in dict.fromkeys(st.reviewer_ids)] +
                                 [WorkflowAssignment(role=r) for r in dict.fromkeys(st.reviewer_roles)])
            out.append(stage)
        return out

    def create(self, ctx, payload):
        if self.repo.by_name(ctx.org_id, payload.name):
            raise conflict("WORKFLOW_EXISTS", "A workflow with this name already exists.")
        self._validate(ctx, payload.category_ids, payload.stages)
        wf = Workflow(organization_id=ctx.org_id, name=payload.name.strip(), description=payload.description,
                      category_ids=sorted(set(payload.category_ids)), rejection_policy=payload.rejection_policy.value,
                      created_by=ctx.user.id)
        wf.stages = self._build_stages(payload.stages)
        self.repo.add(wf)
        audit.record(self.db, action="WORKFLOW_CREATED", entity_type="workflow", entity_id=wf.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta=workflow_to_dict(wf))
        self.db.commit()
        return wf

    def get(self, ctx, workflow_id: int):
        wf = self.repo.get_in_org(ctx.org_id, workflow_id)
        if wf is None:
            raise not_found("WORKFLOW_NOT_FOUND", "Workflow not found.")
        return wf

    def list(self, ctx):
        return self.repo.list(ctx.org_id)

    def update(self, ctx, workflow_id: int, payload):
        """Policy for in-flight documents: they keep the frozen workflow snapshot taken at submission.
        Edits (version bump) apply only to documents submitted afterwards."""
        wf = self.get(ctx, workflow_id)
        before = workflow_to_dict(wf)
        if payload.name and payload.name.lower() != wf.name.lower() and self.repo.by_name(ctx.org_id, payload.name):
            raise conflict("WORKFLOW_EXISTS", "A workflow with this name already exists.")
        self._validate(ctx, payload.category_ids, payload.stages)
        if payload.name is not None:
            wf.name = payload.name.strip()
        if payload.description is not None:
            wf.description = payload.description
        if payload.category_ids is not None:
            wf.category_ids = sorted(set(payload.category_ids))
        if payload.rejection_policy is not None:
            wf.rejection_policy = payload.rejection_policy.value
        if payload.stages is not None:
            wf.stages.clear()
            self.db.flush()
            wf.stages.extend(self._build_stages(payload.stages))
        if wf.is_active:
            self._ensure_no_overlap(ctx, wf)
        wf.version += 1
        audit.record(self.db, action="WORKFLOW_UPDATED", entity_type="workflow", entity_id=wf.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"before": before, "after": workflow_to_dict(wf)})
        self.db.commit()
        return wf

    def _ensure_no_overlap(self, ctx, wf):
        mine = set(wf.category_ids or [])
        for other in self.repo.active_list(ctx.org_id):
            if other.id != wf.id and mine & set(other.category_ids or []):
                raise conflict("WORKFLOW_CONFLICT",
                               f"Active workflow '{other.name}' already covers one of these categories.")

    def set_active(self, ctx, workflow_id: int, active: bool):
        wf = self.get(ctx, workflow_id)
        if active:
            if not wf.stages:
                raise unprocessable("INVALID_WORKFLOW", "A workflow needs at least one stage.")
            self._ensure_no_overlap(ctx, wf)
        wf.is_active = active
        audit.record(self.db, action="WORKFLOW_ACTIVATED" if active else "WORKFLOW_DEACTIVATED",
                     entity_type="workflow", entity_id=wf.id, actor_id=ctx.user.id, org_id=ctx.org_id)
        self.db.commit()
        return wf

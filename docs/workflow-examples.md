# Workflow configuration examples

A stage is configured with `reviewer_ids` (specific users) and/or `reviewer_roles` (every active member holding that
role at submission time). Stage order = position in the `stages` array. `require_all=true` means every resolved
reviewer must approve; otherwise `required_approvals` is the quorum (capped at the number of reviewers).

## 1. Contract approval (3 sequential stages)
```json
POST /api/workflows
{
  "name": "Contract approval",
  "category_ids": [1],
  "rejection_policy": "ANY_REJECTION",
  "stages": [
    {"name": "Department Manager Review", "reviewer_ids": [4], "escalate_after_hours": 48, "escalate_to_user_id": 1},
    {"name": "Compliance Review", "reviewer_roles": ["reviewer"], "required_approvals": 2},
    {"name": "Final Approval", "reviewer_roles": ["org_admin"]}
  ]
}
```
Then `POST /api/workflows/{id}/activate`.

## 2. Four-eyes invoice check (everyone must approve)
```json
{"name": "Invoice 4-eyes", "category_ids": [2], "stages": [
  {"name": "Finance", "reviewer_ids": [5, 6], "require_all": true}]}
```

## 3. Tolerant stage (reject only if the quorum became impossible)
```json
{"name": "SOP review", "category_ids": [3], "rejection_policy": "STAGE_QUORUM", "stages": [
  {"name": "Panel", "reviewer_ids": [5, 6, 7], "required_approvals": 2}]}
```
With three reviewers and a quorum of two, one rejection does not reject the document; a second one does.

## Rules worth knowing
* Only one **active** workflow may cover a category (`WORKFLOW_CONFLICT`).
* **In-flight documents are unaffected by edits**: at submission the workflow is frozen into
  `documents.workflow_snapshot` (stages, resolved reviewers, quorum, rejection policy, workflow version).
  Editing/deactivating a workflow only changes documents submitted afterwards.
* The document owner is never resolved as a reviewer of their own document.
* A stage that resolves to zero eligible reviewers blocks submission (`WORKFLOW_NOT_CONFIGURED`).

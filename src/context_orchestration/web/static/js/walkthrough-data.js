/* One real worker turn, dumped out of the store after an actual run of the
   demo task at a 1600-token budget. It is data, not prose, so the page
   cannot drift from what the engine does. */

export const WT = {
  "sections": [
    {
      "key": "assigned_task",
      "title": "YOUR ASSIGNED TASK",
      "priority": 1,
      "lines": [
        "Continue from the current execution state and design authentication and authorization."
      ]
    },
    {
      "key": "objective",
      "title": "TASK OBJECTIVE",
      "priority": 2,
      "lines": [
        "Build the architecture and implementation plan for a Task Management REST API using FastAPI and PostgreSQL."
      ]
    },
    {
      "key": "current_progress",
      "title": "CURRENT PROGRESS",
      "priority": 3,
      "lines": [
        "Schema complete with four tables and FK constraints. Authentication not yet designed."
      ]
    },
    {
      "key": "current_task",
      "title": "CURRENT TASK STATE",
      "priority": 4,
      "lines": [
        "Continue from the current execution state and design authentication and authorization."
      ]
    },
    {
      "key": "decisions",
      "title": "IMPORTANT DECISIONS",
      "priority": 5,
      "lines": [
        "Use FastAPI as the API framework. Reason: Async support and automatic OpenAPI generation",
        "Use PostgreSQL as the primary datastore. Reason: Relational task/user data with transactional integrity",
        "Adopt a layered architecture (router / service / repository). Reason: Keeps business logic testable and independent of the web layer",
        "Use UUID primary keys. Reason: Avoids sequential-ID enumeration on public endpoints",
        "Soft-delete tasks via deleted_at. Reason: Audit history is a stated requirement"
      ]
    },
    {
      "key": "completed_work",
      "title": "COMPLETED WORK",
      "priority": 6,
      "lines": [
        "Define the functional requirements and the overall system architecture for the API.",
        "Continue from the current execution state and design the PostgreSQL database schema."
      ]
    },
    {
      "key": "artifacts",
      "title": "ARTIFACTS",
      "priority": 7,
      "lines": [
        "requirements.md - Functional and non-functional requirements [unverified]",
        "architecture.md - Updated with the data-model section [v2, unverified]",
        "schema.sql - users, projects, tasks, task_assignments with FK constraints [unverified]"
      ]
    },
    {
      "key": "issues",
      "title": "UNRESOLVED ISSUES",
      "priority": 8,
      "lines": [
        "[medium] Task ordering within a project is unspecified (lexical vs explicit rank)"
      ]
    },
    {
      "key": "failed_attempts",
      "title": "FAILED ATTEMPTS (DO NOT REPEAT)",
      "priority": 9,
      "lines": [
        "Modelling tasks and projects in a single denormalised table - Made per-project permission checks impossible to express cleanly",
        "Single denormalised table for tasks and projects - broke permission modelling",
        "Storing task tags as a comma-separated text column - Cannot be indexed for tag filtering; replaced with a join table",
        "Comma-separated tag column - unindexable, replaced by a join table"
      ]
    },
    {
      "key": "assumptions",
      "title": "ASSUMPTIONS",
      "priority": 10,
      "lines": [
        "Single-tenant deployment for the MVP (No multi-tenant requirement was stated)",
        "A task belongs to exactly one project (Simplifies permission inheritance)"
      ]
    },
    {
      "key": "last_action",
      "title": "LAST ACTION",
      "priority": 11,
      "lines": [
        "Wrote schema.sql including the task_tags join table."
      ]
    },
    {
      "key": "next_action",
      "title": "RECOMMENDED NEXT ACTION",
      "priority": 12,
      "lines": [
        "Design authentication and authorization for the API."
      ]
    },
    {
      "key": "previous_worker_notes",
      "title": "NOTES FROM PREVIOUS WORKER (worker-2)",
      "priority": 13,
      "lines": [
        "The schema already contains the user/project/task relationships. Do not redesign it. Permissions should hang off project membership, not off individual tasks.",
        "State at handoff: Schema is complete and consistent with the layered architecture. Auth is untouched."
      ]
    }
  ],
  "packageText": "YOUR ASSIGNED TASK\n\nContinue from the current execution state and design authentication and authorization.\n\nTASK OBJECTIVE\n\nBuild the architecture and implementation plan for a Task Management REST API using FastAPI and PostgreSQL.\n\nCURRENT PROGRESS\n\nSchema complete with four tables and FK constraints. Authentication not yet designed.\n\nCURRENT TASK STATE\n\nContinue from the current execution state and design authentication and authorization.\n\nIMPORTANT DECISIONS\n\nUse FastAPI as the API framework. Reason: Async support and automatic OpenAPI generation\nUse PostgreSQL as the primary datastore. Reason: Relational task/user data with transactional integrity\nAdopt a layered architecture (router / service / repository). Reason: Keeps business logic testable and independent of the web layer\nUse UUID primary keys. Reason: Avoids sequential-ID enumeration on public endpoints\nSoft-delete tasks via deleted_at. Reason: Audit history is a stated requirement\n\nCOMPLETED WORK\n\nDefine the functional requirements and the overall system architecture for the API.\nContinue from the current execution state and design the PostgreSQL database schema.\n\nARTIFACTS\n\nrequirements.md - Functional and non-functional requirements [unverified]\narchitecture.md - Updated with the data-model section [v2, unverified]\nschema.sql - users, projects, tasks, task_assignments with FK constraints [unverified]\n\nUNRESOLVED ISSUES\n\n[medium] Task ordering within a project is unspecified (lexical vs explicit rank)\n\nFAILED ATTEMPTS (DO NOT REPEAT)\n\nModelling tasks and projects in a single denormalised table - Made per-project permission checks impossible to express cleanly\nSingle denormalised table for tasks and projects - broke permission modelling\nStoring task tags as a comma-separated text column - Cannot be indexed for tag filtering; replaced with a join table\nComma-separated tag column - unindexable, replaced by a join table\n\nASSUMPTIONS\n\nSingle-tenant deployment for the MVP (No multi-tenant requirement was stated)\nA task belongs to exactly one project (Simplifies permission inheritance)\n\nLAST ACTION\n\nWrote schema.sql including the task_tags join table.\n\nRECOMMENDED NEXT ACTION\n\nDesign authentication and authorization for the API.\n\nNOTES FROM PREVIOUS WORKER (worker-2)\n\nThe schema already contains the user/project/task relationships. Do not redesign it. Permissions should hang off project membership, not off individual tasks.\nState at handoff: Schema is complete and consistent with the layered architecture. Auth is untouched.",
  "packageTokens": 631,
  "packageBudget": 1600,
  "stateIn": {
    "completed_tasks": 2,
    "decisions": 5,
    "artifacts": 3,
    "open_issues": 1,
    "failed_attempts": 4,
    "assumptions": 2
  },
  "result": {
    "summary": "Designed authentication and project-scoped authorization.",
    "completed_tasks": [
      "Continue from the current execution state and design authentication and authorization."
    ],
    "decisions": [
      {
        "decision": "Use JWT access tokens with refresh tokens",
        "reason": "Stateless verification suits the layered service design"
      },
      {
        "decision": "Hash passwords with argon2id",
        "reason": "Current best-practice KDF with tuned memory cost"
      },
      {
        "decision": "Authorize on project membership",
        "reason": "Matches the inherited schema where tasks belong to one project"
      }
    ],
    "artifacts": [
      {
        "name": "auth.py",
        "kind": "code",
        "description": "JWT issuing, verification and the FastAPI dependency"
      },
      {
        "name": "schema.sql",
        "kind": "sql",
        "description": "Added refresh_tokens and project_members tables"
      }
    ],
    "issues": [
      {
        "description": "Refresh-token rotation and revocation strategy not finalised",
        "severity": "high",
        "resolved": false
      }
    ],
    "failed_attempts": [
      {
        "attempt": "Session cookies held in application memory",
        "reason": "Breaks under multi-instance deployment; no shared session store"
      }
    ],
    "assumptions": [
      {
        "assumption": "Access tokens live 15 minutes, refresh tokens 14 days",
        "reason": "No requirement was stated; conventional defaults"
      }
    ],
    "current_progress": "Auth design complete. Endpoints still undesigned.",
    "last_action": "Specified the get_current_user FastAPI dependency in auth.py.",
    "next_action": "Design the REST API endpoints for task and project CRUD."
  },
  "report": {
    "work_completed": [
      "JWT authentication design",
      "Project-membership authorization model"
    ],
    "current_state": "Auth designed and reflected in the schema. Endpoint surface not yet defined.",
    "important_decisions": [
      {
        "decision": "Use JWT access tokens with refresh tokens",
        "reason": "Stateless verification suits the layered service design"
      },
      {
        "decision": "Hash passwords with argon2id",
        "reason": "Current best-practice KDF with tuned memory cost"
      },
      {
        "decision": "Authorize on project membership",
        "reason": "Matches the inherited schema where tasks belong to one project"
      }
    ],
    "artifacts_created_or_modified": [
      "auth.py",
      "schema.sql",
      "auth_design.md"
    ],
    "problems_encountered": [
      "Refresh-token rotation strategy unresolved"
    ],
    "failed_attempts": [
      "In-memory session cookies - fails under horizontal scaling"
    ],
    "assumptions": [
      "15-minute access tokens, 14-day refresh tokens"
    ],
    "last_action": "Specified the get_current_user FastAPI dependency in auth.py.",
    "recommended_next_action": "Design task and project CRUD endpoints using get_current_user for authorization.",
    "notes_for_next_worker": "Every endpoint must depend on get_current_user and check project membership. Refresh-token rotation is still open - do not assume it is solved."
  },
  "reconcile": {
    "accepted": {
      "completed_tasks": 1,
      "decisions": 3,
      "artifacts": 2,
      "issues": 1,
      "failed_attempts": 2,
      "assumptions": 2
    },
    "duplicates_skipped": {
      "artifacts": 1
    },
    "warnings": [
      "artifact 'auth_design.md' appears only in the handoff report, not in the structured result"
    ],
    "unverified_artifacts": [
      "auth_design.md"
    ],
    "rejected_resolutions": []
  },
  "audit": {
    "previous_worker": "worker-3",
    "next_worker": "worker-4",
    "raw_conversation_transferred": false,
    "canonical_state_transferred": true,
    "handoff_report_included": true,
    "included_sections": [
      "assigned_task",
      "objective",
      "current_progress",
      "current_task",
      "decisions",
      "completed_work",
      "artifacts",
      "issues",
      "failed_attempts",
      "assumptions",
      "last_action",
      "next_action",
      "previous_worker_notes"
    ],
    "omitted_sections": [],
    "package_tokens": 841,
    "token_budget": 1600,
    "state_items_available": {
      "completed_tasks": 3,
      "decisions": 8,
      "artifacts": 5,
      "open_issues": 2,
      "failed_attempts": 6,
      "assumptions": 4
    }
  },
  "nextTokens": 841,
  "nextSections": 13
};

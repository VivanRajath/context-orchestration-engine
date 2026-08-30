"""The built-in demonstration task.

Five sequential workers, one continuous engineering task. Each worker's step
genuinely depends on the previous one's output, so a broken handoff shows up
immediately as a worker redesigning something that already exists.
"""

DEMO_OBJECTIVE = (
    "Build the architecture and implementation plan for a Task Management REST API "
    "using FastAPI and PostgreSQL."
)

DEMO_PLAN = [
    "Define the functional requirements and the overall system architecture for the API.",
    "Continue from the current execution state and design the PostgreSQL database schema.",
    "Continue from the current execution state and design authentication and authorization.",
    "Continue from the current execution state and design the REST API endpoints.",
    "Continue from the current execution state and review the complete architecture for "
    "inconsistencies and missing dependencies.",
]

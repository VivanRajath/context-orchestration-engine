"""Context Orchestration Engine.

N independent LLM workers. One continuous task. Structured context survives
every handoff.

Context is treated as external, portable execution state owned by the engine -
not by any model, worker, session or API key. Workers receive a compiled
context package, never a conversation transcript.

Quick start::

    from context_orchestration import Engine

    with Engine.demo(db="run.db") as engine:
        result = engine.run(
            objective="Design a notification service.",
            plan=["Define requirements.", "Design delivery.", "Review it."],
        )
        print(result.summary.raw_conversation_transfers)  # 0
"""

from context_orchestration.context.compiler import (
    ContextCompiler,
    HeuristicTokenEstimator,
    TiktokenEstimator,
    TokenEstimator,
)
from context_orchestration.context.handoff import handoff_audit, render_report
from context_orchestration.context.state import ExecutionState, create_state
from context_orchestration.core.contracts import (
    Artifact,
    Assignment,
    Assumption,
    ContextSection,
    Decision,
    FailedAttempt,
    HandoffPackage,
    HandoffRecord,
    HandoffReport,
    Issue,
    WorkerConfig,
    WorkerExecution,
    WorkerResult,
    WorkerRun,
    WorkerStatus,
)
from context_orchestration.core.orchestrator import (
    ContextOrchestrator,
    OrchestratorEvents,
    RunSummary,
    SequentialSwitchPolicy,
    SwitchPolicy,
    WorkerRegistry,
    build_assignments,
    load_registry,
)
from context_orchestration.core.reconciler import ReconciliationReport, StateReconciler
from context_orchestration.core.worker import UniversalWorker
from context_orchestration.engine import Engine, RunResult
from context_orchestration.gateway.llm_gateway import (
    GatewayError,
    GatewayResponse,
    LiteLLMGateway,
    LLMGateway,
    MockGateway,
    build_gateway,
)
from context_orchestration.storage.sqlite_store import SQLiteStore

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # facade
    "Engine",
    "RunResult",
    # state
    "ExecutionState",
    "create_state",
    # contracts
    "Artifact",
    "Assignment",
    "Assumption",
    "ContextSection",
    "Decision",
    "FailedAttempt",
    "HandoffPackage",
    "HandoffRecord",
    "HandoffReport",
    "Issue",
    "WorkerConfig",
    "WorkerExecution",
    "WorkerResult",
    "WorkerRun",
    "WorkerStatus",
    # orchestration
    "ContextOrchestrator",
    "OrchestratorEvents",
    "RunSummary",
    "SequentialSwitchPolicy",
    "SwitchPolicy",
    "WorkerRegistry",
    "UniversalWorker",
    "build_assignments",
    "load_registry",
    # context compilation
    "ContextCompiler",
    "TokenEstimator",
    "HeuristicTokenEstimator",
    "TiktokenEstimator",
    "handoff_audit",
    "render_report",
    # reconciliation
    "StateReconciler",
    "ReconciliationReport",
    # gateway
    "LLMGateway",
    "LiteLLMGateway",
    "MockGateway",
    "GatewayError",
    "GatewayResponse",
    "build_gateway",
    # storage
    "SQLiteStore",
]

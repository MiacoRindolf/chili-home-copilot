"""Pydantic schemas for API and planner. Re-export for backward compatibility."""
from ..modules import get_enabled_module_names

from .trading import (
    WatchlistAdd,
    WatchlistOut,
    TradeCreate,
    TradeClose,
    TradeUpdate,
    TradeOut,
    JournalCreate,
    JournalOut,
    AnalyzeRequest,
    InsightOut,
    BacktestRequest,
    ScanRequest,
)

from .household import (
    ChoreCreate,
    ChoreOut,
    BirthdayCreate,
    BirthdayOut,
    AddChoreBody,
    UpdateChoreBody,
    AddBirthdayBody,
    PairRequestBody,
    PairVerifyBody,
)

from .core import (
    UserOut,
    DeviceOut,
    PairCodeOut,
    BrokerCredentialOut,
    BrainWorkerControlOut,
)

from .code_brain import (
    CodeRepoOut,
    CodeInsightOut,
    CodeSnapshotOut,
    CodeHotspotOut,
    CodeReviewOut,
    CodeDepAlertOut,
    CodeQualitySnapshotOut,
)

from .reasoning_brain import (
    ReasoningUserModelOut,
    ReasoningInterestOut,
    ReasoningResearchOut,
    ReasoningAnticipationOut,
    ReasoningHypothesisOut,
    ReasoningLearningGoalOut,
)

from .project_brain import (
    ProjectAgentStateOut,
    AgentFindingOut,
    AgentResearchOut,
    AgentGoalOut,
    POQuestionOut,
    PORequirementOut,
    QATestCaseOut,
    QATestRunOut,
    QABugReportOut,
)

from .intercom import IntercomMessageOut, IntercomConsentOut

from .marketplace import MarketplaceModuleOut

from .coding_task import (
    PlanTaskCodingProfileOut,
    TaskClarificationOut,
    CodingTaskBriefOut,
    CodingTaskValidationRunOut,
    CodingAgentSuggestionOut,
    CodingAgentSuggestionApplyOut,
    CodingBlockerReportOut,
)

from .trading_brain_phase1 import (
    BrainLearningCycleRunOut,
    BrainStageJobOut,
    BrainPredictionSnapshotOut,
    BrainPredictionLineOut,
    BrainIntegrationEventOut,
)

__all__ = [
    # Household
    "ChoreCreate",
    "ChoreOut",
    "BirthdayCreate",
    "BirthdayOut",
    "AddChoreBody",
    "UpdateChoreBody",
    "AddBirthdayBody",
    "PairRequestBody",
    "PairVerifyBody",
    # Trading
    "WatchlistAdd",
    "WatchlistOut",
    "TradeCreate",
    "TradeClose",
    "TradeUpdate",
    "TradeOut",
    "JournalCreate",
    "JournalOut",
    "AnalyzeRequest",
    "InsightOut",
    "BacktestRequest",
    "ScanRequest",
    # Core
    "UserOut",
    "DeviceOut",
    "PairCodeOut",
    "BrokerCredentialOut",
    "BrainWorkerControlOut",
    # Code Brain
    "CodeRepoOut",
    "CodeInsightOut",
    "CodeSnapshotOut",
    "CodeHotspotOut",
    "CodeReviewOut",
    "CodeDepAlertOut",
    "CodeQualitySnapshotOut",
    # Reasoning Brain
    "ReasoningUserModelOut",
    "ReasoningInterestOut",
    "ReasoningResearchOut",
    "ReasoningAnticipationOut",
    "ReasoningHypothesisOut",
    "ReasoningLearningGoalOut",
    # Project Brain
    "ProjectAgentStateOut",
    "AgentFindingOut",
    "AgentResearchOut",
    "AgentGoalOut",
    "POQuestionOut",
    "PORequirementOut",
    "QATestCaseOut",
    "QATestRunOut",
    "QABugReportOut",
    # Intercom
    "IntercomMessageOut",
    "IntercomConsentOut",
    # Marketplace
    "MarketplaceModuleOut",
    # Coding Task
    "PlanTaskCodingProfileOut",
    "TaskClarificationOut",
    "CodingTaskBriefOut",
    "CodingTaskValidationRunOut",
    "CodingAgentSuggestionOut",
    "CodingAgentSuggestionApplyOut",
    "CodingBlockerReportOut",
    # Trading Brain Phase 1
    "BrainLearningCycleRunOut",
    "BrainStageJobOut",
    "BrainPredictionSnapshotOut",
    "BrainPredictionLineOut",
    "BrainIntegrationEventOut",
]

# The action planner (llm_planner.py) always needs validate_plan available,
# even if the planner UI module is disabled, so we import planner schemas
# unconditionally but only re-export them when the planner module is enabled.
from .planner import (  # type: ignore[assignment]
    validate_plan,
    ProjectBody,
    MemberBody,
    RoleBody,
    TaskBody,
    CommentBody,
    LabelBody,
    EmptyData,
    AddChoreData,
    MarkChoreDoneData,
    AddBirthdayData,
    AddPlanProjectData,
    AddPlanProjectWithTasksData,
    AddPlanTaskData,
    TaskSpec,
)

if "planner" in get_enabled_module_names():
    __all__ += [
        "validate_plan",
        "ProjectBody",
        "MemberBody",
        "RoleBody",
        "TaskBody",
        "CommentBody",
        "LabelBody",
        "EmptyData",
        "AddChoreData",
        "MarkChoreDoneData",
        "AddBirthdayData",
        "AddPlanProjectData",
        "AddPlanProjectWithTasksData",
        "AddPlanTaskData",
        "TaskSpec",
    ]

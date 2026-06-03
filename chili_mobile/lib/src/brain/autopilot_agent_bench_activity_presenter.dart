import 'autopilot_release_gate_ladder.dart';

class AutopilotAgentBenchActivityPresentation {
  const AutopilotAgentBenchActivityPresentation({
    this.agentName = '',
    this.activeRunCount = 0,
    this.openRunCount = 0,
    this.queuedRunCount = 0,
    this.workerActiveRunCount = 0,
    this.waitingRunCount = 0,
    this.pendingQuestionCount = 0,
    this.activeRunId = '',
    this.activeRunStatus = '',
    this.activeRunStage = '',
    this.activeRunPlanStatus = '',
    this.activeRunTitle = '',
    this.activeRunGoalObjective = '',
    this.activeRunGoalStatus = '',
    this.activeRunGoalProgressPercent = 0,
    this.activeRunGoalCurrentStep = '',
    this.activeRunGoalNextAction = '',
    this.activeRunGoalCompletionGate = '',
    this.goalHealthAutomation = '',
    this.goalHealthStatus = '',
    this.goalHealthSeverity = '',
    this.goalHealthTargetThread = '',
    this.goalHealthGoalStatus = '',
    this.goalHealthTokens = '',
    this.goalHealthGoalHours = '',
    this.goalHealthSessionMb = '',
    this.goalHealthSessionAge = '',
    this.goalHealthControlRisk = '',
    this.goalHealthPressure = '',
    this.goalHealthStopAction = '',
    this.goalHealthReason = '',
    this.goalHealthSignal = '',
    this.goalHealthNextAction = '',
    this.goalHealthPath = '',
    this.goalHealthOpenPath = '',
    this.goalHealthProvidedHandoffLabel = '',
    this.goalHealthProvidedHandoffCopy = '',
    this.activeRunUpdatedAt = '',
    this.activeRunLatestStepTitle = '',
    this.activeRunLatestStepStatus = '',
    this.activeRunLatestStepStage = '',
    this.activeRunStale = false,
    this.activeRunStaleKind = '',
    this.activeRunLastSeenAgeMinutes = 0,
    this.activeRunStaleAfterMinutes = 0,
    this.activeRunStaleAction = '',
    this.activeRunStaleActionLabel = '',
    this.activeRunStaleDetail = '',
    this.activeRunStaleSafeNextStep = '',
    this.activeRunStaleHandoffLabel = '',
    this.activeRunStaleHandoffCopy = '',
    this.blockedRunId = '',
    this.blockedRunStatus = '',
    this.blockedRunStage = '',
    this.blockedRunTitle = '',
    this.blockedRunReason = '',
    this.blockedRunUpdatedAt = '',
    this.blockedRunRecoveryAction = '',
    this.blockedRunRecoveryActionLabel = '',
    this.blockedRunRecoveryCategory = '',
    this.blockedRunRecoveryCanRerun = false,
    this.blockedRunRecoveryDecisionLabel = '',
    this.blockedRunRecoverySafeNextStep = '',
    this.blockedRunRecoveryLastFailedStep = '',
    this.blockedRunRecoveryLastFailedExitCode = '',
    this.blockedRunRecoveryLastFailedSummary = '',
    this.blockedRunRecoveryHandoffLabel = '',
    this.blockedRunRecoveryHandoffCopy = '',
    this.operatingNeedsInput = false,
    this.statusBlocked = false,
    this.scheduleEnabled = false,
    this.sourceActive = false,
    this.scheduledQualityStatus = '',
    this.scheduledQualityTotal = 0,
    this.scheduledQualityPassedCount = 0,
    this.scheduledQualityRepairedCount = 0,
    this.scheduledQualityLowQualityCount = 0,
    this.scheduledQualityAverageScoreLabel = '',
    this.scheduledQualityIssue = '',
    this.scheduledQualityIssueLabel = '',
    this.scheduledQualityIssueCount = 0,
    this.scheduledQualityNextAction = '',
    this.scheduledQualityLatestRunId = '',
    this.scheduledQualityLatestStatus = '',
    this.scheduledQualityLatestScoreLabel = '',
    this.scheduledQualityLatestInitialScoreLabel = '',
    this.scheduledQualityHandoffLabel = '',
    this.scheduledQualityHandoffCopy = '',
    this.benchmarkProfile = '',
    this.benchmarkPromotionStatus = '',
    this.benchmarkSelectedScenariosStatus = '',
    this.benchmarkPromotionScope = '',
    this.benchmarkPassRate = '',
    this.benchmarkSourceStability = '',
    this.benchmarkEvidenceGapLabels = const <String>[],
    this.benchmarkEvidenceNextAction = '',
    this.benchmarkEvidenceHandoffLabel = '',
    this.benchmarkEvidenceHandoffCopy = '',
    this.benchmarkEvidenceIntakeStatus = '',
    this.benchmarkEvidenceIntakeReadyCount = 0,
    this.benchmarkEvidenceIntakeRequiredCount = 0,
    this.benchmarkEvidenceIntakeMissingCount = 0,
    this.benchmarkEvidenceIntakeNextAction = '',
    this.benchmarkEvidenceIntakeSourceRoot = '',
    this.benchmarkEvidenceIntakePromptPackManifest = '',
    this.benchmarkEvidenceIntakeMissingSources = const <String>[],
    this.benchmarkEvidenceRecoverySources = const <String>[],
    this.benchmarkEvidenceRecoveryDetail = '',
    this.benchmarkEvidenceIntakeLocalModelStatus = '',
    this.benchmarkEvidenceIntakeLocalModelTimeoutSalvagedCases =
        const <String>[],
    this.kpiBlockedPrCount = 0,
    this.kpiReadyCandidateCount = 0,
    this.kpiTempArtifactCount = 0,
    this.kpiScoreLabel = '',
    this.kpiSeverity = '',
    this.kpiSignal = '',
    this.kpiNextAction = '',
    this.kpiPrNumbers = const <String>[],
    this.kpiPrBlockerDirtyCount = 0,
    this.kpiPrBlockerNoChecksCount = 0,
    this.kpiPrBlockerFailingCount = 0,
    this.kpiPrBlockerNonDraftCount = 0,
    this.kpiPrBlockerTopPr = '',
    this.kpiPrBlockerTopBranch = '',
    this.kpiPrBlockerTopMerge = '',
    this.kpiPrBlockerTopCi = '',
    this.kpiPrBlockerTopPosture = '',
    this.kpiPrBlockerProofFloor = '',
    this.kpiPrBlockerGateState = '',
    this.kpiPrBlockerGateLabel = '',
    this.kpiPrBlockerAllowedDecisions = const <String>[],
    this.kpiPrBlockerRequiredProof = '',
    this.kpiPrBlockerBlockedAction = '',
    this.kpiGeneratedStateRefreshRequired = false,
    this.kpiGeneratedStateRefreshTarget = '',
    this.kpiGeneratedStateDriftCount = 0,
    this.kpiGeneratedStateDriftKind = '',
    this.kpiGeneratedStateDriftLabel = '',
    this.kpiGeneratedStateDriftSummary = '',
    this.kpiGeneratedStateBoardGenerated = '',
    this.kpiGeneratedStateScorecardGenerated = '',
    this.kpiGeneratedStateScorecardTopAction = '',
    this.kpiGeneratedStateBoardTopAction = '',
    this.kpiGeneratedStateNextAction = '',
    this.kpiGeneratedStatePath = '',
    this.kpiGeneratedStateOpenPath = '',
    this.expediteOpenPrBlockerCount = 0,
    this.expediteReadyCandidateCount = 0,
    this.expediteStableInboxRequestCount = 0,
    this.expediteControlPlaneCount = 0,
    this.expediteSeverity = '',
    this.expediteSignal = '',
    this.expediteNextAction = '',
    this.expeditePrNumbers = const <String>[],
    this.expediteTopRank = 0,
    this.expediteTopType = '',
    this.expediteTopEvidence = '',
    this.expediteTopOwner = '',
    this.expediteBoardPath = '',
    this.expediteBoardOpenPath = '',
    this.ownerReadyFirstCount = 0,
    this.ownerReadyFirstPrimaryOwnerCount = 0,
    this.ownerReadyFirstSupportCount = 0,
    this.ownerReadyFirstTopRank = 0,
    this.ownerReadyFirstPr = '',
    this.ownerReadyFirstPrNumber = '',
    this.ownerReadyFirstTitle = '',
    this.ownerReadyFirstOwner = '',
    this.ownerReadyFirstState = '',
    this.ownerReadyFirstRequiredLanes = '',
    this.ownerReadyFirstExistingRequest = '',
    this.ownerReadyFirstExistingRequestOpenPath = '',
    this.ownerReadyFirstNextAction = '',
    this.ownerReadyFirstLaneRole = '',
    this.ownerReadyFirstPath = '',
    this.ownerReadyFirstOpenPath = '',
    this.ownerReadyFirstGeneratedAt = '',
    this.ownerReadyFirstSha256 = '',
    this.ownerReadyFirstSafety = '',
    this.ownerReadyFirstHandoffCopy = '',
    this.stableInboxTopRank = 0,
    this.stableInboxRequestPath = '',
    this.stableInboxRequestOpenPath = '',
    this.stableInboxRequestSha256 = '',
    this.stableInboxRequestPriority = '',
    this.stableInboxRequestFrom = '',
    this.stableInboxRequestTo = '',
    this.stableInboxRequestBacklogId = '',
    this.stableInboxRequestPreview = '',
    this.stableInboxRequestExpectedDeliverable = '',
    this.stableInboxRequestSuccessCriteria = '',
    this.stableInboxRequestSafety = '',
    this.stableInboxStaleForLatest = false,
    this.stableInboxLatestRequestPath = '',
    this.stableInboxLatestRequestOpenPath = '',
    this.stableInboxLatestRequestSha256 = '',
    this.stableInboxLatestRequestCreated = '',
    this.stableInboxLatestRequestPriority = '',
    this.stableInboxLatestRequestFrom = '',
    this.stableInboxLatestRequestTo = '',
    this.stableInboxLatestRequestBacklogId = '',
    this.stableInboxLatestRequestPreview = '',
    this.stableInboxLatestRequestExpectedDeliverable = '',
    this.stableInboxLatestRequestSuccessCriteria = '',
    this.stableInboxLatestRequestSafety = '',
    this.stableInboxLatestHandoffCopy = '',
    this.stableInboxFreshnessDetail = '',
    this.stableInboxRequestProcessed = false,
    this.stableInboxRequestProcessedUtc = '',
    this.stableInboxRequestProcessedStatus = '',
    this.stableInboxRequestProcessedResult = '',
    this.stableInboxRequestProcessedReportPath = '',
    this.stableInboxRequestProcessedSummary = '',
    this.stableInboxLatestRequestProcessed = false,
    this.stableInboxLatestRequestProcessedUtc = '',
    this.stableInboxLatestRequestProcessedStatus = '',
    this.stableInboxLatestRequestProcessedResult = '',
    this.stableInboxLatestRequestProcessedReportPath = '',
    this.stableInboxLatestRequestProcessedSummary = '',
    this.stableInboxProcessedDetail = '',
    this.stableInboxHandoffLabel = '',
    this.stableInboxHandoffCopy = '',
    this.stableInboxLiveBrokerActionCount = 0,
    this.stableInboxLiveBrokerLabel = '',
    this.stableInboxLiveBrokerDetail = '',
    this.stableInboxLiveBrokerProofFloorUtc = '',
    this.stableInboxControlPlaneActionCount = 0,
    this.stableInboxControlPlaneLabel = '',
    this.stableInboxControlPlaneDetail = '',
    this.stableInboxControlPlaneProofFloorUtc = '',
    this.stableInboxControlPlaneThreadIds = const <String>[],
    this.flowAgent = '',
    this.flowPendingCount = 0,
    this.flowStablePendingCount = 0,
    this.flowUrgentCount = 0,
    this.flowControlPlaneBlockerCount = 0,
    this.flowQuarantinedTargetCount = 0,
    this.flowQuarantineThreadId = '',
    this.flowQuarantineStatus = '',
    this.flowQuarantineOperatorLabel = '',
    this.flowQuarantineRequiredProof = '',
    this.flowQuarantineOperatorGuidance = '',
    this.flowQuarantineSessionAgeMinutes = -1,
    this.flowQuarantineSessionGoalStatus = '',
    this.flowQuarantineSessionPath = '',
    this.flowQuarantineActivityState = '',
    this.flowQuarantineTargetLastWriteUtc = '',
    this.flowQuarantineProofFloorUtc = '',
    this.flowQuarantineProofWindowMinutes = 0,
    this.flowQuarantineProofRemainingMinutes = -1,
    this.flowQuarantineProofNextCheckUtc = '',
    this.flowQuarantineProofSatisfied = false,
    this.flowPausedAutomationCount = 0,
    this.flowPausedAutomationId = '',
    this.flowPausedAutomationName = '',
    this.flowPausedAutomationStatus = '',
    this.flowPausedAutomationThreadId = '',
    this.flowPausedAutomationSessionAgeMinutes = -1,
    this.flowPausedAutomationThresholdMinutes = -1,
    this.flowPausedAutomationGoalStatus = '',
    this.flowPausedAutomationSessionPath = '',
    this.flowPausedAutomationGuidance = '',
    this.flowPausedAutomationCoveredByQuarantine = false,
    this.flowPausedAutomationHandoffLabel = '',
    this.flowPausedAutomationHandoffCopy = '',
    this.flowPausedAutomationHandoffMutatesControlPlane = false,
    this.flowWorktreeHygieneCount = 0,
    this.flowDirtyWorktreeCount = 0,
    this.flowDetachedWorktreeCount = 0,
    this.flowWorktreeMergeConflictCount = 0,
    this.flowWorktreeChangeCount = 0,
    this.flowRepositoryRootDirtyCount = 0,
    this.flowRepositoryRootChangeCount = 0,
    this.flowRepositoryRootMergeConflictCount = 0,
    this.flowRepositoryRootBranch = '',
    this.flowSourceIsolationBlocked = false,
    this.flowCaptainExpiredCount = 0,
    this.flowShapeInvalidCount = 0,
    this.flowMailboxMalformedCorrectionCount = 0,
    this.flowSupersededShapeInvalidCount = 0,
    this.flowReviewMismatchCount = 0,
    this.flowActiveLockStarvationCount = 0,
    this.flowStaleTempPublishCount = 0,
    this.flowSignal = '',
    this.flowNextAction = '',
    this.flowNextActionPath = '',
    this.flowNextActionOpenPath = '',
    this.flowNextActionHandoffLabel = '',
    this.flowNextActionHandoffCopy = '',
    this.flowNextActionHandoffMutatesControlPlane = false,
    this.flowSeverity = '',
  });

  final String agentName;
  final int activeRunCount;
  final int openRunCount;
  final int queuedRunCount;
  final int workerActiveRunCount;
  final int waitingRunCount;
  final int pendingQuestionCount;
  final String activeRunId;
  final String activeRunStatus;
  final String activeRunStage;
  final String activeRunPlanStatus;
  final String activeRunTitle;
  final String activeRunGoalObjective;
  final String activeRunGoalStatus;
  final int activeRunGoalProgressPercent;
  final String activeRunGoalCurrentStep;
  final String activeRunGoalNextAction;
  final String activeRunGoalCompletionGate;
  final String goalHealthAutomation;
  final String goalHealthStatus;
  final String goalHealthSeverity;
  final String goalHealthTargetThread;
  final String goalHealthGoalStatus;
  final String goalHealthTokens;
  final String goalHealthGoalHours;
  final String goalHealthSessionMb;
  final String goalHealthSessionAge;
  final String goalHealthControlRisk;
  final String goalHealthPressure;
  final String goalHealthStopAction;
  final String goalHealthReason;
  final String goalHealthSignal;
  final String goalHealthNextAction;
  final String goalHealthPath;
  final String goalHealthOpenPath;
  final String goalHealthProvidedHandoffLabel;
  final String goalHealthProvidedHandoffCopy;
  final String activeRunUpdatedAt;
  final String activeRunLatestStepTitle;
  final String activeRunLatestStepStatus;
  final String activeRunLatestStepStage;
  final bool activeRunStale;
  final String activeRunStaleKind;
  final int activeRunLastSeenAgeMinutes;
  final int activeRunStaleAfterMinutes;
  final String activeRunStaleAction;
  final String activeRunStaleActionLabel;
  final String activeRunStaleDetail;
  final String activeRunStaleSafeNextStep;
  final String activeRunStaleHandoffLabel;
  final String activeRunStaleHandoffCopy;
  final String blockedRunId;
  final String blockedRunStatus;
  final String blockedRunStage;
  final String blockedRunTitle;
  final String blockedRunReason;
  final String blockedRunUpdatedAt;
  final String blockedRunRecoveryAction;
  final String blockedRunRecoveryActionLabel;
  final String blockedRunRecoveryCategory;
  final bool blockedRunRecoveryCanRerun;
  final String blockedRunRecoveryDecisionLabel;
  final String blockedRunRecoverySafeNextStep;
  final String blockedRunRecoveryLastFailedStep;
  final String blockedRunRecoveryLastFailedExitCode;
  final String blockedRunRecoveryLastFailedSummary;
  final String blockedRunRecoveryHandoffLabel;
  final String blockedRunRecoveryHandoffCopy;
  final bool operatingNeedsInput;
  final bool statusBlocked;
  final bool scheduleEnabled;
  final bool sourceActive;
  final String scheduledQualityStatus;
  final int scheduledQualityTotal;
  final int scheduledQualityPassedCount;
  final int scheduledQualityRepairedCount;
  final int scheduledQualityLowQualityCount;
  final String scheduledQualityAverageScoreLabel;
  final String scheduledQualityIssue;
  final String scheduledQualityIssueLabel;
  final int scheduledQualityIssueCount;
  final String scheduledQualityNextAction;
  final String scheduledQualityLatestRunId;
  final String scheduledQualityLatestStatus;
  final String scheduledQualityLatestScoreLabel;
  final String scheduledQualityLatestInitialScoreLabel;
  final String scheduledQualityHandoffLabel;
  final String scheduledQualityHandoffCopy;
  final String benchmarkProfile;
  final String benchmarkPromotionStatus;
  final String benchmarkSelectedScenariosStatus;
  final String benchmarkPromotionScope;
  final String benchmarkPassRate;
  final String benchmarkSourceStability;
  final List<String> benchmarkEvidenceGapLabels;
  final String benchmarkEvidenceNextAction;
  final String benchmarkEvidenceHandoffLabel;
  final String benchmarkEvidenceHandoffCopy;
  final String benchmarkEvidenceIntakeStatus;
  final int benchmarkEvidenceIntakeReadyCount;
  final int benchmarkEvidenceIntakeRequiredCount;
  final int benchmarkEvidenceIntakeMissingCount;
  final String benchmarkEvidenceIntakeNextAction;
  final String benchmarkEvidenceIntakeSourceRoot;
  final String benchmarkEvidenceIntakePromptPackManifest;
  final List<String> benchmarkEvidenceIntakeMissingSources;
  final List<String> benchmarkEvidenceRecoverySources;
  final String benchmarkEvidenceRecoveryDetail;
  final String benchmarkEvidenceIntakeLocalModelStatus;
  final List<String> benchmarkEvidenceIntakeLocalModelTimeoutSalvagedCases;
  final int kpiBlockedPrCount;
  final int kpiReadyCandidateCount;
  final int kpiTempArtifactCount;
  final String kpiScoreLabel;
  final String kpiSeverity;
  final String kpiSignal;
  final String kpiNextAction;
  final List<String> kpiPrNumbers;
  final int kpiPrBlockerDirtyCount;
  final int kpiPrBlockerNoChecksCount;
  final int kpiPrBlockerFailingCount;
  final int kpiPrBlockerNonDraftCount;
  final String kpiPrBlockerTopPr;
  final String kpiPrBlockerTopBranch;
  final String kpiPrBlockerTopMerge;
  final String kpiPrBlockerTopCi;
  final String kpiPrBlockerTopPosture;
  final String kpiPrBlockerProofFloor;
  final String kpiPrBlockerGateState;
  final String kpiPrBlockerGateLabel;
  final List<String> kpiPrBlockerAllowedDecisions;
  final String kpiPrBlockerRequiredProof;
  final String kpiPrBlockerBlockedAction;
  final bool kpiGeneratedStateRefreshRequired;
  final String kpiGeneratedStateRefreshTarget;
  final int kpiGeneratedStateDriftCount;
  final String kpiGeneratedStateDriftKind;
  final String kpiGeneratedStateDriftLabel;
  final String kpiGeneratedStateDriftSummary;
  final String kpiGeneratedStateBoardGenerated;
  final String kpiGeneratedStateScorecardGenerated;
  final String kpiGeneratedStateScorecardTopAction;
  final String kpiGeneratedStateBoardTopAction;
  final String kpiGeneratedStateNextAction;
  final String kpiGeneratedStatePath;
  final String kpiGeneratedStateOpenPath;
  final int expediteOpenPrBlockerCount;
  final int expediteReadyCandidateCount;
  final int expediteStableInboxRequestCount;
  final int expediteControlPlaneCount;
  final String expediteSeverity;
  final String expediteSignal;
  final String expediteNextAction;
  final List<String> expeditePrNumbers;
  final int expediteTopRank;
  final String expediteTopType;
  final String expediteTopEvidence;
  final String expediteTopOwner;
  final String expediteBoardPath;
  final String expediteBoardOpenPath;
  final int ownerReadyFirstCount;
  final int ownerReadyFirstPrimaryOwnerCount;
  final int ownerReadyFirstSupportCount;
  final int ownerReadyFirstTopRank;
  final String ownerReadyFirstPr;
  final String ownerReadyFirstPrNumber;
  final String ownerReadyFirstTitle;
  final String ownerReadyFirstOwner;
  final String ownerReadyFirstState;
  final String ownerReadyFirstRequiredLanes;
  final String ownerReadyFirstExistingRequest;
  final String ownerReadyFirstExistingRequestOpenPath;
  final String ownerReadyFirstNextAction;
  final String ownerReadyFirstLaneRole;
  final String ownerReadyFirstPath;
  final String ownerReadyFirstOpenPath;
  final String ownerReadyFirstGeneratedAt;
  final String ownerReadyFirstSha256;
  final String ownerReadyFirstSafety;
  final String ownerReadyFirstHandoffCopy;
  final int stableInboxTopRank;
  final String stableInboxRequestPath;
  final String stableInboxRequestOpenPath;
  final String stableInboxRequestSha256;
  final String stableInboxRequestPriority;
  final String stableInboxRequestFrom;
  final String stableInboxRequestTo;
  final String stableInboxRequestBacklogId;
  final String stableInboxRequestPreview;
  final String stableInboxRequestExpectedDeliverable;
  final String stableInboxRequestSuccessCriteria;
  final String stableInboxRequestSafety;
  final bool stableInboxStaleForLatest;
  final String stableInboxLatestRequestPath;
  final String stableInboxLatestRequestOpenPath;
  final String stableInboxLatestRequestSha256;
  final String stableInboxLatestRequestCreated;
  final String stableInboxLatestRequestPriority;
  final String stableInboxLatestRequestFrom;
  final String stableInboxLatestRequestTo;
  final String stableInboxLatestRequestBacklogId;
  final String stableInboxLatestRequestPreview;
  final String stableInboxLatestRequestExpectedDeliverable;
  final String stableInboxLatestRequestSuccessCriteria;
  final String stableInboxLatestRequestSafety;
  final String stableInboxLatestHandoffCopy;
  final String stableInboxFreshnessDetail;
  final bool stableInboxRequestProcessed;
  final String stableInboxRequestProcessedUtc;
  final String stableInboxRequestProcessedStatus;
  final String stableInboxRequestProcessedResult;
  final String stableInboxRequestProcessedReportPath;
  final String stableInboxRequestProcessedSummary;
  final bool stableInboxLatestRequestProcessed;
  final String stableInboxLatestRequestProcessedUtc;
  final String stableInboxLatestRequestProcessedStatus;
  final String stableInboxLatestRequestProcessedResult;
  final String stableInboxLatestRequestProcessedReportPath;
  final String stableInboxLatestRequestProcessedSummary;
  final String stableInboxProcessedDetail;
  final String stableInboxHandoffLabel;
  final String stableInboxHandoffCopy;
  final int stableInboxLiveBrokerActionCount;
  final String stableInboxLiveBrokerLabel;
  final String stableInboxLiveBrokerDetail;
  final String stableInboxLiveBrokerProofFloorUtc;
  final int stableInboxControlPlaneActionCount;
  final String stableInboxControlPlaneLabel;
  final String stableInboxControlPlaneDetail;
  final String stableInboxControlPlaneProofFloorUtc;
  final List<String> stableInboxControlPlaneThreadIds;
  final String flowAgent;
  final int flowPendingCount;
  final int flowStablePendingCount;
  final int flowUrgentCount;
  final int flowControlPlaneBlockerCount;
  final int flowQuarantinedTargetCount;
  final String flowQuarantineThreadId;
  final String flowQuarantineStatus;
  final String flowQuarantineOperatorLabel;
  final String flowQuarantineRequiredProof;
  final String flowQuarantineOperatorGuidance;
  final int flowQuarantineSessionAgeMinutes;
  final String flowQuarantineSessionGoalStatus;
  final String flowQuarantineSessionPath;
  final String flowQuarantineActivityState;
  final String flowQuarantineTargetLastWriteUtc;
  final String flowQuarantineProofFloorUtc;
  final int flowQuarantineProofWindowMinutes;
  final int flowQuarantineProofRemainingMinutes;
  final String flowQuarantineProofNextCheckUtc;
  final bool flowQuarantineProofSatisfied;
  final int flowPausedAutomationCount;
  final String flowPausedAutomationId;
  final String flowPausedAutomationName;
  final String flowPausedAutomationStatus;
  final String flowPausedAutomationThreadId;
  final int flowPausedAutomationSessionAgeMinutes;
  final int flowPausedAutomationThresholdMinutes;
  final String flowPausedAutomationGoalStatus;
  final String flowPausedAutomationSessionPath;
  final String flowPausedAutomationGuidance;
  final bool flowPausedAutomationCoveredByQuarantine;
  final String flowPausedAutomationHandoffLabel;
  final String flowPausedAutomationHandoffCopy;
  final bool flowPausedAutomationHandoffMutatesControlPlane;
  final int flowWorktreeHygieneCount;
  final int flowDirtyWorktreeCount;
  final int flowDetachedWorktreeCount;
  final int flowWorktreeMergeConflictCount;
  final int flowWorktreeChangeCount;
  final int flowRepositoryRootDirtyCount;
  final int flowRepositoryRootChangeCount;
  final int flowRepositoryRootMergeConflictCount;
  final String flowRepositoryRootBranch;
  final bool flowSourceIsolationBlocked;
  final int flowCaptainExpiredCount;
  final int flowShapeInvalidCount;
  final int flowMailboxMalformedCorrectionCount;
  final int flowSupersededShapeInvalidCount;
  final int flowReviewMismatchCount;
  final int flowActiveLockStarvationCount;
  final int flowStaleTempPublishCount;
  final String flowSignal;
  final String flowNextAction;
  final String flowNextActionPath;
  final String flowNextActionOpenPath;
  final String flowNextActionHandoffLabel;
  final String flowNextActionHandoffCopy;
  final bool flowNextActionHandoffMutatesControlPlane;
  final String flowSeverity;

  bool get hasRunStateBreakdown =>
      queuedRunCount > 0 || workerActiveRunCount > 0 || waitingRunCount > 0;
  int get visibleActiveRunCount =>
      hasRunStateBreakdown ? workerActiveRunCount : activeRunCount;
  int get classifiedOpenRunCount =>
      visibleActiveRunCount + queuedRunCount + waitingRunCount;
  bool get hasQueuedRuns => queuedRunCount > 0 || activeRunStatus == 'queued';
  String get activeChatLabel => visibleActiveRunCount > 0
      ? '$visibleActiveRunCount active chat${visibleActiveRunCount == 1 ? '' : 's'}'
      : '';
  String get activeChipLabel =>
      visibleActiveRunCount > 0 ? '$visibleActiveRunCount active' : '';
  String get activeGoalChipLabel {
    if (activeRunGoalObjective.isEmpty) return '';
    if (activeRunGoalProgressPercent > 0) {
      return 'goal ${activeRunGoalProgressPercent.clamp(0, 100)}%';
    }
    if (activeRunGoalStatus.isNotEmpty) {
      return 'goal ${activeRunGoalStatus.replaceAll('_', ' ')}';
    }
    return 'goal active';
  }

  double? get activeGoalProgressFraction {
    if (activeRunGoalObjective.isEmpty) return null;
    return activeRunGoalProgressPercent.clamp(0, 100).toDouble() / 100;
  }

  bool get hasActiveGoal => activeRunGoalObjective.isNotEmpty;

  String get activeGoalContractLabel {
    if (!hasActiveGoal) return '';
    final parts = <String>[
      if (activeRunGoalCurrentStep.isNotEmpty)
        'current $activeRunGoalCurrentStep',
      if (activeRunGoalNextAction.isNotEmpty) 'next $activeRunGoalNextAction',
      if (activeRunGoalCompletionGate.isNotEmpty)
        'gate $activeRunGoalCompletionGate',
    ];
    if (parts.isEmpty) return '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Goal contract: ${parts.join(' | ')}',
      180,
    );
  }

  bool get hasGoalReceiptPressure =>
      hasScheduledQualityPressure &&
      (AutopilotAgentBenchActivityPresenter._isGoalReceiptIssue(
            scheduledQualityIssue,
          ) ||
          AutopilotAgentBenchActivityPresenter._isGoalReceiptIssue(
            scheduledQualityIssueLabel,
          ) ||
          AutopilotAgentBenchActivityPresenter._isGoalReceiptIssue(
            scheduledQualityNextAction,
          ));

  bool get hasPursuingGoalProofPressure =>
      hasScheduledQualityPressure &&
      (AutopilotAgentBenchActivityPresenter._isPursuingGoalProofIssue(
            scheduledQualityIssue,
          ) ||
          AutopilotAgentBenchActivityPresenter._isPursuingGoalProofIssue(
            scheduledQualityIssueLabel,
          ) ||
          AutopilotAgentBenchActivityPresenter._isPursuingGoalProofIssue(
            scheduledQualityNextAction,
          ) ||
          AutopilotAgentBenchActivityPresenter._isPursuingGoalProofIssue(
            scheduledQualityHandoffCopy,
          ));

  String get pursuingGoalProofIssueLabel {
    if (!hasPursuingGoalProofPressure) return '';
    final issue = scheduledQualityIssue.toLowerCase();
    final label = scheduledQualityIssueLabel.trim();
    if (issue.contains('goal_evidence_unbound') ||
        label.toLowerCase().contains('evidence unbound')) {
      return 'goal evidence unbound';
    }
    if (issue.contains('goal_progress_overclaimed') ||
        label.toLowerCase().contains('progress overclaimed')) {
      return 'goal progress overclaimed';
    }
    if (issue.contains('goal_objective_mismatch') ||
        label.toLowerCase().contains('objective mismatch')) {
      return 'goal objective mismatch';
    }
    return label.isNotEmpty ? label : 'objective proof weak';
  }

  String get pursuingGoalProofDetail {
    if (!hasPursuingGoalProofPressure) return '';
    return 'Goal progress remains untrusted until findings, evidence, or checks name the active objective, scheduled request, or completion gate.';
  }

  bool get hasPrPublicationReceiptPressure =>
      hasScheduledQualityPressure &&
      (AutopilotAgentBenchActivityPresenter._isPrPublicationReceiptIssue(
            scheduledQualityIssue,
          ) ||
          AutopilotAgentBenchActivityPresenter._isPrPublicationReceiptIssue(
            scheduledQualityIssueLabel,
          ) ||
          AutopilotAgentBenchActivityPresenter._isPrPublicationReceiptIssue(
            scheduledQualityNextAction,
          ) ||
          AutopilotAgentBenchActivityPresenter._isPrPublicationReceiptIssue(
            scheduledQualityHandoffLabel,
          ) ||
          AutopilotAgentBenchActivityPresenter._isPrPublicationReceiptIssue(
            scheduledQualityHandoffCopy,
          ));

  bool get hasScheduledQualityHandoff =>
      scheduledQualityHandoffCopy.trim().isNotEmpty;

  bool get hasPursuingGoalFocus =>
      hasActiveGoal ||
      hasGoalHealthPressure ||
      hasGoalReceiptPressure ||
      hasPursuingGoalProofPressure;

  bool get hasGoalHealthPressure {
    final severity = goalHealthSeverity.toLowerCase();
    final pressure = goalHealthPressure.toLowerCase();
    final controlRisk = goalHealthControlRisk.toLowerCase();
    return goalHealthCritical ||
        severity == 'warning' ||
        pressure.startsWith('red') ||
        pressure.startsWith('yellow') ||
        controlRisk == 'red' ||
        controlRisk == 'yellow';
  }

  bool get goalHealthCritical =>
      goalHealthSeverity.toLowerCase() == 'high' ||
      goalHealthPressure.toLowerCase().startsWith('red') ||
      goalHealthControlRisk.toLowerCase() == 'red' ||
      goalHealthPausedActive;

  bool get goalHealthPausedActive =>
      goalHealthStatus.toUpperCase() == 'PAUSED' &&
      goalHealthGoalStatus.toLowerCase() == 'active';

  String get goalHealthActionChipLabel {
    if (!hasGoalHealthPressure) return '';
    final action = goalHealthStopAction.toLowerCase();
    if (action.contains('containment_closeout')) return 'contain goal';
    if (action.contains('hard_stop')) return 'hard stop goal';
    if (action.contains('manual_goal_fastlane') ||
        action.contains('fastlane_required')) {
      return 'fastlane goal';
    }
    if (goalHealthPausedActive) return 'contain goal';
    if (goalHealthCritical) return 'reduce goal load';
    return 'watch goal load';
  }

  String get goalHealthActionDetail {
    if (!hasGoalHealthPressure) return '';
    final action = goalHealthStopAction.toLowerCase();
    final target = goalHealthTargetThread.length > 8
        ? goalHealthTargetThread.substring(0, 8)
        : goalHealthTargetThread;
    final subject = [
      if (goalHealthAutomation.isNotEmpty) goalHealthAutomation,
      if (target.isNotEmpty) 'target $target',
    ].join(' ');
    final prefix = subject.isEmpty ? 'Goal action' : 'Goal action for $subject';
    if (action.contains('containment_closeout')) {
      return AutopilotAgentBenchActivityPresenter._clip(
        '$prefix: paste the containment stop message, require one zero-hold closeout, then wait for quiet proof before source/runtime trust.',
        240,
      );
    }
    if (action.contains('hard_stop')) {
      return AutopilotAgentBenchActivityPresenter._clip(
        '$prefix: paste the hard-stop message and stop heartbeat/read-only/status churn before RecoveryWave work resumes.',
        240,
      );
    }
    if (action.contains('manual_goal_fastlane') ||
        action.contains('fastlane_required')) {
      return AutopilotAgentBenchActivityPresenter._clip(
        '$prefix: fastlane only; work the PR/blocker/disposition item or publish one exact next-owner blocker.',
        240,
      );
    }
    if (goalHealthPausedActive) {
      return AutopilotAgentBenchActivityPresenter._clip(
        '$prefix: contain the paused active target before accepting source/runtime trust or assigning broad work.',
        220,
      );
    }
    if (goalHealthCritical) {
      return AutopilotAgentBenchActivityPresenter._clip(
        '$prefix: reduce, defer, or close work before assigning more source-capable load.',
        220,
      );
    }
    return AutopilotAgentBenchActivityPresenter._clip(
      '$prefix: monitor goal load before adding routine work.',
      180,
    );
  }

  String get goalHealthChipLabel {
    if (!hasGoalHealthPressure) return '';
    if (goalHealthPausedActive) {
      return 'paused goal active';
    }
    if (goalHealthCritical) return 'goal overloaded';
    if (goalHealthSeverity.toLowerCase() == 'warning' ||
        goalHealthPressure.toLowerCase().startsWith('yellow')) {
      return 'goal warm';
    }
    return 'goal active';
  }

  String get goalHealthTokensChipLabel {
    final label = AutopilotAgentBenchActivityPresenter._compactLargeNumberLabel(
      goalHealthTokens,
    );
    return label.isEmpty ? '' : '$label tok';
  }

  String get goalHealthLabel {
    if (!hasGoalHealthPressure) return '';
    final status =
        goalHealthChipLabel.isEmpty ? 'goal pressure' : goalHealthChipLabel;
    return AutopilotAgentBenchActivityPresenter._clip(
      'Goal health: $status',
      96,
    );
  }

  String get goalHealthDetail {
    if (!hasGoalHealthPressure) return '';
    final thread = goalHealthTargetThread.length > 8
        ? goalHealthTargetThread.substring(0, 8)
        : goalHealthTargetThread;
    final parts = <String>[
      if (goalHealthAutomation.isNotEmpty) goalHealthAutomation,
      if (thread.isNotEmpty) 'target $thread',
      if (goalHealthTokensChipLabel.isNotEmpty) goalHealthTokensChipLabel,
      if (goalHealthGoalHours.isNotEmpty) '${goalHealthGoalHours}h goal',
      if (goalHealthSessionMb.isNotEmpty) '$goalHealthSessionMb MB session',
      if (goalHealthSessionAge.isNotEmpty) 'age $goalHealthSessionAge',
      if (goalHealthStopAction.isNotEmpty)
        goalHealthStopAction.replaceAll('_', ' '),
      if (goalHealthActionDetail.isNotEmpty) goalHealthActionDetail,
      if (goalHealthPressure.isNotEmpty) goalHealthPressure,
      if (goalHealthNextAction.isNotEmpty) goalHealthNextAction,
      if (goalHealthReason.isNotEmpty) goalHealthReason,
    ];
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(' | '), 220);
  }

  bool get hasGoalHealthTarget =>
      hasGoalHealthPressure &&
      (goalHealthPath.isNotEmpty ||
          goalHealthOpenPath.isNotEmpty ||
          hasGoalHealthHandoff);

  String get goalHealthPrimaryOpenPath =>
      goalHealthOpenPath.isNotEmpty ? goalHealthOpenPath : goalHealthPath;

  String get goalHealthOpenActionLabel =>
      goalHealthCritical ? 'Review goal pressure' : 'Open goal health';

  String get goalHealthHandoffLabel {
    if (!hasGoalHealthHandoff) return '';
    return goalHealthProvidedHandoffLabel.isNotEmpty
        ? goalHealthProvidedHandoffLabel
        : 'Copy goal-pressure handoff';
  }

  bool get hasGoalHealthHandoff => goalHealthHandoffCopy.isNotEmpty;

  String get goalHealthHandoffCopy {
    if (!hasGoalHealthPressure) return '';
    if (goalHealthProvidedHandoffCopy.isNotEmpty) {
      return goalHealthProvidedHandoffCopy;
    }
    final stopInstruction = goalHealthStopInstruction;
    final quietProof = goalHealthQuietProofInstruction;
    final lines = <String>[
      'Project Autopilot agent goal-pressure handoff',
      if (stopInstruction.isNotEmpty) 'Copy-ready one-liner: $stopInstruction',
      if (goalHealthAutomation.isNotEmpty) 'Automation: $goalHealthAutomation',
      if (goalHealthTargetThread.isNotEmpty)
        'Target thread: $goalHealthTargetThread',
      if (goalHealthStatus.isNotEmpty) 'Automation status: $goalHealthStatus',
      if (goalHealthGoalStatus.isNotEmpty) 'Goal status: $goalHealthGoalStatus',
      if (goalHealthPressure.isNotEmpty) 'Pressure: $goalHealthPressure',
      if (goalHealthStopAction.isNotEmpty) 'Stop action: $goalHealthStopAction',
      if (goalHealthControlRisk.isNotEmpty)
        'Control risk: $goalHealthControlRisk',
      if (goalHealthTokens.isNotEmpty) 'Tokens used: $goalHealthTokens',
      if (goalHealthGoalHours.isNotEmpty) 'Goal hours: $goalHealthGoalHours',
      if (goalHealthSessionMb.isNotEmpty) 'Session MB: $goalHealthSessionMb',
      if (goalHealthSessionAge.isNotEmpty) 'Session age: $goalHealthSessionAge',
      if (goalHealthReason.isNotEmpty) 'Reason: $goalHealthReason',
      if (goalHealthNextAction.isNotEmpty) 'Next action: $goalHealthNextAction',
      if (quietProof.isNotEmpty) 'Quiet proof: $quietProof',
      if (goalHealthPath.isNotEmpty) 'Board: $goalHealthPath',
      if (goalHealthOpenPath.isNotEmpty) 'Open path: $goalHealthOpenPath',
      'Permission boundary: goal containment only. This copied handoff does not authorize thread cancellation, automation mutation, source/test edits, runtime restart, Docker, database/migration, broker/API, PR mutation, commit, push, merge, release, deploy, route/model changes, or live-trading behavior.',
    ];
    return lines.join('\n');
  }

  String get goalHealthStopInstruction {
    if (!hasGoalHealthPressure) return '';
    final action = goalHealthStopAction.toLowerCase();
    if (action.contains('containment_closeout')) {
      return 'stop this manually active goal now; publish one zero-hold containment closeout with worktree/session/lock evidence, release any helper lock, then go quiet with no heartbeat/read-only/status reports until RecoveryWave1 explicitly includes this lane.';
    }
    if (action.contains('hard_stop')) {
      return 'hard-stop this manually active goal now; do not publish another heartbeat, read-only, status, or audit report; publish only a missing containment closeout, release helper locks, then stop.';
    }
    if (action.contains('manual_goal_fastlane') ||
        action.contains('fastlane_required')) {
      return 'this operator-set manual goal is live, so run fastlane only: close the current PR/blocker/disposition item or publish one exact next-owner blocker; do not do backlog, status, audit, source, test, runtime, branch, commit, push, database, broker, deployment, or new-research work under red load; release helper locks, then stop or wait for the next explicit operator goal.';
    }
    if (goalHealthCritical) {
      return 'reduce or close this active goal lane before assigning more source-capable work; publish one exact blocker only if it cannot move safely.';
    }
    return '';
  }

  String get goalHealthQuietProofInstruction {
    if (!hasGoalHealthPressure) return '';
    final action = goalHealthStopAction.toLowerCase();
    if (action.contains('containment_closeout') ||
        goalHealthPausedActive ||
        action.contains('hard_stop')) {
      return 'after the closeout, require a quiet window with no thread goal update, tool call, source write, runtime action, PR mutation, or status churn before trusting the lane again.';
    }
    if (action.contains('manual_goal_fastlane') ||
        action.contains('fastlane_required')) {
      return 'manual goals remain useful only while tied to one PR/blocker/disposition movement; generic progress reports do not reduce recovery pressure.';
    }
    return '';
  }

  String get queuedChatLabel => queuedRunCount > 0
      ? '$queuedRunCount queued chat${queuedRunCount == 1 ? '' : 's'}'
      : '';
  String get queuedChipLabel =>
      queuedRunCount > 0 ? '$queuedRunCount queued' : '';
  String get waitingChipLabel =>
      waitingRunCount > 0 ? '$waitingRunCount waiting' : '';
  String get openChatLabel =>
      openRunCount > classifiedOpenRunCount && openRunCount > 0
          ? '$openRunCount open chat${openRunCount == 1 ? '' : 's'}'
          : '';
  String get openChipLabel =>
      openRunCount > classifiedOpenRunCount && openRunCount > 0
          ? '$openRunCount open'
          : '';
  String get questionLabel => pendingQuestionCount > 0
      ? '$pendingQuestionCount question${pendingQuestionCount == 1 ? '' : 's'}'
      : '';
  String get questionChipLabel => questionLabel;
  String get needsInputChipLabel =>
      operatingNeedsInput && pendingQuestionCount <= 0 ? 'needs input' : '';
  bool get hasStaleActiveRun => activeRunStale && activeRunId.isNotEmpty;
  String get activeStaleChipLabel {
    if (!hasStaleActiveRun) return '';
    if (activeRunLastSeenAgeMinutes > 0) {
      return 'stale ${activeRunLastSeenAgeMinutes}m';
    }
    return activeRunStaleKind == 'queued' ? 'stale queued' : 'stale run';
  }

  String get activeStaleActionChipLabel {
    if (!hasStaleActiveRun) return '';
    return activeRunStaleActionLabel.isNotEmpty
        ? activeRunStaleActionLabel.toLowerCase()
        : 'inspect stale run';
  }

  bool get hasStaleRunHandoff =>
      hasStaleActiveRun && activeRunStaleHandoffCopy.isNotEmpty;

  String get staleActiveOpenActionLabel => activeRunStaleActionLabel.isNotEmpty
      ? hasStaleRunHandoff
          ? 'Open/copy stale run'
          : activeRunStaleActionLabel
      : hasStaleRunHandoff
          ? 'Open/copy stale run'
          : 'Inspect stale run';

  String get activeProgressLabel {
    if (pendingQuestionCount > 0) {
      return 'Current: waiting on operator question';
    }
    if (hasStaleActiveRun && activeRunLatestStepTitle.isNotEmpty) {
      final age = activeRunLastSeenAgeMinutes > 0
          ? ' (${activeRunLastSeenAgeMinutes}m)'
          : '';
      return AutopilotAgentBenchActivityPresenter._clip(
        'Current: stale on $activeRunLatestStepTitle$age',
        110,
      );
    }
    if (hasStaleActiveRun) {
      return activeRunStaleKind == 'queued'
          ? 'Current: stale queued run'
          : 'Current: stale active run';
    }
    if (activeRunStatus == 'queued') {
      return 'Current: queued for worker';
    }
    if (activeRunGoalCurrentStep.isNotEmpty &&
        activeRunLatestStepTitle.isEmpty) {
      return AutopilotAgentBenchActivityPresenter._clip(
        'Goal: $activeRunGoalCurrentStep',
        110,
      );
    }
    if (activeRunLatestStepTitle.isNotEmpty) {
      final status = activeRunLatestStepStatus.isEmpty
          ? ''
          : ' (${AutopilotAgentBenchActivityPresenter._statusLabel(activeRunLatestStepStatus)})';
      return AutopilotAgentBenchActivityPresenter._clip(
        'Current: $activeRunLatestStepTitle$status',
        110,
      );
    }
    if (activeRunPlanStatus == 'awaiting_approval' ||
        activeRunStatus == 'awaiting_approval') {
      return 'Current: waiting for plan approval';
    }
    if (activeRunPlanStatus == 'awaiting_clarification' ||
        activeRunStatus == 'awaiting_clarification') {
      return 'Current: waiting for clarification';
    }
    if (activeRunStage.isNotEmpty) {
      return 'Current: working on ${AutopilotAgentBenchActivityPresenter._stageLabel(activeRunStage)}';
    }
    if (openRunCount > 0 || activeRunCount > 0) {
      return 'Current: run in progress';
    }
    return '';
  }

  String get activeProgressDetail {
    if (activeProgressLabel.isEmpty) return '';
    final parts = <String>[
      if (activeRunStatus == 'queued')
        'Waiting for worker start; queued work is visible and should not be double-started',
      if (activeRunStaleDetail.isNotEmpty) activeRunStaleDetail,
      if (activeRunStaleSafeNextStep.isNotEmpty) activeRunStaleSafeNextStep,
      if (activeRunGoalObjective.isNotEmpty)
        'goal ${activeRunGoalProgressPercent.clamp(0, 100)}%: $activeRunGoalObjective',
      if (activeRunGoalCurrentStep.isNotEmpty)
        'goal step: $activeRunGoalCurrentStep',
      if (activeRunGoalNextAction.isNotEmpty)
        'next goal action: $activeRunGoalNextAction',
      if (activeRunGoalCompletionGate.isNotEmpty)
        'completion gate: $activeRunGoalCompletionGate',
      if (activeRunTitle.isNotEmpty) activeRunTitle,
      if (activeRunId.isNotEmpty) activeRunId,
      if (activeRunUpdatedAt.isNotEmpty) 'updated $activeRunUpdatedAt',
    ];
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(' | '), 220);
  }

  bool get hasBlockedRecovery => blockedRunId.isNotEmpty;
  String get blockedRecoveryChipLabel =>
      hasBlockedRecovery ? 'blocked recovery' : '';
  String get blockedRecoveryDecisionChipLabel {
    if (!hasBlockedRecovery) return '';
    if (blockedRunRecoveryDecisionLabel.isNotEmpty) {
      return blockedRunRecoveryDecisionLabel.toLowerCase();
    }
    return blockedRunRecoveryCanRerun ? 'rerun safely' : 'review only';
  }

  String get blockedRecoveryLabel {
    if (!hasBlockedRecovery) return '';
    final decision = blockedRunRecoveryDecisionLabel.isNotEmpty
        ? blockedRunRecoveryDecisionLabel
        : blockedRunRecoveryCanRerun
            ? 'Rerun safely'
            : 'Review only';
    if (blockedRunRecoveryLastFailedStep.isNotEmpty) {
      final exit = blockedRunRecoveryLastFailedExitCode.isEmpty
          ? ''
          : ' exit $blockedRunRecoveryLastFailedExitCode';
      return AutopilotAgentBenchActivityPresenter._clip(
        'Recovery: $decision after $blockedRunRecoveryLastFailedStep$exit',
        110,
      );
    }
    if (blockedRunReason.isNotEmpty) {
      return AutopilotAgentBenchActivityPresenter._clip(
        'Recovery: $decision - $blockedRunReason',
        110,
      );
    }
    return 'Recovery: $decision';
  }

  String get blockedRecoveryDetail {
    if (!hasBlockedRecovery) return '';
    final parts = <String>[
      if (blockedRunRecoverySafeNextStep.isNotEmpty)
        blockedRunRecoverySafeNextStep,
      if (blockedRunRecoverySafeNextStep.isEmpty && blockedRunReason.isNotEmpty)
        blockedRunReason,
      if (blockedRunRecoveryLastFailedSummary.isNotEmpty)
        blockedRunRecoveryLastFailedSummary,
      if (blockedRunTitle.isNotEmpty) blockedRunTitle,
      blockedRunId,
      if (blockedRunUpdatedAt.isNotEmpty) 'updated $blockedRunUpdatedAt',
    ];
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(' | '), 220);
  }

  String get blockedRecoveryOpenActionLabel =>
      blockedRunRecoveryCanRerun ? 'Rerun safely' : 'Review blocker';

  String get blockedRecoveryPrecheckLabel {
    if (!hasBlockedRecovery) return '';
    final decision = blockedRunRecoveryDecisionLabel.isNotEmpty
        ? blockedRunRecoveryDecisionLabel
        : blockedRunRecoveryCanRerun
            ? 'Rerun safely'
            : 'Review only';
    final guard =
        blockedRunRecoveryCanRerun ? 'approval-first' : 'rerun blocked';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Precheck: $decision, $guard',
      72,
    );
  }

  String get blockedRecoveryPrecheckDetail {
    if (!hasBlockedRecovery) return '';
    final parts = <String>[
      if (blockedRunRecoveryCanRerun)
        'fresh validation evidence before merge'
      else
        'operator review required before recovery',
      if (blockedRunRecoverySafeNextStep.isNotEmpty)
        blockedRunRecoverySafeNextStep,
      if (blockedRunRecoveryLastFailedStep.isNotEmpty)
        blockedRunRecoveryLastFailedExitCode.isEmpty
            ? 'failed check: $blockedRunRecoveryLastFailedStep'
            : 'failed check: $blockedRunRecoveryLastFailedStep exit $blockedRunRecoveryLastFailedExitCode',
      if (blockedRunRecoveryPermissionBoundaryLabel.isNotEmpty)
        blockedRunRecoveryPermissionBoundaryLabel,
    ];
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(' | '), 220);
  }

  String get blockedRecoveryPrecheckChipLabel {
    if (!hasBlockedRecovery) return '';
    return blockedRunRecoveryCanRerun ? 'approval-first' : 'rerun blocked';
  }

  bool get hasScheduledQualityPressure =>
      scheduledQualityLowQualityCount > 0 || scheduledQualityRepairedCount > 0;
  bool get hasScheduledQualityTarget =>
      hasScheduledQualityPressure && scheduledQualityLatestRunId.isNotEmpty;
  String get _benchmarkFullStatusKey =>
      AutopilotAgentBenchActivityPresenter._statusKey(benchmarkPromotionStatus);
  String get _benchmarkSelectedStatusKey =>
      AutopilotAgentBenchActivityPresenter._statusKey(
        benchmarkSelectedScenariosStatus,
      );
  String get _benchmarkScopeKey =>
      AutopilotAgentBenchActivityPresenter._statusKey(benchmarkPromotionScope);
  bool get hasBenchmarkPromotionScopeSignal =>
      benchmarkPromotionStatus.trim().isNotEmpty ||
      benchmarkSelectedScenariosStatus.trim().isNotEmpty ||
      benchmarkPromotionScope.trim().isNotEmpty ||
      benchmarkEvidenceGapLabels.isNotEmpty;
  bool get hasBenchmarkEvidenceGaps => benchmarkEvidenceGapLabels.isNotEmpty;
  bool get hasBenchmarkEvidenceHandoff =>
      benchmarkEvidenceHandoffCopy.trim().isNotEmpty;
  bool get hasBenchmarkEvidenceRecovery =>
      benchmarkEvidenceRecoverySources.isNotEmpty ||
      benchmarkEvidenceRecoveryDetail.trim().isNotEmpty;
  bool get hasBenchmarkEvidenceIntakeStatus =>
      benchmarkEvidenceIntakeStatus.trim().isNotEmpty ||
      benchmarkEvidenceIntakeRequiredCount > 0;
  bool get benchmarkEvidenceIntakeReady =>
      hasBenchmarkEvidenceIntakeStatus &&
      benchmarkEvidenceIntakeRequiredCount > 0 &&
      benchmarkEvidenceIntakeReadyCount >= benchmarkEvidenceIntakeRequiredCount;
  bool get benchmarkSelectedSmokePassedOnly =>
      hasBenchmarkPromotionScopeSignal &&
      (_benchmarkScopeKey == 'selected_smoke_only' ||
          _benchmarkScopeKey == 'smoke_passed_only' ||
          _benchmarkScopeKey == 'partial_pass_only' ||
          (_benchmarkScopeKey != 'unstable_full_evidence' &&
              _benchmarkSelectedStatusKey == 'passed' &&
              _benchmarkFullStatusKey.isNotEmpty &&
              _benchmarkFullStatusKey != 'passed'));
  bool get benchmarkUnstableFullEvidence =>
      hasBenchmarkPromotionScopeSignal &&
      _benchmarkScopeKey == 'unstable_full_evidence';
  bool get benchmarkFullPromotionBlocked =>
      hasBenchmarkPromotionScopeSignal &&
      _benchmarkFullStatusKey.isNotEmpty &&
      _benchmarkFullStatusKey != 'passed';
  bool get benchmarkFullPromotionPassed =>
      hasBenchmarkPromotionScopeSignal && _benchmarkFullStatusKey == 'passed';
  bool get hasBenchmarkPromotionScopeWarning =>
      benchmarkSelectedSmokePassedOnly ||
      benchmarkUnstableFullEvidence ||
      benchmarkFullPromotionBlocked ||
      hasBenchmarkEvidenceGaps;

  String get benchmarkPromotionScopeChipLabel {
    if (benchmarkSelectedSmokePassedOnly) return 'smoke passed only';
    if (benchmarkUnstableFullEvidence) return 'full bench unstable';
    if (benchmarkFullPromotionBlocked) return 'full bench blocked';
    if (benchmarkFullPromotionPassed) return 'full bench passed';
    return '';
  }

  String get benchmarkPromotionScopeLabel {
    if (benchmarkSelectedSmokePassedOnly) {
      return 'Benchmark scope: selected smoke passed, full promotion blocked';
    }
    if (benchmarkUnstableFullEvidence) {
      return 'Benchmark scope: full benchmark evidence unstable';
    }
    if (benchmarkFullPromotionBlocked) {
      return 'Benchmark scope: full promotion blocked';
    }
    if (benchmarkFullPromotionPassed) {
      return 'Benchmark scope: full promotion passed';
    }
    return '';
  }

  String get benchmarkPromotionScopeDetail {
    if (!hasBenchmarkPromotionScopeSignal) return '';
    final statusLabel =
        benchmarkPromotionStatus.isEmpty ? 'missing' : benchmarkPromotionStatus;
    final selectedLabel = benchmarkSelectedScenariosStatus.isEmpty
        ? ''
        : 'selected $benchmarkSelectedScenariosStatus';
    final facts = <String>[
      if (benchmarkProfile.isNotEmpty) 'profile $benchmarkProfile',
      if (benchmarkPassRate.isNotEmpty) 'pass rate $benchmarkPassRate',
      if (benchmarkSourceStability.isNotEmpty)
        'source ${benchmarkSourceStability.replaceAll('_', ' ')}',
      if (selectedLabel.isNotEmpty) selectedLabel,
      'full $statusLabel',
    ];
    final warning = benchmarkSelectedSmokePassedOnly
        ? 'Selected scenario slice passed, but full coding benchmark promotion remains blocked.'
        : benchmarkUnstableFullEvidence
            ? 'Full coding benchmark scenarios passed, but source stability evidence is not clean.'
            : benchmarkFullPromotionBlocked
                ? 'Full coding benchmark promotion remains blocked.'
                : 'Full coding benchmark promotion passed.';
    final gate = benchmarkFullPromotionPassed
        ? 'Keep dependent scorecards and source freshness current.'
        : 'Do not treat this as promotion-ready until the full scorecard, source freshness, and dependent gates pass.';
    final evidenceSummary = benchmarkEvidenceGapLabels.isNotEmpty
        ? ' Proof gaps: ${benchmarkEvidenceGapLabels.take(3).join(', ')}.'
        : '';
    return AutopilotAgentBenchActivityPresenter._clip(
      '$warning$evidenceSummary $gate ${facts.join(' | ')}'
      '${benchmarkEvidenceNextAction.isNotEmpty ? ' | next proof $benchmarkEvidenceNextAction' : ''}',
      240,
    );
  }

  String get benchmarkEvidenceOpenActionLabel {
    if (!hasBenchmarkEvidenceHandoff) return '';
    if (hasBenchmarkEvidenceRecovery) return 'Copy frontier recovery';
    return benchmarkEvidenceHandoffLabel.isNotEmpty
        ? benchmarkEvidenceHandoffLabel
        : 'Copy frontier proof packet';
  }

  String get benchmarkEvidencePermissionBoundaryLabel =>
      hasBenchmarkEvidenceHandoff ? 'evidence collection only' : '';

  String get benchmarkEvidencePermissionBoundaryDetail =>
      hasBenchmarkEvidenceHandoff
          ? 'Frontier proof packets can collect or verify scorecards, manifests, transcripts, hashes, and check receipts; they do not authorize source/runtime/git/PR/live action.'
          : '';

  String get benchmarkEvidenceIntakeChipLabel {
    if (!hasBenchmarkEvidenceIntakeStatus) return '';
    if (benchmarkEvidenceIntakeReady) return 'intake ready';
    if (benchmarkEvidenceIntakeRequiredCount > 0) {
      return 'intake $benchmarkEvidenceIntakeReadyCount/$benchmarkEvidenceIntakeRequiredCount ready';
    }
    return 'intake ${benchmarkEvidenceIntakeStatus.replaceAll('_', ' ')}';
  }

  String get benchmarkEvidenceIntakeDetail {
    if (!hasBenchmarkEvidenceIntakeStatus) return '';
    final status = benchmarkEvidenceIntakeStatus.trim().isEmpty
        ? 'unknown'
        : benchmarkEvidenceIntakeStatus.replaceAll('_', ' ');
    final counts = benchmarkEvidenceIntakeRequiredCount > 0
        ? '$benchmarkEvidenceIntakeReadyCount/$benchmarkEvidenceIntakeRequiredCount sources ready'
        : 'source readiness unknown';
    final missing = benchmarkEvidenceIntakeMissingSources.isEmpty
        ? ''
        : ' Missing: ${benchmarkEvidenceIntakeMissingSources.take(3).join(', ')}.';
    final root = benchmarkEvidenceIntakeSourceRoot.trim().isEmpty
        ? ''
        : ' Root: $benchmarkEvidenceIntakeSourceRoot.';
    final localModelStatus = benchmarkEvidenceIntakeLocalModelStatus
            .trim()
            .isEmpty
        ? ''
        : ' Local model: ${benchmarkEvidenceIntakeLocalModelStatus.replaceAll('_', ' ')}.';
    final timeoutSalvage = benchmarkEvidenceIntakeLocalModelTimeoutSalvagedCases
            .isEmpty
        ? ''
        : ' Timeout salvage: ${benchmarkEvidenceIntakeLocalModelTimeoutSalvagedCases.take(3).join(', ')}.';
    final recovery = benchmarkEvidenceRecoveryDetail.trim().isEmpty
        ? ''
        : ' Recovery: $benchmarkEvidenceRecoveryDetail.';
    final next = benchmarkEvidenceIntakeNextAction.trim().isEmpty ||
            benchmarkEvidenceIntakeNextAction == 'none'
        ? ''
        : ' Next: $benchmarkEvidenceIntakeNextAction';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Frontier intake $status: $counts.$missing$recovery$localModelStatus$timeoutSalvage$next$root',
      240,
    );
  }

  String get scheduledQualityChipLabel {
    if (scheduledQualityLowQualityCount > 0) {
      return scheduledQualityLowQualityCount == 1
          ? 'quality rejected'
          : '$scheduledQualityLowQualityCount quality rejected';
    }
    if (scheduledQualityRepairedCount > 0) {
      return scheduledQualityRepairedCount == 1
          ? 'quality repaired'
          : '$scheduledQualityRepairedCount quality repaired';
    }
    return '';
  }

  String get scheduledQualityIssueChipLabel {
    if (!hasScheduledQualityPressure) return '';
    if (hasPursuingGoalProofPressure) return 'Pursuing goal proof';
    if (scheduledQualityIssueLabel.isNotEmpty) {
      return AutopilotAgentBenchActivityPresenter._clip(
        scheduledQualityIssueLabel,
        36,
      );
    }
    if (hasPrPublicationReceiptPressure) return 'PR receipt missing';
    if (hasGoalReceiptPressure) return 'goal receipt missing';
    return scheduledQualityLowQualityCount > 0
        ? 'local report rejected'
        : 'local report repaired';
  }

  String get scheduledQualityLabel {
    if (!hasScheduledQualityPressure) return '';
    final count = scheduledQualityLowQualityCount > 0
        ? scheduledQualityLowQualityCount
        : scheduledQualityRepairedCount;
    final state = scheduledQualityLowQualityCount > 0 ? 'rejected' : 'repaired';
    if (hasPursuingGoalProofPressure) {
      final issue = pursuingGoalProofIssueLabel.isEmpty
          ? ''
          : ' - $pursuingGoalProofIssueLabel';
      return AutopilotAgentBenchActivityPresenter._clip(
        'Pursuing goal proof: $count $state$issue',
        110,
      );
    }
    final issueLabel = scheduledQualityIssueLabel.isNotEmpty
        ? scheduledQualityIssueLabel
        : hasPrPublicationReceiptPressure
            ? 'PR receipt missing'
            : hasGoalReceiptPressure
                ? 'goal receipt missing'
                : '';
    final issue = issueLabel.isEmpty ? '' : ' - $issueLabel';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Quality gate: $count $state$issue',
      110,
    );
  }

  String get scheduledQualityDetail {
    if (!hasScheduledQualityPressure) return '';
    final score = scheduledQualityLatestScoreLabel.isEmpty
        ? ''
        : scheduledQualityLatestInitialScoreLabel.isNotEmpty
            ? 'score $scheduledQualityLatestInitialScoreLabel->$scheduledQualityLatestScoreLabel'
            : 'score $scheduledQualityLatestScoreLabel';
    final parts = <String>[
      if (pursuingGoalProofDetail.isNotEmpty) pursuingGoalProofDetail,
      if (scheduledQualityNextAction.isNotEmpty) scheduledQualityNextAction,
      if (score.isNotEmpty) score,
      if (scheduledQualityAverageScoreLabel.isNotEmpty)
        'avg $scheduledQualityAverageScoreLabel',
      if (scheduledQualityLatestRunId.isNotEmpty) scheduledQualityLatestRunId,
      if (scheduledQualityLatestStatus.isNotEmpty)
        scheduledQualityLatestStatus.replaceAll('_', ' '),
    ];
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(' | '), 220);
  }

  String get scheduledQualityOpenActionLabel {
    if (!hasScheduledQualityTarget) return '';
    if (hasScheduledQualityHandoff) {
      return scheduledQualityHandoffLabel.isNotEmpty
          ? scheduledQualityHandoffLabel
          : hasPursuingGoalProofPressure
              ? 'Copy goal proof gate'
              : hasPrPublicationReceiptPressure
                  ? 'Copy PR receipt gate'
                  : 'Copy goal receipt gate';
    }
    if (hasPursuingGoalProofPressure) return 'Review goal proof';
    if (hasPrPublicationReceiptPressure) return 'Review PR receipt';
    return hasGoalReceiptPressure
        ? 'Review goal receipt'
        : 'Review quality run';
  }

  String get kpiBlockedPrChipLabel => kpiBlockedPrCount > 0
      ? '$kpiBlockedPrCount blocked PR${kpiBlockedPrCount == 1 ? '' : 's'}'
      : '';
  String get kpiReadyCandidateChipLabel =>
      kpiReadyCandidateCount > 0 ? '$kpiReadyCandidateCount ready' : '';
  String get kpiTempArtifactChipLabel =>
      kpiTempArtifactCount > 0 ? '$kpiTempArtifactCount temp' : '';
  String get kpiScoreChipLabel =>
      kpiScoreLabel.isNotEmpty ? 'KPI $kpiScoreLabel' : '';
  String get kpiPrPreviewLabel =>
      kpiPrNumbers.isNotEmpty ? 'PR #${kpiPrNumbers.take(3).join(', #')}' : '';
  String get kpiOwnerLaneName =>
      AutopilotAgentBenchActivityPresenter._clip(agentName.trim(), 42);
  bool get hasKpiPrOwnerLaneAction =>
      kpiBlockedPrCount > 0 &&
      (kpiOwnerLaneName.isNotEmpty ||
          kpiPrNumbers.isNotEmpty ||
          kpiSignal.isNotEmpty ||
          kpiNextAction.isNotEmpty ||
          hasKpiPrBlockerProofFloor);
  String get kpiPrOwnerLaneChipLabel {
    if (!hasKpiPrOwnerLaneAction) return '';
    final lane = kpiOwnerLaneName.isEmpty ? 'Owner' : kpiOwnerLaneName;
    return AutopilotAgentBenchActivityPresenter._clip('$lane PR lane', 34);
  }

  String get kpiPrOwnerLaneLabel {
    if (!hasKpiPrOwnerLaneAction) return '';
    final lane = kpiOwnerLaneName.isEmpty
        ? 'PR owner lane'
        : '$kpiOwnerLaneName PR lane';
    return AutopilotAgentBenchActivityPresenter._clip(
      '$lane: $kpiBlockedPrChipLabel',
      110,
    );
  }

  String get kpiPrOwnerLaneDetail {
    if (!hasKpiPrOwnerLaneAction) return '';
    final parts = <String>[
      if (kpiOwnerLaneName.isNotEmpty) 'Owner lane: $kpiOwnerLaneName',
      if (kpiPrPreviewLabel.isNotEmpty) kpiPrPreviewLabel,
      if (kpiSignal.isNotEmpty) kpiSignal,
      if (kpiNextAction.isNotEmpty) 'next: $kpiNextAction',
      if (kpiPrBlockerTopDetail.isNotEmpty)
        'top blocker: $kpiPrBlockerTopDetail',
      if (kpiPrBlockerProofFloorDetail.isNotEmpty)
        AutopilotAgentBenchActivityPresenter._clip(
          kpiPrBlockerProofFloorDetail,
          150,
        ),
    ];
    return AutopilotAgentBenchActivityPresenter._clip(
      'PR owner lane: ${parts.join(' | ')}',
      520,
    );
  }

  String get kpiPrOwnerLaneActionLabel =>
      hasKpiPrOwnerLaneAction ? 'Copy PR lane handoff' : '';

  String get kpiPrOwnerLaneCopyText {
    if (!hasKpiPrOwnerLaneAction) return '';
    final lines = <String>[
      'Project Autopilot PR owner-lane handoff',
      if (kpiOwnerLaneName.isNotEmpty) 'Owner lane: $kpiOwnerLaneName',
      if (kpiPrPreviewLabel.isNotEmpty) 'PRs: $kpiPrPreviewLabel',
      if (kpiSignal.isNotEmpty) 'Signal: $kpiSignal',
      if (kpiNextAction.isNotEmpty) 'Next action: $kpiNextAction',
      if (kpiPrBlockerTopDetail.isNotEmpty)
        'Top blocker: $kpiPrBlockerTopDetail',
      if (prBlockerProofFloorCopyText.isNotEmpty)
        prBlockerProofFloorCopyText
      else
        'Required proof: current head SHA, branch/worktree, focused check evidence, and PM/operator disposition before PR state changes.',
      'Safety boundary: this copied handoff is decision-only. It does not authorize source edits, git or PR mutation, commit, push, merge, release, deploy, runtime, database, broker/API, route/model changes, or live-trading behavior.',
    ];
    return lines.join('\n');
  }

  bool get hasKpiPrBlockerProofFloor =>
      kpiBlockedPrCount > 0 &&
      (kpiPrBlockerProofFloor.isNotEmpty ||
          kpiPrBlockerDirtyCount > 0 ||
          kpiPrBlockerNoChecksCount > 0 ||
          kpiPrBlockerFailingCount > 0 ||
          kpiPrBlockerNonDraftCount > 0);
  String get kpiPrBlockerProofChipLabel {
    if (!hasKpiPrBlockerProofFloor) return '';
    final parts = <String>[];
    if (kpiPrBlockerNoChecksCount > 0) {
      parts.add('$kpiPrBlockerNoChecksCount no-check');
    }
    if (kpiPrBlockerDirtyCount > 0) {
      parts.add('$kpiPrBlockerDirtyCount dirty');
    }
    if (kpiPrBlockerFailingCount > 0) {
      parts.add('$kpiPrBlockerFailingCount failing');
    }
    if (parts.isEmpty) return 'PR proof floor';
    return AutopilotAgentBenchActivityPresenter._clip(
      'proof ${parts.join(', ')}',
      42,
    );
  }

  bool get hasKpiPrBlockerDecisionGate =>
      hasKpiPrBlockerProofFloor &&
      (kpiPrBlockerGateState.isNotEmpty ||
          kpiPrBlockerGateLabel.isNotEmpty ||
          kpiPrBlockerAllowedDecisions.isNotEmpty ||
          kpiPrBlockerRequiredProof.isNotEmpty);

  String get kpiPrBlockerGateChipLabel {
    if (!hasKpiPrBlockerDecisionGate) return '';
    final label = kpiPrBlockerGateLabel.isNotEmpty
        ? kpiPrBlockerGateLabel
        : 'Blocked until PR proof';
    return AutopilotAgentBenchActivityPresenter._clip(label, 34);
  }

  String get kpiPrBlockerDecisionDetail {
    if (!hasKpiPrBlockerDecisionGate) return '';
    final decisions = kpiPrBlockerAllowedDecisions.take(3).toList();
    final parts = <String>[
      kpiPrBlockerGateLabel.isNotEmpty
          ? kpiPrBlockerGateLabel
          : 'Blocked until PR proof',
      if (kpiPrBlockerGateState.isNotEmpty)
        kpiPrBlockerGateState.replaceAll('_', ' '),
      if (kpiPrBlockerRequiredProof.isNotEmpty)
        'proof: $kpiPrBlockerRequiredProof',
      if (decisions.isNotEmpty) 'allowed: ${decisions.join(' | ')}',
    ];
    return AutopilotAgentBenchActivityPresenter._clip(
      'PR blocker decision gate: ${parts.join(' | ')}',
      520,
    );
  }

  String get kpiPrBlockerDecisionMenuCopyText {
    if (!hasKpiPrBlockerDecisionGate) return '';
    final lines = <String>[
      'Decision gate: ${kpiPrBlockerGateLabel.isNotEmpty ? kpiPrBlockerGateLabel : 'Blocked until PR proof'}',
      if (kpiPrBlockerGateState.isNotEmpty)
        'Gate state: $kpiPrBlockerGateState',
      if (kpiPrBlockerRequiredProof.isNotEmpty)
        'Required proof: $kpiPrBlockerRequiredProof',
      if (kpiPrBlockerAllowedDecisions.isNotEmpty) 'Allowed decisions:',
      for (final decision in kpiPrBlockerAllowedDecisions) '- $decision',
    ];
    return lines.join('\n');
  }

  String get kpiPrBlockerTopDetail {
    if (!hasKpiPrBlockerProofFloor) return '';
    final pr = kpiPrBlockerTopPr.isNotEmpty
        ? 'PR #$kpiPrBlockerTopPr'
        : _prPreflightPrLabel;
    final parts = <String>[
      if (pr.isNotEmpty) pr,
      if (kpiPrBlockerTopCi.isNotEmpty) 'CI $kpiPrBlockerTopCi',
      if (kpiPrBlockerTopMerge.isNotEmpty) 'merge $kpiPrBlockerTopMerge',
      if (kpiPrBlockerTopBranch.isNotEmpty) 'branch $kpiPrBlockerTopBranch',
      if (kpiPrBlockerTopPosture.isNotEmpty) kpiPrBlockerTopPosture,
    ];
    return AutopilotAgentBenchActivityPresenter._clip(
      parts.join('; '),
      220,
    );
  }

  String get kpiPrBlockerProofFloorDetail {
    if (!hasKpiPrBlockerProofFloor) return '';
    final fallbackBits = <String>[
      '$kpiBlockedPrCount blocked PR${kpiBlockedPrCount == 1 ? '' : 's'}',
      if (kpiPrBlockerNoChecksCount > 0)
        '$kpiPrBlockerNoChecksCount missing checks',
      if (kpiPrBlockerDirtyCount > 0) '$kpiPrBlockerDirtyCount dirty',
      if (kpiPrBlockerFailingCount > 0) '$kpiPrBlockerFailingCount failing CI',
      if (kpiPrBlockerNonDraftCount > 0) '$kpiPrBlockerNonDraftCount non-draft',
      if (kpiPrBlockerTopDetail.isNotEmpty) kpiPrBlockerTopDetail,
      'required current head SHA, branch, clean owner worktree or owner path, focused check evidence, and PM/operator disposition',
    ];
    final floor = kpiPrBlockerProofFloor.isNotEmpty
        ? kpiPrBlockerProofFloor
        : 'PR blocker proof floor: ${fallbackBits.join('; ')}.';
    final blocked = kpiPrBlockerBlockedAction.isEmpty
        ? ''
        : ' Blocked action: $kpiPrBlockerBlockedAction';
    return AutopilotAgentBenchActivityPresenter._clip(
      '$floor$blocked',
      620,
    );
  }

  String get prBlockerProofFloorCopyText {
    if (!hasKpiPrBlockerProofFloor) return '';
    final lines = <String>[
      'Proof floor: ${kpiPrBlockerProofFloor.isNotEmpty ? kpiPrBlockerProofFloor : kpiPrBlockerProofFloorDetail}',
      if (kpiPrBlockerDecisionMenuCopyText.isNotEmpty)
        kpiPrBlockerDecisionMenuCopyText,
      if (kpiPrBlockerTopDetail.isNotEmpty)
        'Top blocker: $kpiPrBlockerTopDetail',
      if (kpiPrBlockerBlockedAction.isNotEmpty)
        'Blocked action: $kpiPrBlockerBlockedAction',
    ];
    return lines.join('\n');
  }

  bool get hasKpiPrBlockerDecisionPacket =>
      hasKpiPrBlockerProofFloor && prBlockerProofFloorCopyText.isNotEmpty;

  String get kpiPrBlockerDecisionPacketLabel =>
      hasKpiPrBlockerDecisionPacket ? 'Copy PR proof packet' : '';

  bool get hasKpiGeneratedStateDrift =>
      kpiGeneratedStateRefreshRequired ||
      kpiGeneratedStateDriftCount > 0 ||
      kpiGeneratedStateDriftSummary.isNotEmpty;
  String get kpiGeneratedStateChipLabel {
    if (!hasKpiGeneratedStateDrift) return '';
    if (kpiGeneratedStateDriftLabel.isNotEmpty) {
      return AutopilotAgentBenchActivityPresenter._clip(
        kpiGeneratedStateDriftLabel,
        36,
      );
    }
    if (kpiGeneratedStateDriftCount > 0) {
      return kpiGeneratedStateDriftCount == 1
          ? '1 stale board action'
          : '$kpiGeneratedStateDriftCount stale board actions';
    }
    return 'stale scorecard';
  }

  String get kpiGeneratedStateLabel {
    if (!hasKpiGeneratedStateDrift) return '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Generated state: $kpiGeneratedStateChipLabel',
      110,
    );
  }

  String get kpiGeneratedStateDetail {
    if (!hasKpiGeneratedStateDrift) return '';
    final parts = <String>[
      if (kpiGeneratedStateBlocksBoardActions)
        'Guard: refresh/reconcile generated state before following board ranks',
      if (kpiGeneratedStateNextAction.isNotEmpty) kpiGeneratedStateNextAction,
      if (kpiGeneratedStateDriftSummary.isNotEmpty)
        kpiGeneratedStateDriftSummary,
      if (kpiGeneratedStateScorecardTopAction.isNotEmpty)
        'scorecard shows ${AutopilotAgentBenchActivityPresenter._clip(kpiGeneratedStateScorecardTopAction, 84)}',
      if (kpiGeneratedStateBoardTopAction.isNotEmpty)
        'board now ${AutopilotAgentBenchActivityPresenter._clip(kpiGeneratedStateBoardTopAction, 84)}',
      if (kpiGeneratedStateBoardGenerated.isNotEmpty)
        'board $kpiGeneratedStateBoardGenerated',
      if (kpiGeneratedStateScorecardGenerated.isNotEmpty)
        'scorecard $kpiGeneratedStateScorecardGenerated',
      if (kpiGeneratedStatePath.isNotEmpty) kpiGeneratedStatePath,
    ];
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(' | '), 520);
  }

  bool get kpiGeneratedStateBlocksBoardActions {
    if (!hasKpiGeneratedStateDrift) return false;
    final sample = [
      kpiGeneratedStateRefreshTarget,
      kpiGeneratedStateDriftKind,
      kpiGeneratedStateDriftLabel,
      kpiGeneratedStateDriftSummary,
      kpiGeneratedStateNextAction,
    ].join(' ').toLowerCase();
    return kpiGeneratedStateRefreshRequired ||
        kpiGeneratedStateDriftCount > 0 ||
        sample.contains('stale board') ||
        sample.contains('stale expedite') ||
        sample.contains('refresh agentops') ||
        sample.contains('generated-state drift');
  }

  bool get shouldPrioritizeKpiGeneratedStateAction =>
      kpiGeneratedStateBlocksBoardActions &&
      hasKpiGeneratedStateTarget &&
      (hasKpiPressure ||
          hasExpeditePressure ||
          hasDeliveryFocus ||
          hasPrPreflightPressure ||
          hasOwnerReadyFirstTarget ||
          stableInboxTopRank > 0 ||
          flowQuarantinedTargetCount > 0 ||
          flowPausedAutomationCount > 0);

  String get kpiGeneratedStateGuardLabel {
    if (!shouldPrioritizeKpiGeneratedStateAction) return '';
    final target = kpiGeneratedStateRefreshTarget.toLowerCase();
    return target == 'expedite_board'
        ? 'Refresh expedite board first'
        : 'Refresh scorecard first';
  }

  bool get hasKpiGeneratedStateTarget =>
      hasKpiGeneratedStateDrift &&
      (kpiGeneratedStateOpenPath.isNotEmpty ||
          kpiGeneratedStatePath.isNotEmpty);
  String get kpiGeneratedStatePrimaryOpenPath =>
      kpiGeneratedStateOpenPath.isNotEmpty
          ? kpiGeneratedStateOpenPath
          : kpiGeneratedStatePath;
  String get kpiGeneratedStateOpenActionLabel => hasKpiGeneratedStateTarget
      ? shouldPrioritizeKpiGeneratedStateAction
          ? 'Open stale-state guard'
          : 'Open scorecard'
      : '';
  String get expeditePrBlockerChipLabel => expediteOpenPrBlockerCount > 0
      ? '$expediteOpenPrBlockerCount board PR${expediteOpenPrBlockerCount == 1 ? '' : 's'}'
      : '';
  String get expediteReadyCandidateChipLabel => expediteReadyCandidateCount > 0
      ? '$expediteReadyCandidateCount board ready'
      : '';
  String get expediteStableInboxChipLabel => expediteStableInboxRequestCount > 0
      ? '$expediteStableInboxRequestCount board inbox'
      : '';
  String get stableInboxPriorityChipLabel =>
      stableInboxRequestPriority.isNotEmpty
          ? '${stableInboxRequestPriority.toLowerCase()} inbox'
          : '';
  String get stableInboxFreshnessChipLabel =>
      stableInboxStaleForLatest ? 'stale board inbox' : '';
  bool get stableInboxHasLiveControlGate =>
      stableInboxLiveBrokerActionCount > 0 ||
      stableInboxLiveBrokerLabel.isNotEmpty ||
      stableInboxLiveBrokerDetail.isNotEmpty;
  String get stableInboxLiveControlGateChipLabel =>
      stableInboxLiveBrokerActionCount > 0
          ? '$stableInboxLiveBrokerActionCount live-control gate${stableInboxLiveBrokerActionCount == 1 ? '' : 's'}'
          : stableInboxLiveBrokerLabel;
  String get stableInboxLiveControlGateDetail {
    if (!stableInboxHasLiveControlGate) return '';
    if (stableInboxLiveBrokerDetail.isNotEmpty) {
      return stableInboxLiveBrokerDetail;
    }
    final floor = stableInboxLiveBrokerProofFloorUtc.isEmpty
        ? 'the latest named proof floor'
        : stableInboxLiveBrokerProofFloorUtc;
    return 'Live-control gate: PM/operator boundary required before broker truth, runtime trust, release, readiness, or live behavior can be restored; require proof later than $floor.';
  }

  bool get stableInboxHasControlPlaneGate =>
      stableInboxControlPlaneActionCount > 0 ||
      stableInboxControlPlaneLabel.isNotEmpty ||
      stableInboxControlPlaneDetail.isNotEmpty;
  String get stableInboxControlPlaneChipLabel =>
      stableInboxHasControlPlaneGate ? 'control-plane proof' : '';
  String get stableInboxControlPlaneGateDetail {
    if (!stableInboxHasControlPlaneGate) return '';
    if (stableInboxControlPlaneDetail.isNotEmpty) {
      return stableInboxControlPlaneDetail;
    }
    final floor = stableInboxControlPlaneProofFloorUtc.isEmpty
        ? 'the current containment proof floor'
        : stableInboxControlPlaneProofFloorUtc;
    final target = stableInboxControlPlaneThreadIds.isEmpty
        ? 'target thread(s)'
        : 'target ${AutopilotAgentBenchActivityPresenter._shortId(stableInboxControlPlaneThreadIds.first)}${stableInboxControlPlaneThreadIds.length > 1 ? ' +${stableInboxControlPlaneThreadIds.length - 1}' : ''}';
    return 'Control-plane proof: PM/operator/control-plane must stop, contain, or block $target and record quiet-window proof later than $floor before source/runtime trust returns.';
  }

  String get _stableInboxClassificationSample => [
        expediteTopType,
        expediteTopEvidence,
        expediteNextAction,
        stableInboxRequestPath,
        stableInboxRequestPriority,
        stableInboxRequestFrom,
        stableInboxRequestTo,
        stableInboxRequestBacklogId,
        stableInboxRequestPreview,
        stableInboxLatestRequestPath,
        stableInboxLatestRequestPriority,
        stableInboxLatestRequestFrom,
        stableInboxLatestRequestTo,
        stableInboxLatestRequestBacklogId,
        stableInboxLatestRequestPreview,
        stableInboxLiveBrokerLabel,
        stableInboxLiveBrokerDetail,
        stableInboxLiveBrokerProofFloorUtc,
        stableInboxControlPlaneLabel,
        stableInboxControlPlaneDetail,
        stableInboxControlPlaneProofFloorUtc,
        ...stableInboxControlPlaneThreadIds,
      ].join(' ').toLowerCase();
  bool get stableInboxClassificationOnly {
    if (stableInboxProcessed) return false;
    if (stableInboxHasLiveControlGate) return false;
    if (stableInboxHasControlPlaneGate) return false;
    final hasStableInboxRow = expediteTopType == 'stable_inbox_request' ||
        stableInboxTopRank > 0 ||
        stableInboxRequestPath.isNotEmpty ||
        stableInboxLatestRequestPath.isNotEmpty;
    if (!hasStableInboxRow) return false;
    final sample = _stableInboxClassificationSample;
    final asksForClassification = sample.contains('classification') ||
        sample.contains('classify') ||
        sample.contains('disposition') ||
        sample.contains('pm/operator');
    final safetySensitive = sample.contains('live-money') ||
        sample.contains('live money') ||
        sample.contains('live-trading') ||
        sample.contains('live trading') ||
        sample.contains('broker') ||
        sample.contains('broker/api') ||
        sample.contains('order') ||
        sample.contains('overfill') ||
        sample.contains('capital') ||
        sample.contains('breaker') ||
        sample.contains('coinbase') ||
        sample.contains('robinhood') ||
        sample.contains('position') ||
        sample.contains('trade') ||
        sample.contains('alert `') ||
        sample.contains('alert ');
    return asksForClassification && safetySensitive;
  }

  String get stableInboxClassificationChipLabel =>
      stableInboxClassificationOnly ? 'classification only' : '';

  String get stableInboxClassificationDetail {
    if (!stableInboxClassificationOnly) return '';
    final evidence = expediteTopEvidence.isEmpty
        ? ''
        : ' | ${AutopilotAgentBenchActivityPresenter._clip(expediteTopEvidence, 96)}';
    return 'Classification only: PM/operator may review evidence and record a disposition; this does not authorize runtime refresh, database/migration, broker/API, breaker/capital/model, monitor, route, deploy, or live-trading changes$evidence.';
  }

  bool get stableInboxRequiresDecisionBoundary =>
      stableInboxHasLiveControlGate ||
      stableInboxHasControlPlaneGate ||
      stableInboxClassificationOnly;

  bool get stableInboxProcessed =>
      stableInboxLatestRequestProcessed ||
      stableInboxRequestProcessed ||
      stableInboxProcessedDetail.isNotEmpty;
  String get stableInboxProcessedChipLabel =>
      stableInboxProcessed ? 'processed inbox' : '';
  bool get _useLatestProcessedReceipt => stableInboxLatestRequestProcessed;
  String get stableInboxProcessedUtc => _useLatestProcessedReceipt
      ? stableInboxLatestRequestProcessedUtc
      : stableInboxRequestProcessedUtc;
  String get stableInboxProcessedStatus => _useLatestProcessedReceipt
      ? stableInboxLatestRequestProcessedStatus
      : stableInboxRequestProcessedStatus;
  String get stableInboxProcessedResult => _useLatestProcessedReceipt
      ? stableInboxLatestRequestProcessedResult
      : stableInboxRequestProcessedResult;
  String get stableInboxProcessedReportPath => _useLatestProcessedReceipt
      ? stableInboxLatestRequestProcessedReportPath
      : stableInboxRequestProcessedReportPath;
  String get stableInboxProcessedSummary => _useLatestProcessedReceipt
      ? stableInboxLatestRequestProcessedSummary
      : stableInboxRequestProcessedSummary;
  String get stableInboxProcessedReviewDetail {
    if (!stableInboxProcessed) return '';
    final parts = <String>[
      if (stableInboxProcessedDetail.isNotEmpty) stableInboxProcessedDetail,
      if (stableInboxProcessedDetail.isEmpty &&
          stableInboxProcessedUtc.isNotEmpty)
        'processed $stableInboxProcessedUtc',
      if (stableInboxProcessedDetail.isEmpty &&
          stableInboxProcessedStatus.isNotEmpty)
        stableInboxProcessedStatus,
      if (stableInboxProcessedDetail.isEmpty &&
          stableInboxProcessedResult.isNotEmpty)
        stableInboxProcessedResult,
      if (stableInboxProcessedDetail.isEmpty &&
          stableInboxProcessedReportPath.isNotEmpty)
        'report $stableInboxProcessedReportPath',
      if (stableInboxProcessedDetail.isEmpty &&
          stableInboxProcessedSummary.isNotEmpty)
        stableInboxProcessedSummary,
    ];
    if (parts.isEmpty) return 'Processed receipt';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Processed receipt: ${parts.join(' | ')}',
      160,
    );
  }

  String get expediteControlPlaneChipLabel => expediteControlPlaneCount > 0
      ? '$expediteControlPlaneCount board containment'
      : '';
  String get expediteRankChipLabel =>
      expediteTopRank > 0 ? 'board #$expediteTopRank' : '';
  String get expeditePrPreviewLabel => expeditePrNumbers.isNotEmpty
      ? 'Board PR #${expeditePrNumbers.take(3).join(', #')}'
      : '';
  String get expediteTopActionLabel {
    final evidence = AutopilotAgentBenchActivityPresenter._clip(
      expediteTopEvidence.isNotEmpty ? expediteTopEvidence : expediteSignal,
      86,
    );
    if (expediteTopRank <= 0 || evidence.isEmpty) return '';
    return 'Board #$expediteTopRank: $evidence';
  }

  bool get hasOwnerReadyFirstPressure =>
      ownerReadyFirstCount > 0 || ownerReadyFirstTopRank > 0;
  String get ownerReadyFirstChipLabel {
    if (!hasOwnerReadyFirstPressure) return '';
    if (ownerReadyFirstCount <= 1) return 'owner-ready PR';
    return '$ownerReadyFirstCount owner-ready PRs';
  }

  String get ownerReadyFirstRankChipLabel =>
      ownerReadyFirstTopRank > 0 ? 'owner-ready #$ownerReadyFirstTopRank' : '';
  String get ownerReadyFirstRoleChipLabel {
    final role = ownerReadyFirstLaneRole.toLowerCase();
    if (role == 'owner') return 'PR owner';
    if (role == 'support') return 'support lane';
    return '';
  }

  String get ownerReadyFirstLabel {
    if (!hasOwnerReadyFirstPressure) return '';
    final pr = ownerReadyFirstPrNumber.isNotEmpty
        ? '#$ownerReadyFirstPrNumber'
        : ownerReadyFirstPr;
    final rank = ownerReadyFirstTopRank > 0
        ? 'Owner-ready #$ownerReadyFirstTopRank'
        : 'Owner-ready PR';
    final owner = ownerReadyFirstOwner.trim();
    return AutopilotAgentBenchActivityPresenter._clip(
      [rank, if (pr.isNotEmpty) pr, if (owner.isNotEmpty) owner].join(' '),
      96,
    );
  }

  String get ownerReadyFirstDetail {
    if (!hasOwnerReadyFirstPressure) return '';
    final role = ownerReadyFirstLaneRole.toLowerCase();
    final parts = <String>[
      if (role == 'owner')
        'owns this PR lane'
      else if (role == 'support')
        'required support lane',
      if (ownerReadyFirstNextAction.isNotEmpty) ownerReadyFirstNextAction,
      if (ownerReadyFirstState.isNotEmpty) ownerReadyFirstState,
      if (ownerReadyFirstRequiredLanes.isNotEmpty)
        'requires $ownerReadyFirstRequiredLanes',
      if (ownerReadyFirstExistingRequest.isNotEmpty &&
          ownerReadyFirstExistingRequest.toLowerCase() != 'none')
        'request $ownerReadyFirstExistingRequest',
      if (ownerReadyFirstGeneratedAt.isNotEmpty)
        'generated $ownerReadyFirstGeneratedAt',
    ];
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(' | '), 260);
  }

  bool get hasOwnerReadyFirstTarget =>
      hasOwnerReadyFirstPressure &&
      (ownerReadyFirstExistingRequestOpenPath.isNotEmpty ||
          ownerReadyFirstOpenPath.isNotEmpty ||
          ownerReadyFirstPath.isNotEmpty ||
          ownerReadyFirstHandoffCopy.isNotEmpty);
  String get ownerReadyFirstPrimaryOpenPath =>
      ownerReadyFirstExistingRequestOpenPath.isNotEmpty
          ? ownerReadyFirstExistingRequestOpenPath
          : ownerReadyFirstOpenPath.isNotEmpty
              ? ownerReadyFirstOpenPath
              : ownerReadyFirstPath;
  String get ownerReadyFirstOpenActionLabel => hasOwnerReadyFirstTarget
      ? ownerReadyFirstTopRank > 0
          ? 'Open/copy owner-ready #$ownerReadyFirstTopRank'
          : 'Open/copy owner-ready'
      : '';

  String get ownerReadyFirstCopyText {
    if (ownerReadyFirstHandoffCopy.isNotEmpty) {
      return ownerReadyFirstHandoffCopy;
    }
    if (!hasOwnerReadyFirstPressure) return '';
    final lines = <String>[
      'Project Autopilot owner-ready-first row',
      if (ownerReadyFirstTopRank > 0) 'Rank: $ownerReadyFirstTopRank',
      if (ownerReadyFirstLaneRole.isNotEmpty)
        'Lane role: $ownerReadyFirstLaneRole',
      if (ownerReadyFirstPr.isNotEmpty) 'PR: $ownerReadyFirstPr',
      if (ownerReadyFirstOwner.isNotEmpty) 'Owner: $ownerReadyFirstOwner',
      if (ownerReadyFirstState.isNotEmpty) 'State: $ownerReadyFirstState',
      if (ownerReadyFirstRequiredLanes.isNotEmpty)
        'Required lanes: $ownerReadyFirstRequiredLanes',
      if (ownerReadyFirstExistingRequest.isNotEmpty &&
          ownerReadyFirstExistingRequest.toLowerCase() != 'none')
        'Existing request: $ownerReadyFirstExistingRequest',
      if (ownerReadyFirstNextAction.isNotEmpty)
        'Next action: $ownerReadyFirstNextAction',
      if (prRecoveryContractDetail.isNotEmpty)
        'Recovery contract: $prRecoveryContractDetail',
      if (ownerReadyFirstPath.isNotEmpty) 'Report: $ownerReadyFirstPath',
      if (ownerReadyFirstOpenPath.isNotEmpty)
        'Open path: $ownerReadyFirstOpenPath',
      if (ownerReadyFirstSafety.isNotEmpty)
        'Safety boundary: $ownerReadyFirstSafety',
    ];
    return lines.length > 1 ? lines.join('\n') : '';
  }

  bool get hasExpediteOpenTarget =>
      stableInboxProcessed ||
      stableInboxLatestRequestOpenPath.isNotEmpty ||
      stableInboxLatestHandoffCopy.isNotEmpty ||
      stableInboxRequestOpenPath.isNotEmpty ||
      stableInboxHandoffCopy.isNotEmpty ||
      hasOwnerReadyFirstTarget ||
      expediteBoardOpenPath.isNotEmpty ||
      expediteBoardPath.isNotEmpty;
  bool get hasStableInboxHandoff =>
      stableInboxProcessed ||
      stableInboxLatestRequestOpenPath.isNotEmpty ||
      stableInboxLatestHandoffCopy.isNotEmpty ||
      stableInboxRequestOpenPath.isNotEmpty ||
      stableInboxHandoffCopy.isNotEmpty;
  String get expeditePrimaryOpenPath => stableInboxProcessed
      ? ''
      : stableInboxLatestRequestOpenPath.isNotEmpty
          ? stableInboxLatestRequestOpenPath
          : stableInboxRequestOpenPath.isNotEmpty
              ? stableInboxRequestOpenPath
              : ownerReadyFirstPrimaryOpenPath.isNotEmpty
                  ? ownerReadyFirstPrimaryOpenPath
                  : expediteBoardOpenPath.isNotEmpty
                      ? expediteBoardOpenPath
                      : expediteBoardPath;
  String get expediteOpenActionLabel => hasStableInboxHandoff
      ? stableInboxProcessed
          ? 'Copy inbox receipt'
          : stableInboxStaleForLatest
              ? 'Open/copy latest inbox'
              : stableInboxTopRank > 0
                  ? 'Open/copy inbox #$stableInboxTopRank'
                  : 'Open/copy inbox handoff'
      : hasOwnerReadyFirstTarget
          ? ownerReadyFirstOpenActionLabel
          : expediteTopRank > 0
              ? 'Open/copy board #$expediteTopRank'
              : 'Open/copy board';
  String get stableInboxReviewDetail {
    final parts = <String>[];
    final stale = stableInboxStaleForLatest;
    final priority = stale && stableInboxLatestRequestPriority.isNotEmpty
        ? stableInboxLatestRequestPriority
        : stableInboxRequestPriority;
    final from = stale && stableInboxLatestRequestFrom.isNotEmpty
        ? stableInboxLatestRequestFrom
        : stableInboxRequestFrom;
    final to = stale && stableInboxLatestRequestTo.isNotEmpty
        ? stableInboxLatestRequestTo
        : stableInboxRequestTo;
    final backlog = stale && stableInboxLatestRequestBacklogId.isNotEmpty
        ? stableInboxLatestRequestBacklogId
        : stableInboxRequestBacklogId;
    final path = stale && stableInboxLatestRequestPath.isNotEmpty
        ? stableInboxLatestRequestPath
        : stableInboxRequestPath;
    final preview = stale && stableInboxLatestRequestPreview.isNotEmpty
        ? stableInboxLatestRequestPreview
        : stableInboxRequestPreview;
    if (stableInboxTopRank > 0) {
      parts.add(stale
          ? 'Stale inbox #$stableInboxTopRank'
          : 'Inbox #$stableInboxTopRank');
    }
    if (stale && stableInboxLatestRequestCreated.isNotEmpty) {
      parts.add('newer $stableInboxLatestRequestCreated');
    }
    if (stableInboxProcessedReviewDetail.isNotEmpty) {
      parts.add(stableInboxProcessedReviewDetail);
    }
    if (priority.isNotEmpty) {
      parts.add(priority);
    }
    if (from.isNotEmpty || to.isNotEmpty) {
      parts.add(
        [from, to].where((part) => part.isNotEmpty).join(' -> '),
      );
    }
    if (backlog.isNotEmpty) {
      parts.add(backlog);
    } else if (path.isNotEmpty) {
      parts.add(path);
    }
    if (preview.isNotEmpty) {
      parts.add(preview);
    }
    if (parts.isEmpty) return '';
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(' | '), 140);
  }

  String get _stableInboxContractExpected => stableInboxStaleForLatest &&
          stableInboxLatestRequestExpectedDeliverable.isNotEmpty
      ? stableInboxLatestRequestExpectedDeliverable
      : stableInboxRequestExpectedDeliverable;
  String get _stableInboxContractSuccess => stableInboxStaleForLatest &&
          stableInboxLatestRequestSuccessCriteria.isNotEmpty
      ? stableInboxLatestRequestSuccessCriteria
      : stableInboxRequestSuccessCriteria;
  String get _stableInboxContractSafety =>
      stableInboxStaleForLatest && stableInboxLatestRequestSafety.isNotEmpty
          ? stableInboxLatestRequestSafety
          : stableInboxRequestSafety;
  bool get hasStableInboxCompletionContract =>
      _stableInboxContractExpected.isNotEmpty ||
      _stableInboxContractSuccess.isNotEmpty ||
      _stableInboxContractSafety.isNotEmpty;
  String get stableInboxCompletionContractChipLabel =>
      hasStableInboxCompletionContract ? 'proof contract' : '';
  String get stableInboxCompletionContractDetail {
    if (!hasStableInboxCompletionContract) return '';
    final parts = <String>[
      if (_stableInboxContractExpected.isNotEmpty)
        'Deliverable: $_stableInboxContractExpected',
      if (_stableInboxContractSuccess.isNotEmpty)
        'Success: $_stableInboxContractSuccess',
      if (_stableInboxContractSafety.isNotEmpty)
        'Safety: $_stableInboxContractSafety',
    ];
    return AutopilotAgentBenchActivityPresenter._clip(
      'Completion contract: ${parts.join(' | ')}',
      260,
    );
  }

  String get _expediteDecisionSample =>
      '$expediteTopType $expediteTopEvidence $expediteNextAction'.toLowerCase();
  String get _expediteCompactOwnerLabel {
    final parts = expediteTopOwner
        .split(RegExp(r'\s*/\s*'))
        .map((part) => part.trim())
        .where((part) => part.isNotEmpty)
        .toList(growable: false);
    final concrete = parts.where((part) {
      final lower = part.toLowerCase();
      return lower != 'operator' &&
          lower != 'owner' &&
          lower != 'owners' &&
          lower != 'stalled owners' &&
          lower != 'affected owners';
    }).toList(growable: false);
    final visibleOwners = concrete.isNotEmpty ? concrete : parts;
    if (visibleOwners.isEmpty) return '';
    final visible = visibleOwners.take(2).join('/');
    final extra = visibleOwners.length - 2;
    return AutopilotAgentBenchActivityPresenter._clip(
      extra > 0 ? '$visible +$extra' : visible,
      24,
    );
  }

  String get expediteOwnerActionChipLabel {
    if (expediteTopRank <= 0 || expediteTopOwner.isEmpty) return '';
    if (expeditePrPostureChipLabel.isEmpty &&
        expediteDecisionChipLabel.isEmpty) {
      return '';
    }
    final owner = _expediteCompactOwnerLabel;
    return owner.isEmpty ? 'owner next' : '$owner next';
  }

  String get expediteOwnerActionDetail {
    if (expediteOwnerActionChipLabel.isEmpty) return '';
    var action = expediteNextAction.trim();
    while (
        action.endsWith('.') || action.endsWith(';') || action.endsWith(':')) {
      action = action.substring(0, action.length - 1).trim();
    }
    final owner = expediteTopOwner.trim();
    final gate = expeditePrPostureChipLabel == 'PR blocked'
        ? 'keep lane blocked until owner supplies Worktree/branch/head evidence'
        : expediteHeartbeatLagChipLabel.isNotEmpty
            ? 'process oldest pending request or repair owner heartbeat before routine work'
            : stableInboxHasControlPlaneGate
                ? 'control-plane proof required; no session, quarantine, source/runtime trust, PR, release, or runtime authority'
                : stableInboxHasLiveControlGate
                    ? 'live-control disposition required; no runtime, broker/API, release, or live-trading authority'
                    : stableInboxClassificationOnly
                        ? 'classification-only disposition required; no runtime, broker/API, or live-trading authority'
                        : expeditePrPostureChipLabel == 'ready review'
                            ? 'PM/operator acceptance required before promotion'
                            : expeditePrPostureChipLabel == 'needs PR decision'
                                ? 'decision required before PR state changes'
                                : 'owner evidence required before routine work continues';
    return AutopilotAgentBenchActivityPresenter._clip(
      [
        'Owner/action: ${owner.isEmpty ? 'owner' : owner}',
        if (action.isNotEmpty) action,
        gate,
      ].join(' | '),
      240,
    );
  }

  bool get _expediteSampleIsReadyCandidate {
    final sample = _expediteDecisionSample;
    return sample.contains('ready_candidate') ||
        sample.contains('ready candidate') ||
        sample.contains('ready draft');
  }

  bool get _expediteSampleIsPrBlocker {
    final sample = _expediteDecisionSample;
    return sample.contains('open_pr') ||
        sample.contains('pr #') ||
        sample.contains('checks') ||
        RegExp(r'(^|[^a-z0-9])ci([^a-z0-9]|$)').hasMatch(sample) ||
        sample.contains('merge=dirty') ||
        sample.contains('merge=unstable') ||
        sample.contains('test:failure');
  }

  bool get _expediteSampleIsHeartbeatLag {
    final sample = _expediteDecisionSample;
    return sample.contains('heartbeat_delivery_lag') ||
        sample.contains('heartbeat lag') ||
        sample.contains('owner heartbeat') ||
        sample.contains('target session stale') ||
        sample.contains('repair the owner heartbeat');
  }

  String get expeditePrPostureChipLabel {
    if (expediteTopRank <= 0) return '';
    if (_expediteSampleIsReadyCandidate) return 'ready review';
    if (_expediteSampleIsPrBlocker) {
      return 'PR blocked';
    }
    return '';
  }

  bool get expeditePrPostureBlocked =>
      expeditePrPostureChipLabel == 'PR blocked';

  String get _expeditePrBlockerReason {
    final sample = _expediteDecisionSample;
    if (sample.contains('no checks') ||
        sample.contains('ci_missing') ||
        sample.contains('missing checks')) {
      return 'missing current-head checks';
    }
    if (sample.contains('test:failure') ||
        sample.contains('ci_failing') ||
        sample.contains('failure')) {
      return 'failing checks';
    }
    if (sample.contains('merge=dirty') || sample.contains('merge dirty')) {
      return 'dirty merge state';
    }
    if (sample.contains('merge=unstable') ||
        sample.contains('merge unstable')) {
      return 'unstable merge state';
    }
    return 'blocked board evidence';
  }

  String get expeditePrPostureDetail {
    final chip = expeditePrPostureChipLabel;
    if (chip.isEmpty) return '';
    final evidence = expediteTopEvidence.isEmpty
        ? ''
        : ' | ${AutopilotAgentBenchActivityPresenter._clip(expediteTopEvidence, 92)}';
    if (chip == 'ready review') {
      return 'PR posture: ready review; verify current-head checks and PM/operator acceptance before promoting$evidence.';
    }
    if (chip == 'needs PR decision') {
      return 'PR posture: needs decision; use board evidence before changing PR state$evidence.';
    }
    return 'PR posture: blocked by $_expeditePrBlockerReason; keep blocked or rebuild current-head checks with Worktree/branch/head evidence$evidence.';
  }

  String get _expediteEvidenceRequirementKey {
    if (expediteTopRank <= 0) return '';
    final sample = _expediteDecisionSample;
    if (sample.trim().isEmpty) return '';
    if (_expediteSampleIsReadyCandidate) return '';
    if (sample.contains('no checks') ||
        sample.contains('ci_missing') ||
        sample.contains('missing checks')) {
      return 'needs_checks';
    }
    if (sample.contains('test:failure') ||
        sample.contains('ci_failing') ||
        sample.contains('failure')) {
      return 'failing_ci';
    }
    if (sample.contains('merge=dirty') || sample.contains('merge dirty')) {
      return 'dirty_merge';
    }
    if (sample.contains('merge=unstable') ||
        sample.contains('merge unstable')) {
      return 'unstable_merge';
    }
    if (_expediteSampleIsPrBlocker) {
      return 'evidence_needed';
    }
    return '';
  }

  String get expediteEvidenceRequiredChipLabel {
    switch (_expediteEvidenceRequirementKey) {
      case 'needs_checks':
        return 'needs checks';
      case 'failing_ci':
        return 'failing CI';
      case 'dirty_merge':
        return 'dirty merge';
      case 'unstable_merge':
        return 'unstable merge';
      case 'evidence_needed':
        return 'evidence needed';
      default:
        return '';
    }
  }

  bool get expediteEvidenceRequiredCritical =>
      expediteEvidenceRequiredChipLabel == 'needs checks' ||
      expediteEvidenceRequiredChipLabel == 'failing CI';

  String get expediteEvidenceRequiredDetail {
    if (expediteEvidenceRequiredChipLabel.isEmpty) return '';
    final evidence = expediteTopEvidence.isEmpty
        ? ''
        : ' | ${AutopilotAgentBenchActivityPresenter._clip(expediteTopEvidence, 92)}';
    switch (_expediteEvidenceRequirementKey) {
      case 'needs_checks':
        return 'Evidence required: current-head checks are missing; rebuild checks with Worktree/branch/head evidence before PR state changes$evidence.';
      case 'failing_ci':
        return 'Evidence required: checks are failing; assign the failing check owner and keep the PR blocked until a current-head pass exists$evidence.';
      case 'dirty_merge':
        return 'Evidence required: merge state is dirty; classify branch/base state and keep PR movement blocked until resolved$evidence.';
      case 'unstable_merge':
        return 'Evidence required: merge state is unstable; classify branch/base state and keep PR movement blocked until stable$evidence.';
      case 'evidence_needed':
        return 'Evidence required: board evidence is incomplete; keep the PR blocked until owner, Worktree, branch, and head proof are attached$evidence.';
      default:
        return '';
    }
  }

  bool get hasPrPreflightPressure =>
      expediteOpenPrBlockerCount > 0 ||
      expediteReadyCandidateCount > 0 ||
      expeditePrPostureChipLabel.isNotEmpty ||
      expediteEvidenceRequiredChipLabel.isNotEmpty ||
      hasOwnerReadyFirstPressure ||
      kpiBlockedPrCount > 0 ||
      kpiReadyCandidateCount > 0 ||
      hasFlowSourceIsolation ||
      flowWorktreeMergeConflictCount > 0;

  bool get _hasPrPreflightSafetyBlocker =>
      hasFlowSourceIsolation ||
      flowWorktreeMergeConflictCount > 0 ||
      flowControlPlaneBlockerCount > 0 ||
      flowQuarantinedTargetCount > 0 ||
      flowPausedAutomationCount > 0 ||
      goalHealthCritical ||
      expediteControlPlaneCount > 0 ||
      stableInboxHasControlPlaneGate ||
      stableInboxHasLiveControlGate;

  bool get prPreflightBlocked =>
      hasPrPreflightPressure &&
      (_hasPrPreflightSafetyBlocker ||
          expeditePrPostureBlocked ||
          expediteEvidenceRequiredChipLabel.isNotEmpty ||
          expediteOpenPrBlockerCount > 0 ||
          kpiBlockedPrCount > 0);

  bool get prPreflightReadyCandidate =>
      hasPrPreflightPressure &&
      !prPreflightBlocked &&
      (expediteReadyCandidateCount > 0 ||
          kpiReadyCandidateCount > 0 ||
          expeditePrPostureChipLabel == 'ready review');

  String get prPreflightChipLabel {
    if (!hasPrPreflightPressure) return '';
    if (prPreflightBlocked) return 'PR preflight blocked';
    if (prPreflightReadyCandidate) return 'PR preflight ready';
    return 'PR preflight';
  }

  String get _prPreflightPrLabel {
    if (ownerReadyFirstPrNumber.isNotEmpty) {
      return 'PR #$ownerReadyFirstPrNumber';
    }
    if (ownerReadyFirstPr.isNotEmpty) {
      final trimmed = ownerReadyFirstPr.trim();
      if (trimmed.startsWith('#')) return 'PR $trimmed';
      return AutopilotAgentBenchActivityPresenter._clip(trimmed, 48);
    }
    final evidence = [expediteTopEvidence, expediteSignal, kpiSignal]
        .where((part) => part.trim().isNotEmpty)
        .join(' ');
    final match =
        RegExp(r'\bPR\s*#?(\d+)\b', caseSensitive: false).firstMatch(evidence);
    if (match != null) return 'PR #${match.group(1)}';
    if (expeditePrNumbers.isNotEmpty) {
      return 'PR #${expeditePrNumbers.first}';
    }
    if (kpiPrNumbers.isNotEmpty) {
      return 'PR #${kpiPrNumbers.first}';
    }
    return '';
  }

  String get _prPreflightPressureSummary {
    final parts = <String>[];
    void add(String value) {
      final clean = value.trim();
      if (clean.isEmpty || parts.contains(clean)) return;
      parts.add(clean);
    }

    if (expediteTopRank > 0) add('board #$expediteTopRank');
    if (ownerReadyFirstTopRank > 0) {
      add('owner-ready #$ownerReadyFirstTopRank');
    }
    add(_prPreflightPrLabel);
    if (expediteOpenPrBlockerCount > 0) {
      add('$expediteOpenPrBlockerCount board PR blocker${expediteOpenPrBlockerCount == 1 ? '' : 's'}');
    }
    if (kpiBlockedPrCount > 0) {
      add('$kpiBlockedPrCount scorecard PR blocker${kpiBlockedPrCount == 1 ? '' : 's'}');
    }
    if (expediteReadyCandidateCount > 0) {
      add('$expediteReadyCandidateCount board ready');
    }
    if (kpiReadyCandidateCount > 0) {
      add('$kpiReadyCandidateCount scorecard ready');
    }
    if (parts.isEmpty) return 'PR work';
    return AutopilotAgentBenchActivityPresenter._clip(parts.join(', '), 120);
  }

  String get prPreflightLabel {
    if (!hasPrPreflightPressure) return '';
    return AutopilotAgentBenchActivityPresenter._clip(
      '$prPreflightChipLabel: $_prPreflightPressureSummary',
      140,
    );
  }

  String get _prPreflightNextAction {
    if (shouldPrioritizeFlowSafetyAction && flowNextAction.isNotEmpty) {
      return flowNextAction;
    }
    if (ownerReadyFirstNextAction.isNotEmpty) {
      return ownerReadyFirstNextAction;
    }
    if (expediteNextAction.isNotEmpty) return expediteNextAction;
    if (flowNextAction.isNotEmpty) return flowNextAction;
    return '';
  }

  String get _prPreflightEvidenceSummary {
    final evidence = expediteTopEvidence.trim();
    if (evidence.isNotEmpty) {
      return AutopilotAgentBenchActivityPresenter._clip(evidence, 120);
    }
    if (ownerReadyFirstPr.isNotEmpty || ownerReadyFirstTitle.isNotEmpty) {
      return AutopilotAgentBenchActivityPresenter._clip(
        [ownerReadyFirstPr, ownerReadyFirstTitle]
            .where((part) => part.trim().isNotEmpty)
            .join(' | '),
        120,
      );
    }
    if (expediteSignal.isNotEmpty) {
      return AutopilotAgentBenchActivityPresenter._clip(expediteSignal, 120);
    }
    if (kpiSignal.isNotEmpty) {
      return AutopilotAgentBenchActivityPresenter._clip(kpiSignal, 120);
    }
    return '';
  }

  String get prPreflightDetail {
    if (!hasPrPreflightPressure) return '';
    final blockers = <String>[];
    void addBlocker(String value) {
      final clean = value.trim();
      if (clean.isEmpty || blockers.contains(clean)) return;
      blockers.add(clean);
    }

    if (hasFlowSourceIsolation) addBlocker(_flowSourceIsolationChangeLabel);
    if (flowWorktreeMergeConflictCount > 0) addBlocker(flowWorktreeChipLabel);
    if (expediteEvidenceRequiredChipLabel.isNotEmpty) {
      addBlocker(expediteEvidenceRequiredChipLabel);
    } else if (expeditePrPostureBlocked) {
      addBlocker(_expeditePrBlockerReason);
    }
    addBlocker(expediteControlPlaneChipLabel);
    addBlocker(flowContainmentChipLabel);
    addBlocker(flowPausedAutomationChipLabel);
    if (goalHealthCritical) addBlocker(goalHealthChipLabel);
    addBlocker(stableInboxControlPlaneChipLabel);
    addBlocker(stableInboxLiveControlGateChipLabel);

    final parts = <String>[
      _prPreflightPressureSummary,
      if (blockers.isNotEmpty) 'blockers ${blockers.join(', ')}',
      if (flowSourceTrustEvidenceLabel.isNotEmpty) flowSourceTrustEvidenceLabel,
      if (_prPreflightNextAction.isNotEmpty) 'next $_prPreflightNextAction',
      if (kpiPrBlockerProofFloorDetail.isNotEmpty) kpiPrBlockerProofFloorDetail,
      if (prDeliveryRouteDetail.isNotEmpty) prDeliveryRouteDetail,
      if (prPreflightReadyCandidate)
        'verify current-head checks and PM/operator acceptance',
      if (_prPreflightEvidenceSummary.isNotEmpty)
        'evidence $_prPreflightEvidenceSummary',
    ];
    return AutopilotAgentBenchActivityPresenter._clip(
      '$prPreflightChipLabel: ${parts.join(' | ')}',
      600,
    );
  }

  String get _prDeliveryRouteSample => [
        expediteTopType,
        expediteTopEvidence,
        expediteNextAction,
        expediteSignal,
        ownerReadyFirstState,
        ownerReadyFirstNextAction,
        ownerReadyFirstRequiredLanes,
        ownerReadyFirstExistingRequest,
        ownerReadyFirstSafety,
        kpiSignal,
      ].where((part) => part.trim().isNotEmpty).join(' ').toLowerCase();

  String get _prDeliveryRouteKey {
    if (!hasPrPreflightPressure) return '';
    final sample = _prDeliveryRouteSample;
    if (expediteTopType.toLowerCase().contains('ready_candidate') ||
        expediteTopType.toLowerCase().contains('ready candidate') ||
        expediteTopType.toLowerCase().contains('ready draft') ||
        expeditePrPostureChipLabel == 'ready review') {
      return 'review_only';
    }
    if (sample.contains('do not repair it in place') ||
        sample.contains('do not repair in place') ||
        sample.contains('close/recreate') ||
        sample.contains('clean rebuild') ||
        sample.contains('rebuild only through clean owner worktrees')) {
      return 'clean_rebuild';
    }
    if (sample.contains('isolated pr worktree') ||
        sample.contains('isolated worktree') ||
        sample.contains('owner worktree')) {
      return 'isolated_worktree';
    }
    if (sample.contains('pm/operator disposition') ||
        sample.contains('pm_operator_disposition_required') ||
        sample.contains('operator disposition already blocks')) {
      return 'pm_disposition';
    }
    if (sample.contains('no checks') ||
        sample.contains('missing checks') ||
        sample.contains('ci_missing') ||
        sample.contains('test:failure') ||
        sample.contains('ci_failing') ||
        sample.contains('merge=dirty') ||
        sample.contains('merge=unstable') ||
        sample.contains('merge dirty') ||
        sample.contains('merge unstable')) {
      return 'current_head_checks';
    }
    if (hasFlowSourceIsolation || flowWorktreeMergeConflictCount > 0) {
      return 'source_trust_first';
    }
    return '';
  }

  String get prDeliveryRouteChipLabel {
    switch (_prDeliveryRouteKey) {
      case 'review_only':
        return 'review only';
      case 'clean_rebuild':
        return 'clean rebuild only';
      case 'isolated_worktree':
        return 'isolated worktree';
      case 'pm_disposition':
        return 'PM disposition';
      case 'current_head_checks':
        return 'current-head checks';
      case 'source_trust_first':
        return 'source trust first';
      default:
        return '';
    }
  }

  bool get prDeliveryRouteHighRisk =>
      _prDeliveryRouteKey == 'clean_rebuild' ||
      _prDeliveryRouteKey == 'pm_disposition' ||
      _prDeliveryRouteKey == 'source_trust_first';

  bool get prDeliveryRouteReviewOnly => _prDeliveryRouteKey == 'review_only';

  bool get hasPrRecoveryContract =>
      hasPrPreflightPressure &&
      _prDeliveryRouteKey.isNotEmpty &&
      _prDeliveryRouteKey != 'review_only' &&
      (prPreflightBlocked || hasOwnerReadyFirstPressure);

  String get prRecoveryContractChipLabel =>
      hasPrRecoveryContract ? 'PR recovery contract' : '';

  String get prRecoveryContractLabel {
    if (!hasPrRecoveryContract) return '';
    final pr = _prPreflightPrLabel;
    final subject = pr.isEmpty ? 'PR recovery' : '$pr recovery';
    return AutopilotAgentBenchActivityPresenter._clip(
      '$subject: ${prDeliveryRouteChipLabel.isEmpty ? 'blocked' : prDeliveryRouteChipLabel}',
      120,
    );
  }

  String get _prRecoveryProofLabel {
    final parts = <String>[
      'PM/operator acceptance',
      'clean owner worktree',
      'Worktree/branch/head evidence',
      'current-head checks',
    ];
    return parts.join(' + ');
  }

  String get prRecoveryContractDetail {
    if (!hasPrRecoveryContract) return '';
    final pr = _prPreflightPrLabel;
    final subject = pr.isEmpty ? 'PR recovery contract' : 'PR recovery for $pr';
    final route = _prDeliveryRouteKey;
    final proof = _prRecoveryProofLabel;
    switch (route) {
      case 'clean_rebuild':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: PM/operator must accept close/recreate, clean rebuild, current-head gates, or one named repair path before any PR movement. Keep the current PR frozen; do not repair it in place. Required proof: $proof.',
          360,
        );
      case 'pm_disposition':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: PM/operator disposition controls the lane. Allowed outcomes are keep blocked, close/recreate, clean rebuild, current-head gates, or one named repair path. Required proof: $proof.',
          340,
        );
      case 'current_head_checks':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: keep blocked or rebuild current-head checks from an isolated owner worktree before ready/merge claims. Required proof: Worktree/branch/head plus selected-test evidence and PM/operator acceptance.',
          340,
        );
      case 'isolated_worktree':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: owner resolves or publishes cannot-resolve evidence from an isolated PR worktree, then support lanes refresh CI/review evidence. No PR-state movement until PM/operator accepts the result.',
          340,
        );
      case 'source_trust_first':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: source trust must clear first. Shared-root dirt, merge conflicts, or untrusted worktree state cannot be used as PR recovery evidence.',
          300,
        );
      default:
        return '';
    }
  }

  String get prRecoveryContractCopyText {
    if (!hasPrRecoveryContract) return '';
    final routeLabel = prDeliveryRouteChipLabel.isEmpty
        ? _prDeliveryRouteKey
        : prDeliveryRouteChipLabel;
    final lines = <String>[
      'Project Autopilot PR recovery contract',
      if (_prPreflightPrLabel.isNotEmpty) 'PR: $_prPreflightPrLabel',
      if (routeLabel.isNotEmpty) 'Route: $routeLabel',
      if (_prPreflightNextAction.isNotEmpty)
        'Next action: $_prPreflightNextAction',
      if (prRecoveryContractDetail.isNotEmpty)
        'Contract: $prRecoveryContractDetail',
      if (prBlockerProofFloorCopyText.isNotEmpty) prBlockerProofFloorCopyText,
      'Allowed decisions:',
      '- Keep blocked with exact owner/evidence/branch/head named.',
      '- Close or defer with explicit PM/operator acceptance.',
      '- Rebuild only through a clean owner worktree with Worktree/branch/head and current-head check evidence.',
      '- Accept one named repair path only if PM/operator records it for this current head.',
      'Safety boundary: this copied recovery contract is decision-only. It does not authorize source edits, branch mutation, PR-state change, commit, push, merge, release, deploy, runtime, database, broker/API, or live-trading action.',
    ];
    return lines.join('\n');
  }

  String get prDeliveryRouteDetail {
    final route = _prDeliveryRouteKey;
    if (route.isEmpty) return '';
    final pr = _prPreflightPrLabel;
    final subject =
        pr.isEmpty ? 'PR delivery route' : 'PR delivery route for $pr';
    final evidence = _prPreflightEvidenceSummary.isEmpty
        ? ''
        : ' Evidence: ${AutopilotAgentBenchActivityPresenter._clip(_prPreflightEvidenceSummary, 96)}.';
    switch (route) {
      case 'review_only':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: review-only; verify current-head checks and PM/operator acceptance before promotion. No merge authority is implied.$evidence',
          280,
        );
      case 'clean_rebuild':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: keep the current PR frozen; do not repair it in place. Close/recreate or rebuild only through a clean owner worktree after PM/operator acceptance, with Worktree/branch/head and current-head check evidence.$evidence',
          320,
        );
      case 'isolated_worktree':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: resolve or publish cannot-resolve evidence from an isolated PR worktree, then coordinate required support lanes for CI/review refresh.$evidence',
          300,
        );
      case 'pm_disposition':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: PM/operator disposition controls this lane; keep it frozen until close/recreate, clean rebuild, current-head gates, or one named repair path is accepted.$evidence',
          300,
        );
      case 'current_head_checks':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: rebuild current-head checks from an isolated owner worktree and attach Worktree/branch/head plus selected-test evidence before marking PR-ready.$evidence',
          300,
        );
      case 'source_trust_first':
        return AutopilotAgentBenchActivityPresenter._clip(
          '$subject: clear source trust first; shared-root dirt or merge conflicts are not PR-ready evidence.',
          240,
        );
      default:
        return '';
    }
  }

  String get expediteHeartbeatLagChipLabel =>
      _expediteSampleIsHeartbeatLag && expediteTopRank > 0
          ? _heartbeatLagChipLabelFromEvidence
          : '';

  String get _heartbeatLagChipLabelFromEvidence {
    final match = RegExp(r'(\d+)\s+stalled owner lane')
        .firstMatch(expediteTopEvidence.toLowerCase());
    final count = int.tryParse(match?.group(1) ?? '');
    if (count != null && count > 1) {
      return '$count heartbeat lags';
    }
    return 'heartbeat lag';
  }

  String get expediteHeartbeatLagDetail {
    if (expediteHeartbeatLagChipLabel.isEmpty) return '';
    final evidence = expediteTopEvidence.isEmpty
        ? ''
        : ' | ${AutopilotAgentBenchActivityPresenter._clip(expediteTopEvidence, 96)}';
    return 'Heartbeat lag: owner session stale; process oldest pending request or repair owner heartbeat before routine work$evidence.';
  }

  bool get hasHeartbeatRecoverySplit =>
      expediteHeartbeatLagChipLabel.isNotEmpty;
  String get _effectiveStableInboxRequestPath =>
      stableInboxStaleForLatest && stableInboxLatestRequestPath.isNotEmpty
          ? stableInboxLatestRequestPath
          : stableInboxRequestPath;
  String get _effectiveStableInboxRequestTail {
    final path = _effectiveStableInboxRequestPath.trim();
    if (path.isEmpty) return '';
    return path.split(RegExp(r'[\\/]')).where((part) => part.isNotEmpty).last;
  }

  bool get hasHeartbeatInboxRoute =>
      hasHeartbeatRecoverySplit &&
      (_effectiveStableInboxRequestPath.isNotEmpty ||
          stableInboxHandoffCopy.isNotEmpty ||
          stableInboxLatestHandoffCopy.isNotEmpty);
  String get expediteHeartbeatInboxRouteChipLabel =>
      hasHeartbeatInboxRoute ? 'heartbeat inbox' : '';
  String get expediteHeartbeatInboxRouteDetail {
    if (!hasHeartbeatInboxRoute) return '';
    final rank = stableInboxTopRank > 0
        ? 'board #$stableInboxTopRank'
        : 'stable inbox request';
    final request = _effectiveStableInboxRequestTail.isEmpty
        ? 'the stable inbox request'
        : _effectiveStableInboxRequestTail;
    final contract =
        hasStableInboxCompletionContract ? '; answer the proof contract' : '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Open $rank ($request) before heartbeat repair; record processed receipt evidence$contract, then repair heartbeat only if the owner session remains stale and no duplicate worker owns the lane.',
      260,
    );
  }

  String get expeditePendingRequestChipLabel =>
      hasHeartbeatRecoverySplit ? 'process request' : '';
  String get expediteHeartbeatRepairChipLabel =>
      hasHeartbeatRecoverySplit ? 'repair heartbeat' : '';
  String get expeditePendingRequestDetail {
    if (!hasHeartbeatRecoverySplit) return '';
    final requestPath = stableInboxRequestPath.trim();
    final request = requestPath.isEmpty
        ? 'oldest stable inbox request'
        : requestPath
            .split(RegExp(r'[\\/]'))
            .where((part) => part.isNotEmpty)
            .last;
    final contract =
        hasStableInboxCompletionContract ? '; use the proof contract' : '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Process request: close the pending stable inbox request with processed receipt evidence$contract; higher-ranked containment still blocks source/runtime trust; request $request.',
      220,
    );
  }

  String get expediteHeartbeatRepairDetail {
    if (!hasHeartbeatRecoverySplit) return '';
    return 'Repair heartbeat: reconnect or repair the owner heartbeat only after confirming no duplicate worker owns the lane; keep blocked or escalate if the stale session cannot be proven active or stopped.';
  }

  String get expediteDecisionChipLabel {
    final sample = _expediteDecisionSample;
    if (sample.trim().isEmpty || expediteTopRank <= 0) return '';
    if (_expediteSampleIsHeartbeatLag) {
      return 'decide: process/repair/block';
    }
    if (stableInboxHasControlPlaneGate ||
        sample.contains('control_plane') ||
        sample.contains('control-plane') ||
        sample.contains('control plane') ||
        sample.contains('stop proof') ||
        sample.contains('containment') ||
        sample.contains('quarantine')) {
      return 'decide: prove/terminate/block';
    }
    if (stableInboxRequiresDecisionBoundary) {
      return 'decide: classify/defer/escalate';
    }
    if (_expediteSampleIsReadyCandidate) {
      return 'decide: promote/defer/close';
    }
    if (_expediteSampleIsPrBlocker) {
      return 'decide: keep/rebuild/close';
    }
    return 'decide: accept/defer/escalate';
  }

  String get expediteSafeDecisionMenu {
    final sample = _expediteDecisionSample;
    final decisions = <String>[];
    if (_expediteSampleIsHeartbeatLag) {
      decisions.addAll([
        'Process pending request: have the owner close the oldest stable request with receipt evidence.',
        'Repair heartbeat: reconnect or repair the owner heartbeat only after confirming no duplicate worker owns the lane.',
        'Keep blocked: keep the owner lane blocked and escalate if the stale session cannot be proven active or stopped.',
      ]);
    } else if (stableInboxHasControlPlaneGate ||
        sample.contains('control_plane') ||
        sample.contains('control-plane') ||
        sample.contains('control plane') ||
        sample.contains('stop proof') ||
        sample.contains('containment') ||
        sample.contains('quarantine')) {
      decisions.addAll([
        'Prove containment: attach current quiet-window proof before restoring source/runtime trust.',
        'Terminate target: stop or archive the target before accepting trust.',
        'Keep blocked: leave source/runtime trust blocked until fresh proof exists.',
      ]);
    } else if (stableInboxRequiresDecisionBoundary) {
      decisions.addAll([
        stableInboxHasLiveControlGate
            ? 'Classify live-control evidence: record the PM/operator proof-floor disposition without changing runtime, broker/API, capital, monitor, route, release, or live-trading posture.'
            : 'Classify evidence: record the PM/operator disposition without changing runtime, broker/API, capital, monitor, route, release, or live-trading posture.',
        'Defer: keep the request blocked with a written reason and required owner evidence.',
        'Escalate: ask PM/operator/control-plane for explicit direction if the packet implies execution or runtime authority.',
      ]);
    } else if (_expediteSampleIsReadyCandidate) {
      decisions.addAll([
        'Promote ready: PM/operator accepts draft-to-ready only after higher blockers are clear.',
        'Defer: keep the draft behind higher-priority board rows.',
        'Reject or close: document why the candidate is no longer viable.',
      ]);
    } else if (_expediteSampleIsPrBlocker) {
      decisions.addAll([
        'Keep blocked: record the owner and next check path; avoid routine work in that lane.',
        'Rebuild checks: create a current-head check path with Worktree, branch, and head evidence.',
        'Close or defer: explicitly close/defer stale PR work with PM/operator acceptance.',
      ]);
    } else if (expediteTopRank > 0) {
      decisions.addAll([
        'Accept: assign the owner and required evidence before work continues.',
        'Defer: record why this row stays behind higher-priority blockers.',
        'Escalate: ask PM/operator for a written disposition if ownership is unclear.',
      ]);
    }
    if (decisions.isEmpty) return '';
    return [
      'Safe operator decision menu:',
      for (final decision in decisions) '- $decision',
      'Safety boundary: this copied handoff is decision-only; it does not authorize merge, release, deploy, runtime, database, broker/API, or live-trading action.',
    ].join('\n');
  }

  String get expediteSafeDecisionDetail {
    final sample = _expediteDecisionSample;
    if (sample.trim().isEmpty || expediteTopRank <= 0) return '';
    if (_expediteSampleIsHeartbeatLag) {
      return 'Decision ladder: process pending request | repair owner heartbeat | keep blocked or escalate.';
    }
    if (stableInboxHasControlPlaneGate ||
        sample.contains('control_plane') ||
        sample.contains('control-plane') ||
        sample.contains('control plane') ||
        sample.contains('stop proof') ||
        sample.contains('containment') ||
        sample.contains('quarantine')) {
      return 'Decision ladder: prove containment | terminate target | keep blocked.';
    }
    if (stableInboxRequiresDecisionBoundary) {
      return stableInboxHasLiveControlGate
          ? 'Decision ladder: classify live-control evidence | defer with reason | escalate execution ambiguity.'
          : 'Decision ladder: classify evidence only | defer with reason | escalate execution ambiguity.';
    }
    if (_expediteSampleIsReadyCandidate) {
      return 'Decision ladder: promote ready | defer behind blockers | reject or close.';
    }
    if (_expediteSampleIsPrBlocker) {
      return 'Decision ladder: keep blocked | rebuild current-head checks with Worktree/branch/head evidence | close or defer with PM/operator acceptance.';
    }
    return 'Decision ladder: accept owner evidence | defer with reason | escalate ownership.';
  }

  String get expediteBoardRowCopyText {
    final receiptCopy = _stableInboxProcessedCopyText;
    if (receiptCopy.isNotEmpty) return receiptCopy;
    if (stableInboxLatestHandoffCopy.isNotEmpty) {
      return _stableInboxHandoffCopyWithLiveControlGate(
        stableInboxLatestHandoffCopy,
      );
    }
    if (stableInboxHandoffCopy.isNotEmpty) {
      return _stableInboxHandoffCopyWithLiveControlGate(stableInboxHandoffCopy);
    }
    if (ownerReadyFirstCopyText.isNotEmpty) {
      return ownerReadyFirstCopyText;
    }
    final lines = <String>[
      'Project Autopilot expedite board row',
      if (expediteTopRank > 0) 'Rank: $expediteTopRank',
      if (expediteTopOwner.isNotEmpty) 'Owner: $expediteTopOwner',
      if (expediteTopType.isNotEmpty) 'Type: $expediteTopType',
      if (expediteTopEvidence.isNotEmpty) 'Evidence: $expediteTopEvidence',
      if (expediteNextAction.isNotEmpty) 'Next action: $expediteNextAction',
      if (prBlockerProofFloorCopyText.isNotEmpty) prBlockerProofFloorCopyText,
      if (prPreflightDetail.isNotEmpty) 'Preflight: $prPreflightDetail',
      if (prDeliveryRouteDetail.isNotEmpty)
        'Delivery route: $prDeliveryRouteDetail',
      if (prRecoveryContractDetail.isNotEmpty)
        'Recovery contract: $prRecoveryContractDetail',
      if (expediteOwnerActionDetail.isNotEmpty)
        'Owner action: $expediteOwnerActionDetail',
      if (expeditePrPostureDetail.isNotEmpty)
        'Posture: $expeditePrPostureDetail',
      if (expediteEvidenceRequiredDetail.isNotEmpty)
        'Evidence requirement: $expediteEvidenceRequiredDetail',
      if (expediteHeartbeatLagDetail.isNotEmpty)
        'Heartbeat: $expediteHeartbeatLagDetail',
      if (expediteHeartbeatInboxRouteDetail.isNotEmpty)
        'Heartbeat inbox route: $expediteHeartbeatInboxRouteDetail',
      if (expeditePendingRequestDetail.isNotEmpty)
        'Process request lane: $expeditePendingRequestDetail',
      if (expediteHeartbeatRepairDetail.isNotEmpty)
        'Heartbeat repair lane: $expediteHeartbeatRepairDetail',
      if (stableInboxCompletionContractDetail.isNotEmpty)
        stableInboxCompletionContractDetail,
      if (stableInboxRequestPath.isNotEmpty)
        'Stable inbox request: $stableInboxRequestPath',
      if (stableInboxRequestSha256.isNotEmpty)
        'Stable inbox SHA256: $stableInboxRequestSha256',
      if (stableInboxRequestPreview.isNotEmpty)
        'Stable inbox preview: $stableInboxRequestPreview',
      if (stableInboxControlPlaneGateDetail.isNotEmpty)
        'Control-plane proof gate: $stableInboxControlPlaneGateDetail',
      if (stableInboxLiveControlGateDetail.isNotEmpty)
        'Live-control gate: $stableInboxLiveControlGateDetail',
      if (stableInboxClassificationDetail.isNotEmpty)
        'Classification boundary: $stableInboxClassificationDetail',
      if (expediteSafeDecisionMenu.isNotEmpty) expediteSafeDecisionMenu,
      if (expediteBoardPath.isNotEmpty) 'Report: $expediteBoardPath',
      if (expediteBoardOpenPath.isNotEmpty) 'Open path: $expediteBoardOpenPath',
      if (stableInboxRequestOpenPath.isNotEmpty)
        'Request open path: $stableInboxRequestOpenPath',
    ];
    return lines.length > 1 ? lines.join('\n') : '';
  }

  String _stableInboxHandoffCopyWithLiveControlGate(String copy) {
    if (!stableInboxHasLiveControlGate &&
        !stableInboxHasControlPlaneGate &&
        !hasHeartbeatInboxRoute) {
      return copy;
    }
    final trimmed = copy.trimRight();
    if (trimmed.isEmpty) return copy;
    final lines = <String>[trimmed];
    final controlPlaneDetail = stableInboxControlPlaneGateDetail;
    final controlPlaneLine = 'Control-plane proof gate: $controlPlaneDetail';
    if (controlPlaneDetail.isNotEmpty && !trimmed.contains(controlPlaneLine)) {
      lines.add(controlPlaneLine);
    }
    final heartbeatRouteDetail = expediteHeartbeatInboxRouteDetail;
    final heartbeatRouteLine = 'Heartbeat inbox route: $heartbeatRouteDetail';
    if (heartbeatRouteDetail.isNotEmpty &&
        !trimmed.contains(heartbeatRouteLine)) {
      lines.add(heartbeatRouteLine);
    }
    final gateDetail = stableInboxLiveControlGateDetail;
    final gateLine = 'Live-control gate: $gateDetail';
    if (gateDetail.isNotEmpty && !trimmed.contains(gateLine)) {
      lines.add(gateLine);
    }
    final decisionMenu = expediteSafeDecisionMenu;
    if (decisionMenu.isNotEmpty && !trimmed.contains(decisionMenu)) {
      lines.add(decisionMenu);
    }
    return lines.join('\n');
  }

  String get _stableInboxProcessedCopyText {
    if (!stableInboxProcessed) return '';
    final requestPath =
        stableInboxLatestRequestPath.isNotEmpty && _useLatestProcessedReceipt
            ? stableInboxLatestRequestPath
            : stableInboxRequestPath.isNotEmpty
                ? stableInboxRequestPath
                : stableInboxLatestRequestPath;
    final lines = <String>[
      'Project Autopilot processed stable-inbox receipt',
      if (expediteTopRank > 0) 'Board rank: $expediteTopRank',
      if (requestPath.isNotEmpty) 'Request: $requestPath',
      if (stableInboxProcessedUtc.isNotEmpty)
        'Processed UTC: $stableInboxProcessedUtc',
      if (stableInboxProcessedStatus.isNotEmpty)
        'Status: $stableInboxProcessedStatus',
      if (stableInboxProcessedResult.isNotEmpty)
        'Result: $stableInboxProcessedResult',
      if (stableInboxProcessedReportPath.isNotEmpty)
        'Report: $stableInboxProcessedReportPath',
      if (stableInboxProcessedSummary.isNotEmpty)
        'Summary: $stableInboxProcessedSummary',
      if (stableInboxFreshnessDetail.isNotEmpty)
        'Freshness: $stableInboxFreshnessDetail',
      'Safe action: review the processed receipt and refresh stale board guidance before assigning more work for this request.',
      'Blocked action: do not reprocess, mutate mailbox ledgers, edit finalized reports, touch source/runtime/DB/broker/git/PR state, commit, push, release, or live behavior from this copied receipt alone.',
    ];
    return lines.length > 1 ? lines.join('\n') : '';
  }

  String get flowContainmentChipLabel => flowQuarantinedTargetCount > 0
      ? '$flowQuarantinedTargetCount containment'
      : '';
  bool get hasFlowQuarantineProofWindow =>
      flowQuarantineProofSatisfied ||
      flowQuarantineProofRemainingMinutes >= 0 ||
      flowQuarantineProofWindowMinutes > 0;
  bool get flowQuarantineProofReady =>
      hasFlowQuarantineProofWindow &&
      (flowQuarantineProofSatisfied ||
          flowQuarantineProofRemainingMinutes == 0);
  bool get flowQuarantineProofWaiting =>
      hasFlowQuarantineProofWindow &&
      !flowQuarantineProofReady &&
      flowQuarantineProofRemainingMinutes > 0;
  String get flowQuarantineProofChipLabel {
    if (!hasFlowQuarantineProofWindow) return '';
    if (flowQuarantineProofReady) {
      return 'proof ready';
    }
    if (flowQuarantineProofRemainingMinutes > 0) {
      return '${flowQuarantineProofRemainingMinutes}m proof left';
    }
    if (flowQuarantineProofWindowMinutes > 0) {
      return '${flowQuarantineProofWindowMinutes}m proof required';
    }
    return '';
  }

  String get flowQuarantineTrustStateChipLabel {
    if (flowQuarantinedTargetCount <= 0) return '';
    if (flowQuarantineProofReady) return 'proof review';
    return 'trust blocked';
  }

  bool get flowQuarantineTrustBlocked =>
      flowQuarantinedTargetCount > 0 && !flowQuarantineProofReady;

  String get flowQuarantineTrustStateDetail {
    if (flowQuarantinedTargetCount <= 0) return '';
    if (flowQuarantineProofReady) {
      return 'Trust state: proof review only; source/runtime trust stays blocked until registry evidence and PM/operator disposition are current.';
    }
    if (flowQuarantineProofWaiting) {
      return 'Trust state: blocked; ${flowQuarantineProofRemainingMinutes}m quiet proof window remains before review can start.';
    }
    return 'Trust state: blocked; containment proof and PM/operator disposition are still required.';
  }

  String get flowQuarantineProofDetail {
    if (!hasFlowQuarantineProofWindow) return '';
    final target = flowQuarantineThreadId.isEmpty
        ? ''
        : flowQuarantinedTargetCount > 1
            ? ' for top target ${AutopilotAgentBenchActivityPresenter._shortId(flowQuarantineThreadId)} of $flowQuarantinedTargetCount active'
            : ' target ${AutopilotAgentBenchActivityPresenter._shortId(flowQuarantineThreadId)}';
    final status = flowQuarantineStatus.isEmpty
        ? ''
        : ' | ${flowQuarantineStatus.replaceAll('_', ' ')}';
    final operatorLabel = flowQuarantineOperatorLabel.isEmpty
        ? ''
        : ' | $flowQuarantineOperatorLabel';
    final nextCheck = flowQuarantineProofNextCheckUtc.isEmpty
        ? ''
        : ' | next check ${AutopilotAgentBenchActivityPresenter._clip(flowQuarantineProofNextCheckUtc, 28)}';
    if (flowQuarantineProofReady) {
      return 'Proof window ready$target$status$operatorLabel; review registry before clearing source/runtime trust.';
    }
    if (flowQuarantineProofRemainingMinutes > 0) {
      return 'Proof window: wait ${flowQuarantineProofRemainingMinutes}m$target$status$operatorLabel before accepting containment proof$nextCheck.';
    }
    return 'Proof window: ${flowQuarantineProofWindowMinutes}m quiet proof required$target$status$operatorLabel.';
  }

  String get flowQuarantineProofFloorDetail {
    if (flowQuarantinedTargetCount <= 0) return '';
    final parts = <String>[
      if (flowQuarantineSessionAgeMinutes >= 0)
        'target wrote ${flowQuarantineSessionAgeMinutes}m ago',
      if (flowQuarantineProofFloorUtc.isNotEmpty)
        'containment must be later than ${AutopilotAgentBenchActivityPresenter._clip(flowQuarantineProofFloorUtc, 32)}',
      if (flowQuarantineTargetLastWriteUtc.isNotEmpty &&
          flowQuarantineTargetLastWriteUtc != flowQuarantineProofFloorUtc)
        'latest write ${AutopilotAgentBenchActivityPresenter._clip(flowQuarantineTargetLastWriteUtc, 32)}',
      if (flowQuarantineActivityState.isNotEmpty)
        flowQuarantineActivityState.replaceAll('_', ' '),
      if (flowQuarantineSessionGoalStatus.isNotEmpty)
        'goal ${flowQuarantineSessionGoalStatus.replaceAll('_', ' ')}',
      if (flowQuarantineSessionPath.isNotEmpty)
        'session ${AutopilotAgentBenchActivityPresenter._clip(flowQuarantineSessionPath, 72)}',
    ];
    if (parts.isEmpty) return '';
    final label = flowQuarantineProofReady ? 'Proof floor met' : 'Proof floor';
    return AutopilotAgentBenchActivityPresenter._clip(
      '$label: ${parts.join(' | ')}',
      220,
    );
  }

  String get flowQuarantineClearanceChecklistDetail {
    if (flowQuarantinedTargetCount <= 0) return '';
    final containmentStep = flowQuarantinedTargetCount > 1
        ? 'confirm all $flowQuarantinedTargetCount targets stopped/contained'
        : 'confirm target stopped/contained';
    final steps = <String>[
      if (flowQuarantineProofWaiting)
        'wait ${flowQuarantineProofRemainingMinutes}m quiet window'
      else if (flowQuarantineProofReady)
        'review registry evidence'
      else if (flowQuarantineProofWindowMinutes > 0)
        'observe ${flowQuarantineProofWindowMinutes}m quiet window',
      if (flowQuarantineProofFloorUtc.isNotEmpty)
        'proof later than ${AutopilotAgentBenchActivityPresenter._clip(flowQuarantineProofFloorUtc, 32)}',
      containmentStep,
      'confirm no watcher/tool session remains',
      'PM/operator disposition current before trust',
    ];
    return AutopilotAgentBenchActivityPresenter._clip(
      'Clearance checklist: ${steps.join(' | ')}',
      240,
    );
  }

  String get flowQuarantineClearanceChecklistCopyText {
    if (flowQuarantinedTargetCount <= 0) return '';
    final target = flowQuarantineThreadId.isEmpty
        ? flowQuarantinedTargetCount > 1
            ? 'all $flowQuarantinedTargetCount quarantined targets'
            : 'the quarantined target'
        : flowQuarantinedTargetCount > 1
            ? 'all $flowQuarantinedTargetCount quarantined targets, including top target thread $flowQuarantineThreadId'
            : 'target thread $flowQuarantineThreadId';
    final targetVerb = flowQuarantinedTargetCount > 1 ? 'are' : 'is';
    final lines = <String>[
      'Clearance checklist:',
      if (flowQuarantineProofWaiting)
        '1. Do not clear quarantine yet; wait ${flowQuarantineProofRemainingMinutes}m of quiet proof window.'
      else if (flowQuarantineProofReady)
        '1. Review registry evidence before clearing source/runtime trust.'
      else if (flowQuarantineProofWindowMinutes > 0)
        '1. Observe the ${flowQuarantineProofWindowMinutes}m quiet proof window before accepting proof.'
      else
        '1. Review quarantine evidence before accepting proof.',
      '2. Confirm $target $targetVerb stopped or contained in the control plane.',
      if (flowQuarantineProofFloorUtc.isNotEmpty)
        '3. Confirm proof names containment later than $flowQuarantineProofFloorUtc.'
      else
        '3. Confirm proof names a current containment timestamp.',
      '4. Confirm no watcher, tool session, or target-session write remains through the quiet window.',
      '5. Keep source/runtime trust blocked until PM/operator disposition and registry evidence are current.',
    ];
    return lines.join('\n');
  }

  String get flowQuarantineRequirementDetail {
    if (flowQuarantineRequiredProof.isEmpty) return '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Required proof: $flowQuarantineRequiredProof',
      220,
    );
  }

  String get flowQuarantineGuidanceDetail {
    if (flowQuarantineOperatorGuidance.isEmpty) return '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Operator guidance: $flowQuarantineOperatorGuidance',
      180,
    );
  }

  String get flowQuarantineProofRefreshDetail {
    if (!flowQuarantineProofWaiting ||
        flowQuarantineProofNextCheckUtc.isEmpty) {
      return '';
    }
    return 'Auto-refresh proof at ${AutopilotAgentBenchActivityPresenter._clip(flowQuarantineProofNextCheckUtc, 28)}.';
  }

  String get flowPausedAutomationChipLabel => flowPausedAutomationCount > 0
      ? '$flowPausedAutomationCount paused automation'
      : '';
  String get flowPausedAutomationDetail {
    if (flowPausedAutomationCount <= 0) return '';
    final parts = <String>[
      if (flowPausedAutomationName.isNotEmpty) flowPausedAutomationName,
      if (flowPausedAutomationId.isNotEmpty &&
          flowPausedAutomationId != flowPausedAutomationName)
        flowPausedAutomationId,
      if (flowPausedAutomationStatus.isNotEmpty)
        flowPausedAutomationStatus.replaceAll('_', ' '),
      if (flowPausedAutomationThreadId.isNotEmpty)
        'target ${AutopilotAgentBenchActivityPresenter._shortId(flowPausedAutomationThreadId)}',
      if (flowPausedAutomationSessionAgeMinutes >= 0)
        'wrote ${flowPausedAutomationSessionAgeMinutes}m ago',
      if (flowPausedAutomationThresholdMinutes > 0)
        'threshold ${flowPausedAutomationThresholdMinutes}m',
      if (flowPausedAutomationGoalStatus.isNotEmpty)
        'goal ${flowPausedAutomationGoalStatus.replaceAll('_', ' ')}',
      if (flowPausedAutomationCoveredByQuarantine) 'already quarantined',
    ];
    if (parts.isEmpty) return '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Paused automation: ${parts.join(' | ')}',
      180,
    );
  }

  String get flowPausedAutomationGuidanceDetail {
    if (flowPausedAutomationGuidance.isEmpty) return '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Paused automation guidance: $flowPausedAutomationGuidance',
      180,
    );
  }

  int get flowWorktreeIssueCount {
    if (flowWorktreeHygieneCount > 0) return flowWorktreeHygieneCount;
    return flowDirtyWorktreeCount +
        flowDetachedWorktreeCount +
        flowWorktreeMergeConflictCount;
  }

  String get flowWorktreeChipLabel {
    if (flowWorktreeMergeConflictCount > 0) {
      return '$flowWorktreeMergeConflictCount merge conflict${flowWorktreeMergeConflictCount == 1 ? '' : 's'}';
    }
    if (flowWorktreeChangeCount > 0) {
      return '$flowWorktreeChangeCount worktree change${flowWorktreeChangeCount == 1 ? '' : 's'}';
    }
    final issueCount = flowWorktreeIssueCount;
    if (issueCount > 0) {
      return '$issueCount worktree risk${issueCount == 1 ? '' : 's'}';
    }
    return '';
  }

  bool get hasFlowSourceIsolation =>
      flowSourceIsolationBlocked ||
      flowRepositoryRootDirtyCount > 0 ||
      flowRepositoryRootChangeCount > 0 ||
      flowRepositoryRootMergeConflictCount > 0;
  String get flowSourceIsolationChipLabel =>
      hasFlowSourceIsolation ? 'source blocked' : '';
  String get _flowSourceIsolationChangeLabel {
    if (flowRepositoryRootMergeConflictCount > 0) {
      return '$flowRepositoryRootMergeConflictCount shared-root merge conflict${flowRepositoryRootMergeConflictCount == 1 ? '' : 's'}';
    }
    if (flowRepositoryRootChangeCount > 0) {
      return '$flowRepositoryRootChangeCount shared-root change${flowRepositoryRootChangeCount == 1 ? '' : 's'}';
    }
    if (flowRepositoryRootDirtyCount > 0) {
      return '$flowRepositoryRootDirtyCount dirty root checkout${flowRepositoryRootDirtyCount == 1 ? '' : 's'}';
    }
    return 'shared-root source dirty';
  }

  String get flowSourceTrustChipLabel {
    if (!hasFlowSourceIsolation) return '';
    if (flowRepositoryRootMergeConflictCount > 0) {
      return '$flowRepositoryRootMergeConflictCount root conflict${flowRepositoryRootMergeConflictCount == 1 ? '' : 's'}';
    }
    if (flowRepositoryRootChangeCount > 0) {
      return '$flowRepositoryRootChangeCount root change${flowRepositoryRootChangeCount == 1 ? '' : 's'}';
    }
    if (flowRepositoryRootDirtyCount > 0) {
      return '$flowRepositoryRootDirtyCount dirty root${flowRepositoryRootDirtyCount == 1 ? '' : 's'}';
    }
    return 'source trust blocked';
  }

  String get flowSourceTrustEvidenceLabel =>
      flowRepositoryRootMergeConflictCount > 0
          ? 'needs conflict-free Worktree/branch/HEAD evidence'
          : hasFlowSourceIsolation
              ? 'needs Worktree/branch/HEAD evidence'
              : '';

  String get flowSourceTrustDetail {
    if (!hasFlowSourceIsolation) return '';
    final branch = flowRepositoryRootBranch.isEmpty
        ? ''
        : 'branch ${AutopilotAgentBenchActivityPresenter._clip(flowRepositoryRootBranch, 72)}';
    return [
      'Source trust gate: $_flowSourceIsolationChangeLabel',
      if (branch.isNotEmpty) branch,
      flowSourceTrustEvidenceLabel,
    ].where((part) => part.trim().isNotEmpty).join(' | ');
  }

  String get flowSourceIsolationLabel {
    if (!hasFlowSourceIsolation) return '';
    final branch = flowRepositoryRootBranch.isEmpty
        ? ''
        : ' | ${AutopilotAgentBenchActivityPresenter._clip(flowRepositoryRootBranch, 72)}';
    return 'Source blocked: $_flowSourceIsolationChangeLabel$branch';
  }

  String get deliveryFocusChipLabel {
    if (flowQuarantinedTargetCount > 0 ||
        flowControlPlaneBlockerCount > 0 ||
        expediteControlPlaneCount > 0) {
      return 'delivery: containment';
    }
    if (stableInboxHasControlPlaneGate) return 'delivery: PM proof';
    if (stableInboxHasLiveControlGate) return 'delivery: live-control';
    if (prPreflightBlocked) return 'delivery: PR blocked';
    if (hasFlowSourceIsolation) return 'delivery: source trust';
    if (hasPrPreflightPressure) return 'delivery: PR review';
    return '';
  }

  bool get hasDeliveryFocus => deliveryFocusChipLabel.isNotEmpty;

  bool get deliveryFocusCritical =>
      deliveryFocusChipLabel == 'delivery: containment' ||
      deliveryFocusChipLabel == 'delivery: PM proof' ||
      deliveryFocusChipLabel == 'delivery: live-control' ||
      deliveryFocusChipLabel == 'delivery: PR blocked' ||
      deliveryFocusChipLabel == 'delivery: source trust';

  int get deliveryFocusPriorityScore {
    switch (deliveryFocusChipLabel) {
      case 'delivery: containment':
        return 1500;
      case 'delivery: PM proof':
        return 1300;
      case 'delivery: live-control':
        return 1100;
      case 'delivery: source trust':
        return 950;
      case 'delivery: PR blocked':
        return 850;
      case 'delivery: PR review':
        return 300;
      default:
        return 0;
    }
  }

  String get deliveryFocusLabel {
    final chip = deliveryFocusChipLabel;
    if (chip.isEmpty) return '';
    return AutopilotAgentBenchActivityPresenter._clip(
      'Delivery focus: ${chip.replaceFirst('delivery: ', '')}',
      96,
    );
  }

  String get deliveryFocusDetail {
    final label = deliveryFocusChipLabel;
    if (label.isEmpty) return '';
    if (label == 'delivery: containment') {
      final parts = <String>[
        if (flowQuarantineTrustStateDetail.isNotEmpty)
          flowQuarantineTrustStateDetail,
        if (flowQuarantineClearanceChecklistDetail.isNotEmpty)
          flowQuarantineClearanceChecklistDetail,
        if (stableInboxControlPlaneGateDetail.isNotEmpty)
          stableInboxControlPlaneGateDetail,
        if (expediteTopEvidence.isNotEmpty)
          AutopilotAgentBenchActivityPresenter._clip(expediteTopEvidence, 110),
        if (expediteNextAction.isNotEmpty) expediteNextAction,
      ];
      return AutopilotAgentBenchActivityPresenter._clip(
        'Delivery focus: contain or prove the control plane before source/runtime trust, PR repair, release, or routine work. ${parts.join(' | ')}',
        300,
      );
    }
    if (label == 'delivery: PM proof') {
      return AutopilotAgentBenchActivityPresenter._clip(
        'Delivery focus: process the PM control-plane proof request before PR/source trust work. $stableInboxControlPlaneGateDetail',
        280,
      );
    }
    if (label == 'delivery: live-control') {
      return AutopilotAgentBenchActivityPresenter._clip(
        'Delivery focus: record PM/operator live-control disposition before runtime, broker/API, release, or live behavior changes. $stableInboxLiveControlGateDetail',
        280,
      );
    }
    if (label == 'delivery: PR blocked') {
      final parts = <String>[
        if (prDeliveryRouteDetail.isNotEmpty) prDeliveryRouteDetail,
        if (prPreflightDetail.isNotEmpty) prPreflightDetail,
        if (expediteEvidenceRequiredDetail.isNotEmpty)
          expediteEvidenceRequiredDetail,
        if (expediteSafeDecisionDetail.isNotEmpty) expediteSafeDecisionDetail,
      ];
      return AutopilotAgentBenchActivityPresenter._clip(
        'Delivery focus: keep PR lane blocked until current-head checks, owner evidence, and PM/operator disposition are clear. ${parts.join(' | ')}',
        300,
      );
    }
    if (label == 'delivery: source trust') {
      return AutopilotAgentBenchActivityPresenter._clip(
        'Delivery focus: clear source trust before PR/release work. $flowSourceTrustDetail',
        260,
      );
    }
    return AutopilotAgentBenchActivityPresenter._clip(
      'Delivery focus: review PR readiness before promotion. $prPreflightDetail',
      260,
    );
  }

  bool get deliveryFocusRoutesToFlow {
    final label = deliveryFocusChipLabel;
    if (label == 'delivery: containment') {
      return hasFlowActionTarget;
    }
    if (label == 'delivery: source trust') {
      return hasFlowActionTarget;
    }
    return false;
  }

  bool get deliveryFocusRoutesToPrBlockerPacket =>
      !deliveryFocusRoutesToFlow &&
      deliveryFocusChipLabel == 'delivery: PR blocked' &&
      hasKpiPrBlockerDecisionPacket;

  bool get deliveryFocusRoutesToExpedite =>
      !deliveryFocusRoutesToFlow &&
      !deliveryFocusRoutesToPrBlockerPacket &&
      hasExpediteOpenTarget;

  bool get hasDeliveryFocusTarget =>
      hasDeliveryFocus &&
      (deliveryFocusRoutesToFlow ||
          deliveryFocusRoutesToPrBlockerPacket ||
          deliveryFocusRoutesToExpedite);

  String get deliveryFocusPrimaryOpenPath => deliveryFocusRoutesToFlow
      ? flowPrimaryOpenPath
      : deliveryFocusRoutesToPrBlockerPacket
          ? ''
          : deliveryFocusRoutesToExpedite
              ? expeditePrimaryOpenPath
              : '';

  String get deliveryFocusActionLabel {
    switch (deliveryFocusChipLabel) {
      case 'delivery: containment':
        return deliveryFocusRoutesToFlow
            ? flowSafetyActionLabel
            : 'Open/copy containment';
      case 'delivery: PM proof':
        return 'Open/copy PM proof';
      case 'delivery: live-control':
        return 'Open/copy live-control';
      case 'delivery: PR blocked':
        return hasKpiPrBlockerDecisionPacket
            ? kpiPrBlockerDecisionPacketLabel
            : hasPrRecoveryContract
                ? 'Open/copy recovery plan'
                : 'Open/copy PR blocker';
      case 'delivery: source trust':
        return deliveryFocusRoutesToFlow
            ? flowOpenActionLabel
            : 'Open/copy source trust';
      case 'delivery: PR review':
        return 'Open/copy PR review';
      default:
        return '';
    }
  }

  String get flowCaptainChipLabel => flowCaptainExpiredCount > 0
      ? '$flowCaptainExpiredCount expired captain'
      : '';
  String get flowUrgentChipLabel =>
      flowUrgentCount > 0 ? '$flowUrgentCount urgent inbox' : '';
  String get flowStableChipLabel => flowStablePendingCount > 0
      ? '$flowStablePendingCount stable inbox'
      : flowPendingCount > 0
          ? '$flowPendingCount fresh inbox'
          : '';
  String get flowIssueChipLabel {
    if (flowShapeInvalidCount > 0) {
      return '$flowShapeInvalidCount malformed';
    }
    if (flowReviewMismatchCount > 0) {
      return '$flowReviewMismatchCount head mismatch';
    }
    if (flowActiveLockStarvationCount > 0) {
      return '$flowActiveLockStarvationCount lock warning';
    }
    if (flowStaleTempPublishCount > 0) {
      return '$flowStaleTempPublishCount stale temp';
    }
    return '';
  }

  int get flowCoordinationDriftCount =>
      flowShapeInvalidCount +
      flowMailboxMalformedCorrectionCount +
      flowReviewMismatchCount +
      flowActiveLockStarvationCount +
      flowStaleTempPublishCount;

  bool get hasFlowCoordinationDrift =>
      flowCoordinationDriftCount > 0 ||
      flowSupersededShapeInvalidCount > 0 ||
      (flowPausedAutomationCount > 0 &&
          goalHealthStatus.toUpperCase() == 'PAUSED' &&
          goalHealthGoalStatus.toLowerCase() == 'active');

  String get flowCoordinationDriftChipLabel {
    if (!hasFlowCoordinationDrift) return '';
    if (flowMailboxMalformedCorrectionCount > 0) {
      return '$flowMailboxMalformedCorrectionCount builder fix${flowMailboxMalformedCorrectionCount == 1 ? '' : 'es'}';
    }
    if (flowSupersededShapeInvalidCount > 0 &&
        flowCoordinationDriftCount == 0) {
      return '$flowSupersededShapeInvalidCount corrected malformed';
    }
    return 'coordination drift';
  }

  String get flowCoordinationDriftLabel {
    if (!hasFlowCoordinationDrift) return '';
    final parts = <String>[
      if (flowMailboxMalformedCorrectionCount > 0)
        '$flowMailboxMalformedCorrectionCount builder/linter correction${flowMailboxMalformedCorrectionCount == 1 ? '' : 's'}',
      if (flowShapeInvalidCount > 0)
        '$flowShapeInvalidCount active malformed request${flowShapeInvalidCount == 1 ? '' : 's'}',
      if (flowSupersededShapeInvalidCount > 0)
        '$flowSupersededShapeInvalidCount superseded malformed request${flowSupersededShapeInvalidCount == 1 ? '' : 's'}',
      if (flowReviewMismatchCount > 0)
        '$flowReviewMismatchCount review-head mismatch${flowReviewMismatchCount == 1 ? '' : 'es'}',
      if (flowActiveLockStarvationCount > 0)
        '$flowActiveLockStarvationCount lock warning${flowActiveLockStarvationCount == 1 ? '' : 's'}',
      if (flowStaleTempPublishCount > 0)
        '$flowStaleTempPublishCount stale temp publish artifact${flowStaleTempPublishCount == 1 ? '' : 's'}',
    ];
    return AutopilotAgentBenchActivityPresenter._clip(
      'Coordination drift: ${parts.join(', ')}',
      180,
    );
  }

  String get flowCoordinationDriftDetail {
    if (!hasFlowCoordinationDrift) return '';
    final parts = <String>[
      if (flowMailboxMalformedCorrectionCount > 0)
        '$flowMailboxMalformedCorrectionCount builder/linter correction${flowMailboxMalformedCorrectionCount == 1 ? '' : 's'} need mailbox protocol repair',
      if (flowShapeInvalidCount > 0)
        '$flowShapeInvalidCount active malformed mailbox request${flowShapeInvalidCount == 1 ? '' : 's'}',
      if (flowSupersededShapeInvalidCount > 0)
        '$flowSupersededShapeInvalidCount superseded malformed request${flowSupersededShapeInvalidCount == 1 ? '' : 's'} recognized as non-actionable',
      if (flowReviewMismatchCount > 0)
        '$flowReviewMismatchCount review-head mismatch${flowReviewMismatchCount == 1 ? '' : 'es'}',
      if (flowActiveLockStarvationCount > 0)
        '$flowActiveLockStarvationCount active lock warning${flowActiveLockStarvationCount == 1 ? '' : 's'}',
      if (flowStaleTempPublishCount > 0)
        '$flowStaleTempPublishCount stale temp publish artifact${flowStaleTempPublishCount == 1 ? '' : 's'}',
      if (flowPausedAutomationCount > 0)
        '$flowPausedAutomationCount paused automation target${flowPausedAutomationCount == 1 ? '' : 's'}',
      if (hasGoalHealthPressure && goalHealthChipLabel.isNotEmpty)
        goalHealthChipLabel,
      if (hasFlowSourceIsolation) _flowSourceIsolationChangeLabel,
      if (flowNextAction.isNotEmpty) flowNextAction,
    ];
    return AutopilotAgentBenchActivityPresenter._clip(
      'Coordination drift: ${parts.join(' | ')}',
      260,
    );
  }

  bool get hasFlowActionTarget =>
      flowNextActionHandoffCopy.isNotEmpty ||
      flowNextActionOpenPath.isNotEmpty ||
      flowNextActionPath.isNotEmpty;
  String get flowPrimaryOpenPath => flowNextActionOpenPath.isNotEmpty
      ? flowNextActionOpenPath
      : flowNextActionPath;
  String get flowOpenActionLabel => flowNextActionHandoffCopy.isNotEmpty
      ? flowNextActionHandoffLabel.isNotEmpty
          ? flowNextActionHandoffLabel
          : 'Copy flow handoff'
      : 'Open flow action';
  String get flowSafetyActionLabel {
    if (!hasFlowActionTarget) return '';
    if (flowQuarantinedTargetCount > 0 && hasFlowQuarantineProofWindow) {
      if (flowQuarantineProofReady) return 'Review registry';
      if (flowQuarantineProofRemainingMinutes > 0) {
        return 'Wait ${flowQuarantineProofRemainingMinutes}m';
      }
      return 'Wait for proof';
    }
    return flowOpenActionLabel;
  }

  bool get flowSafetyActionEnabled {
    if (flowQuarantinedTargetCount > 0 &&
        hasFlowQuarantineProofWindow &&
        !flowQuarantineProofReady) {
      return false;
    }
    return hasFlowActionTarget;
  }

  bool get shouldPrioritizeFlowSafetyAction =>
      hasFlowActionTarget &&
      (hasFlowSourceIsolation ||
          flowMailboxMalformedCorrectionCount > 0 ||
          flowShapeInvalidCount > 0 ||
          flowControlPlaneBlockerCount > 0 ||
          flowQuarantinedTargetCount > 0 ||
          flowPausedAutomationCount > 0 ||
          flowActiveLockStarvationCount > 0);
  String get flowSafetyPriorityReason {
    if (!shouldPrioritizeFlowSafetyAction) {
      return '';
    }
    if (hasFlowSourceIsolation) {
      final blockedCount = flowRepositoryRootMergeConflictCount > 0
          ? flowRepositoryRootMergeConflictCount
          : flowRepositoryRootChangeCount > 0
              ? flowRepositoryRootChangeCount
              : flowRepositoryRootDirtyCount;
      final blockVerb = blockedCount == 1 ? 'blocks' : 'block';
      return 'Safety first: $_flowSourceIsolationChangeLabel $blockVerb PR work';
    }
    if (flowQuarantinedTargetCount > 0) {
      final targetLabel = flowQuarantinedTargetCount == 1
          ? 'active quarantine'
          : '$flowQuarantinedTargetCount active quarantines';
      return 'Safety first: $targetLabel blocks PR work';
    }
    if (flowPausedAutomationCount > 0) {
      final automationLabel = flowPausedAutomationCount == 1
          ? 'paused automation'
          : '$flowPausedAutomationCount paused automations';
      return 'Safety first: $automationLabel blocks PR work';
    }
    if (flowControlPlaneBlockerCount > 0) {
      final blockerLabel = flowControlPlaneBlockerCount == 1
          ? 'control-plane blocker'
          : '$flowControlPlaneBlockerCount control-plane blockers';
      return 'Safety first: $blockerLabel before PR work';
    }
    if (flowMailboxMalformedCorrectionCount > 0) {
      final fixLabel = flowMailboxMalformedCorrectionCount == 1
          ? 'mailbox builder fix'
          : '$flowMailboxMalformedCorrectionCount mailbox builder fixes';
      return 'Safety first: $fixLabel before PR work';
    }
    if (flowShapeInvalidCount > 0) {
      final malformedLabel = flowShapeInvalidCount == 1
          ? 'malformed mailbox request'
          : '$flowShapeInvalidCount malformed mailbox requests';
      return 'Safety first: $malformedLabel before PR work';
    }
    if (flowActiveLockStarvationCount > 0) {
      final lockLabel = flowActiveLockStarvationCount == 1
          ? 'active lock warning'
          : '$flowActiveLockStarvationCount active lock warnings';
      return 'Safety first: $lockLabel before PR work';
    }
    return '';
  }

  List<String> get benchActionStackLabels {
    final labels = <String>[];
    void addLabel(String label) {
      final clean = label.trim();
      if (clean.isEmpty || labels.contains(clean)) return;
      labels.add(clean);
    }

    if (shouldPrioritizeFlowSafetyAction) {
      addLabel(flowSafetyActionLabel);
    }
    if (hasExpediteOpenTarget) {
      addLabel(expediteOpenActionLabel);
    }
    if (hasScheduledQualityTarget) {
      addLabel(scheduledQualityOpenActionLabel);
    }
    if (hasBenchmarkEvidenceHandoff) {
      addLabel(benchmarkEvidenceOpenActionLabel);
    }
    if (hasKpiPrOwnerLaneAction) {
      addLabel(kpiPrOwnerLaneActionLabel);
    }
    if (hasBlockedRecovery) {
      addLabel(blockedRecoveryOpenActionLabel);
    }
    if (hasStaleActiveRun) {
      addLabel(staleActiveOpenActionLabel);
    }
    if (hasFlowActionTarget && !shouldPrioritizeFlowSafetyAction) {
      addLabel(flowOpenActionLabel);
    }
    return labels;
  }

  String get benchActionStackLabel {
    final labels = benchActionStackLabels;
    if (labels.length < 2) {
      return '';
    }
    final visibleLabels = labels.take(3).toList(growable: false);
    final parts = <String>[
      for (var index = 0; index < visibleLabels.length; index += 1)
        '${index == 0 ? 'Next' : index == 1 ? 'Then' : 'Later'}: ${visibleLabels[index]}',
      if (labels.length > visibleLabels.length)
        '+${labels.length - visibleLabels.length} more',
    ];
    return parts.join(' | ');
  }

  String get flowActionCopyText {
    if (flowNextActionHandoffCopy.isNotEmpty) {
      return flowNextActionHandoffCopy;
    }
    final lines = <String>[
      'Project Autopilot agent-flow action',
      if (flowSignal.isNotEmpty) 'Signal: $flowSignal',
      if (flowNextAction.isNotEmpty) 'Next action: $flowNextAction',
      if (flowQuarantineThreadId.isNotEmpty)
        'Target thread: $flowQuarantineThreadId',
      if (flowQuarantineStatus.isNotEmpty)
        'Quarantine status: $flowQuarantineStatus',
      if (flowQuarantineTrustStateDetail.isNotEmpty)
        flowQuarantineTrustStateDetail,
      if (flowQuarantineRequiredProof.isNotEmpty)
        'Required proof: $flowQuarantineRequiredProof',
      if (flowQuarantineClearanceChecklistCopyText.isNotEmpty)
        flowQuarantineClearanceChecklistCopyText,
      if (flowQuarantineProofFloorUtc.isNotEmpty)
        'Proof floor: containment must be later than $flowQuarantineProofFloorUtc',
      if (flowQuarantineTargetLastWriteUtc.isNotEmpty)
        'Target last write: $flowQuarantineTargetLastWriteUtc',
      if (flowQuarantineSessionAgeMinutes >= 0)
        'Target write age: ${flowQuarantineSessionAgeMinutes}m',
      if (flowQuarantineActivityState.isNotEmpty)
        'Activity state: $flowQuarantineActivityState',
      if (flowQuarantineSessionPath.isNotEmpty)
        'Session path: $flowQuarantineSessionPath',
      if (flowQuarantineOperatorGuidance.isNotEmpty)
        'Operator guidance: $flowQuarantineOperatorGuidance',
      if (flowPausedAutomationDetail.isNotEmpty) flowPausedAutomationDetail,
      if (flowPausedAutomationGuidance.isNotEmpty)
        'Paused automation guidance: $flowPausedAutomationGuidance',
      if (flowNextActionPath.isNotEmpty) 'Path: $flowNextActionPath',
      if (flowNextActionOpenPath.isNotEmpty)
        'Open path: $flowNextActionOpenPath',
      'Safety boundary: this copied note is review-only; it does not authorize source, runtime, database, broker/API, git, PR, release, or live-behavior changes.',
    ];
    return lines.length > 1 ? lines.join('\n') : '';
  }

  String get activeRunStalePermissionBoundaryLabel =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryLabel(
        activeRunStaleHandoffCopy,
      );
  String get activeRunStalePermissionBoundaryDetail =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryDetail(
        activeRunStaleHandoffCopy,
      );
  String get blockedRunRecoveryPermissionBoundaryLabel =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryLabel(
        blockedRunRecoveryHandoffCopy,
      );
  String get blockedRunRecoveryPermissionBoundaryDetail =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryDetail(
        blockedRunRecoveryHandoffCopy,
      );
  String get flowActionPermissionBoundaryLabel =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryLabel(
        flowNextActionHandoffCopy,
      );
  String get flowActionPermissionBoundaryDetail =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryDetail(
        flowNextActionHandoffCopy,
      );
  String get goalHealthPermissionBoundaryLabel =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryLabel(
        goalHealthHandoffCopy,
      );
  String get goalHealthPermissionBoundaryDetail =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryDetail(
        goalHealthHandoffCopy,
      );
  String get _stableInboxBoundarySource =>
      _stableInboxProcessedCopyText.isNotEmpty
          ? _stableInboxProcessedCopyText
          : stableInboxLatestHandoffCopy.isNotEmpty
              ? stableInboxLatestHandoffCopy
              : stableInboxHandoffCopy;
  String get stableInboxPermissionBoundaryLabel =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryLabel(
        _stableInboxBoundarySource,
      );
  String get stableInboxPermissionBoundaryDetail =>
      AutopilotAgentBenchActivityPresenter._permissionBoundaryDetail(
        _stableInboxBoundarySource,
      );

  List<String> get permissionBoundaryLabels {
    final labels = <String>[];
    void add(String label) {
      final clean = label.trim();
      if (clean.isEmpty || labels.contains(clean)) return;
      labels.add(clean);
    }

    add(flowActionPermissionBoundaryLabel);
    add(goalHealthPermissionBoundaryLabel);
    add(blockedRunRecoveryPermissionBoundaryLabel);
    add(activeRunStalePermissionBoundaryLabel);
    add(stableInboxPermissionBoundaryLabel);
    return labels;
  }

  String get permissionBoundaryChipLabel {
    final labels = permissionBoundaryLabels;
    if (labels.isEmpty) return '';
    if (labels.length == 1) return labels.first;
    return '${labels.first} +${labels.length - 1}';
  }

  String get permissionBoundaryStackLabel {
    final parts = <String>[];
    void add(String action, String boundary) {
      final cleanAction = action.trim();
      final cleanBoundary = boundary.trim();
      if (cleanAction.isEmpty || cleanBoundary.isEmpty) return;
      final label = '$cleanAction -> $cleanBoundary';
      if (!parts.contains(label)) parts.add(label);
    }

    add(flowOpenActionLabel, flowActionPermissionBoundaryLabel);
    add(goalHealthOpenActionLabel, goalHealthPermissionBoundaryLabel);
    add(benchmarkEvidenceOpenActionLabel,
        benchmarkEvidencePermissionBoundaryLabel);
    add(blockedRecoveryOpenActionLabel,
        blockedRunRecoveryPermissionBoundaryLabel);
    add(staleActiveOpenActionLabel, activeRunStalePermissionBoundaryLabel);
    add(expediteOpenActionLabel, stableInboxPermissionBoundaryLabel);
    if (parts.isEmpty) return '';
    final visible = parts.take(3).join(' | ');
    final suffix = parts.length > 3 ? ' | +${parts.length - 3} more' : '';
    return 'Boundary: $visible$suffix';
  }

  List<String> get permissionBoundarySearchTerms => [
        activeRunStalePermissionBoundaryLabel,
        activeRunStalePermissionBoundaryDetail,
        blockedRunRecoveryPermissionBoundaryLabel,
        blockedRunRecoveryPermissionBoundaryDetail,
        flowActionPermissionBoundaryLabel,
        flowActionPermissionBoundaryDetail,
        goalHealthPermissionBoundaryLabel,
        goalHealthPermissionBoundaryDetail,
        benchmarkEvidencePermissionBoundaryLabel,
        benchmarkEvidencePermissionBoundaryDetail,
        stableInboxPermissionBoundaryLabel,
        stableInboxPermissionBoundaryDetail,
        stableInboxControlPlaneChipLabel,
        stableInboxControlPlaneGateDetail,
        stableInboxLiveControlGateChipLabel,
        stableInboxLiveControlGateDetail,
      ].where((term) => term.isNotEmpty).toList(growable: false);

  bool get hasOperatorInput =>
      pendingQuestionCount > 0 ||
      operatingNeedsInput ||
      statusBlocked ||
      expediteControlPlaneCount > 0 ||
      expediteStableInboxRequestCount > 0 ||
      expediteOpenPrBlockerCount > 0 ||
      hasOwnerReadyFirstPressure ||
      hasPrPreflightPressure ||
      hasBlockedRecovery ||
      hasStaleActiveRun ||
      hasGoalHealthPressure ||
      hasScheduledQualityPressure ||
      hasKpiGeneratedStateDrift ||
      flowControlPlaneBlockerCount > 0 ||
      hasFlowSourceIsolation ||
      flowWorktreeIssueCount > 0 ||
      hasPrPublicationReceiptPressure ||
      flowWorktreeChangeCount > 0 ||
      flowCaptainExpiredCount > 0 ||
      flowUrgentCount > 0 ||
      hasFlowCoordinationDrift;
  bool get hasKpiPressure =>
      kpiBlockedPrCount > 0 ||
      kpiReadyCandidateCount > 0 ||
      kpiTempArtifactCount > 0 ||
      hasKpiGeneratedStateDrift ||
      kpiSignal.isNotEmpty ||
      kpiScoreLabel.isNotEmpty;
  bool get hasExpeditePressure =>
      expediteOpenPrBlockerCount > 0 ||
      expediteReadyCandidateCount > 0 ||
      expediteStableInboxRequestCount > 0 ||
      expediteControlPlaneCount > 0 ||
      hasOwnerReadyFirstPressure ||
      expediteTopRank > 0 ||
      expediteSignal.isNotEmpty;
  int get boardPriorityRank => expediteTopRank > 0 ? expediteTopRank : 0;
  bool get hasFlowPressure =>
      flowPendingCount > 0 ||
      flowStablePendingCount > 0 ||
      flowUrgentCount > 0 ||
      flowControlPlaneBlockerCount > 0 ||
      flowQuarantinedTargetCount > 0 ||
      flowPausedAutomationCount > 0 ||
      hasFlowSourceIsolation ||
      flowWorktreeIssueCount > 0 ||
      flowWorktreeChangeCount > 0 ||
      flowCaptainExpiredCount > 0 ||
      flowShapeInvalidCount > 0 ||
      flowMailboxMalformedCorrectionCount > 0 ||
      flowSupersededShapeInvalidCount > 0 ||
      flowReviewMismatchCount > 0 ||
      flowActiveLockStarvationCount > 0 ||
      flowStaleTempPublishCount > 0 ||
      flowSignal.isNotEmpty;
  bool get hasBenchAttention =>
      hasOperatorInput ||
      hasKpiPressure ||
      hasExpeditePressure ||
      hasFlowPressure ||
      hasBenchmarkPromotionScopeWarning ||
      activeRunCount > 0 ||
      queuedRunCount > 0;
  bool get hasBenchSafety =>
      expediteControlPlaneCount > 0 ||
      goalHealthCritical ||
      flowMailboxMalformedCorrectionCount > 0 ||
      flowControlPlaneBlockerCount > 0 ||
      flowQuarantinedTargetCount > 0 ||
      flowPausedAutomationCount > 0 ||
      flowActiveLockStarvationCount > 0 ||
      hasFlowSourceIsolation ||
      hasPrPublicationReceiptPressure ||
      hasPursuingGoalProofPressure ||
      benchmarkSelectedSmokePassedOnly ||
      benchmarkUnstableFullEvidence ||
      flowWorktreeIssueCount > 0;

  int get attentionScore {
    final passiveOpenCount =
        openRunCount > activeRunCount ? openRunCount - activeRunCount : 0;
    return (pendingQuestionCount * 10000) +
        (flowQuarantinedTargetCount * 12000) +
        (expediteControlPlaneCount * 11000) +
        (flowPausedAutomationCount * 9500) +
        (flowWorktreeMergeConflictCount * 9400) +
        (hasFlowSourceIsolation ? 8800 : 0) +
        (flowWorktreeIssueCount * 8300) +
        (flowWorktreeChangeCount > 0 ? 500 : 0) +
        (flowCaptainExpiredCount * 9000) +
        (ownerReadyFirstPrimaryOwnerCount * 8200) +
        (expediteStableInboxRequestCount * 7200) +
        (hasBlockedRecovery ? 7100 : 0) +
        (hasStaleActiveRun ? 6800 : 0) +
        (goalHealthCritical
            ? 9300
            : hasGoalHealthPressure
                ? 1600
                : 0) +
        (scheduledQualityLowQualityCount * 7600) +
        (scheduledQualityRepairedCount * 5400) +
        (benchmarkSelectedSmokePassedOnly ? 7350 : 0) +
        (benchmarkUnstableFullEvidence ? 7050 : 0) +
        (benchmarkFullPromotionBlocked ? 1150 : 0) +
        (hasPrPublicationReceiptPressure ? 900 : 0) +
        (hasPursuingGoalProofPressure ? 950 : 0) +
        (hasKpiGeneratedStateDrift ? 6900 : 0) +
        (hasPrRecoveryContract ? 6100 : 0) +
        (operatingNeedsInput ? 8000 : 0) +
        (statusBlocked ? 7000 : 0) +
        (flowUrgentCount * 6500) +
        (flowMailboxMalformedCorrectionCount * 6200) +
        (flowShapeInvalidCount * 3200) +
        (flowReviewMismatchCount * 3000) +
        (flowActiveLockStarvationCount * 2800) +
        (kpiBlockedPrCount * 2500) +
        (ownerReadyFirstSupportCount * 1800) +
        (expediteOpenPrBlockerCount * 2200) +
        (prPreflightBlocked
            ? 1200
            : hasPrPreflightPressure
                ? 320
                : 0) +
        deliveryFocusPriorityScore +
        (flowStablePendingCount * 1800) +
        (visibleActiveRunCount * 1000) +
        (queuedRunCount * 550) +
        (waitingRunCount * 420) +
        (kpiReadyCandidateCount * 450) +
        (expediteReadyCandidateCount * 425) +
        (passiveOpenCount * 300) +
        (flowPendingCount * 220) +
        (flowSupersededShapeInvalidCount * 210) +
        (flowStaleTempPublishCount * 200) +
        (kpiTempArtifactCount * 150) +
        (sourceActive && !scheduleEnabled ? 120 : 0) +
        (sourceActive ? 80 : 0) +
        (scheduleEnabled ? 40 : 0);
  }

  List<String> get searchTerms => [
        activeChatLabel,
        queuedChatLabel,
        queuedChipLabel,
        waitingChipLabel,
        openChatLabel,
        questionLabel,
        activeGoalChipLabel,
        activeRunGoalObjective,
        activeRunGoalStatus,
        activeRunGoalCurrentStep,
        activeRunGoalNextAction,
        activeRunGoalCompletionGate,
        activeGoalContractLabel,
        goalHealthChipLabel,
        goalHealthActionChipLabel,
        goalHealthTokensChipLabel,
        goalHealthLabel,
        goalHealthDetail,
        goalHealthActionDetail,
        goalHealthAutomation,
        goalHealthStatus,
        goalHealthSeverity,
        goalHealthTargetThread,
        goalHealthGoalStatus,
        goalHealthPressure,
        goalHealthStopAction,
        goalHealthSignal,
        goalHealthNextAction,
        goalHealthPath,
        goalHealthOpenPath,
        goalHealthHandoffLabel,
        goalHealthHandoffCopy,
        activeProgressLabel,
        activeProgressDetail,
        activeStaleChipLabel,
        activeStaleActionChipLabel,
        activeRunStaleKind,
        activeRunStaleAction,
        activeRunStaleDetail,
        activeRunStaleSafeNextStep,
        activeRunStaleHandoffLabel,
        blockedRecoveryChipLabel,
        blockedRecoveryDecisionChipLabel,
        blockedRecoveryPrecheckChipLabel,
        blockedRecoveryLabel,
        blockedRecoveryDetail,
        blockedRecoveryPrecheckLabel,
        blockedRecoveryPrecheckDetail,
        blockedRunRecoveryCategory,
        blockedRunRecoverySafeNextStep,
        blockedRunRecoveryLastFailedStep,
        blockedRunRecoveryLastFailedSummary,
        blockedRunRecoveryHandoffLabel,
        scheduledQualityChipLabel,
        scheduledQualityIssueChipLabel,
        scheduledQualityLabel,
        scheduledQualityDetail,
        scheduledQualityStatus,
        scheduledQualityIssue,
        scheduledQualityIssueLabel,
        scheduledQualityNextAction,
        scheduledQualityLatestRunId,
        scheduledQualityLatestStatus,
        benchmarkPromotionScopeChipLabel,
        benchmarkPromotionScopeLabel,
        benchmarkPromotionScopeDetail,
        ...benchmarkEvidenceGapLabels,
        benchmarkEvidenceNextAction,
        benchmarkEvidenceOpenActionLabel,
        benchmarkEvidenceHandoffLabel,
        benchmarkEvidenceHandoffCopy,
        benchmarkEvidenceIntakeChipLabel,
        benchmarkEvidenceIntakeDetail,
        benchmarkEvidenceIntakeStatus,
        benchmarkEvidenceIntakeNextAction,
        benchmarkEvidenceIntakeSourceRoot,
        benchmarkEvidenceIntakePromptPackManifest,
        ...benchmarkEvidenceIntakeMissingSources,
        ...benchmarkEvidenceRecoverySources,
        benchmarkEvidenceRecoveryDetail,
        if (hasBenchmarkEvidenceRecovery) 'frontier recovery',
        if (hasBenchmarkEvidenceRecovery) 'all-cases import',
        if (hasBenchmarkEvidenceRecovery) 'dry-run import',
        if (hasBenchmarkEvidenceRecovery) 'response staging',
        benchmarkEvidenceIntakeLocalModelStatus,
        ...benchmarkEvidenceIntakeLocalModelTimeoutSalvagedCases,
        if (benchmarkEvidenceIntakeLocalModelTimeoutSalvagedCases.isNotEmpty)
          'partial-timeout salvage',
        if (benchmarkEvidenceIntakeLocalModelTimeoutSalvagedCases.isNotEmpty)
          'local model timeout salvage',
        benchmarkProfile,
        benchmarkPromotionStatus,
        benchmarkSelectedScenariosStatus,
        benchmarkPromotionScope,
        benchmarkPassRate,
        benchmarkSourceStability,
        if (benchmarkSelectedSmokePassedOnly) 'smoke passed only',
        if (benchmarkSelectedSmokePassedOnly) 'selected scenarios passed only',
        if (benchmarkSelectedSmokePassedOnly) 'full promotion blocked',
        if (benchmarkUnstableFullEvidence) 'full bench unstable',
        if (benchmarkUnstableFullEvidence) 'source stability',
        if (benchmarkUnstableFullEvidence) 'source quiet',
        if (benchmarkUnstableFullEvidence) 'full benchmark evidence unstable',
        pursuingGoalProofIssueLabel,
        pursuingGoalProofDetail,
        if (hasPursuingGoalProofPressure) 'Pursuing goal proof',
        if (hasPursuingGoalProofPressure) 'goal proof',
        if (hasPursuingGoalProofPressure) 'objective-tied evidence',
        if (hasPursuingGoalProofPressure) 'completion gate proof',
        if (hasPrPublicationReceiptPressure) 'release trust',
        if (hasPrPublicationReceiptPressure) 'PR receipt',
        if (hasPrPublicationReceiptPressure) 'PR publication',
        if (hasPrPublicationReceiptPressure) 'current-head receipt',
        if (hasOperatorInput) 'needs input',
        if (hasBenchAttention) 'attention',
        if (hasBenchSafety) 'safety',
        if (hasPursuingGoalFocus) 'pursuing goal',
        if (hasPursuingGoalFocus) 'goals',
        kpiBlockedPrChipLabel,
        kpiReadyCandidateChipLabel,
        kpiTempArtifactChipLabel,
        kpiScoreChipLabel,
        kpiPrOwnerLaneChipLabel,
        kpiPrOwnerLaneLabel,
        kpiPrOwnerLaneDetail,
        kpiPrOwnerLaneActionLabel,
        kpiPrOwnerLaneCopyText,
        kpiGeneratedStateChipLabel,
        kpiGeneratedStateLabel,
        kpiGeneratedStateDetail,
        kpiGeneratedStateRefreshTarget,
        kpiGeneratedStateDriftKind,
        kpiGeneratedStateDriftSummary,
        kpiGeneratedStateScorecardTopAction,
        kpiGeneratedStateBoardTopAction,
        kpiGeneratedStatePath,
        kpiGeneratedStateOpenPath,
        kpiPrBlockerProofChipLabel,
        kpiPrBlockerGateChipLabel,
        kpiPrBlockerDecisionPacketLabel,
        kpiPrBlockerDecisionDetail,
        kpiPrBlockerDecisionMenuCopyText,
        kpiPrBlockerTopDetail,
        kpiPrBlockerProofFloorDetail,
        prBlockerProofFloorCopyText,
        kpiPrBlockerGateState,
        kpiPrBlockerGateLabel,
        kpiPrBlockerRequiredProof,
        ...kpiPrBlockerAllowedDecisions,
        kpiPrBlockerBlockedAction,
        kpiSignal,
        kpiNextAction,
        kpiPrPreviewLabel,
        expeditePrBlockerChipLabel,
        expediteReadyCandidateChipLabel,
        expediteStableInboxChipLabel,
        stableInboxPriorityChipLabel,
        stableInboxFreshnessChipLabel,
        stableInboxCompletionContractChipLabel,
        stableInboxCompletionContractDetail,
        stableInboxControlPlaneChipLabel,
        stableInboxControlPlaneGateDetail,
        stableInboxLiveControlGateChipLabel,
        stableInboxLiveControlGateDetail,
        stableInboxProcessedChipLabel,
        stableInboxProcessedReviewDetail,
        stableInboxProcessedStatus,
        stableInboxProcessedResult,
        stableInboxProcessedReportPath,
        stableInboxProcessedSummary,
        stableInboxClassificationChipLabel,
        stableInboxClassificationDetail,
        expediteControlPlaneChipLabel,
        expediteRankChipLabel,
        prPreflightChipLabel,
        prPreflightLabel,
        prPreflightDetail,
        prDeliveryRouteChipLabel,
        prDeliveryRouteDetail,
        prRecoveryContractChipLabel,
        prRecoveryContractLabel,
        prRecoveryContractDetail,
        prRecoveryContractCopyText,
        expediteOwnerActionChipLabel,
        expediteOwnerActionDetail,
        expeditePrPostureChipLabel,
        expeditePrPostureDetail,
        expediteEvidenceRequiredChipLabel,
        expediteEvidenceRequiredDetail,
        expediteHeartbeatLagChipLabel,
        expediteHeartbeatLagDetail,
        expediteHeartbeatInboxRouteChipLabel,
        expediteHeartbeatInboxRouteDetail,
        expeditePendingRequestChipLabel,
        expeditePendingRequestDetail,
        expediteHeartbeatRepairChipLabel,
        expediteHeartbeatRepairDetail,
        expediteDecisionChipLabel,
        expediteSafeDecisionDetail,
        expediteTopActionLabel,
        ownerReadyFirstChipLabel,
        ownerReadyFirstRankChipLabel,
        ownerReadyFirstRoleChipLabel,
        ownerReadyFirstLabel,
        ownerReadyFirstDetail,
        ownerReadyFirstPr,
        ownerReadyFirstPrNumber,
        ownerReadyFirstTitle,
        ownerReadyFirstOwner,
        ownerReadyFirstState,
        ownerReadyFirstRequiredLanes,
        ownerReadyFirstExistingRequest,
        ownerReadyFirstExistingRequestOpenPath,
        ownerReadyFirstNextAction,
        ownerReadyFirstPath,
        ownerReadyFirstOpenPath,
        if (hasOwnerReadyFirstPressure) 'owner ready first',
        if (hasOwnerReadyFirstPressure) 'owner-ready',
        stableInboxReviewDetail,
        expediteTopType,
        expediteTopOwner,
        stableInboxRequestPath,
        stableInboxRequestOpenPath,
        stableInboxRequestPriority,
        stableInboxRequestFrom,
        stableInboxRequestTo,
        stableInboxRequestBacklogId,
        stableInboxRequestPreview,
        stableInboxRequestExpectedDeliverable,
        stableInboxRequestSuccessCriteria,
        stableInboxRequestSafety,
        stableInboxLatestRequestPath,
        stableInboxLatestRequestOpenPath,
        stableInboxLatestRequestPriority,
        stableInboxLatestRequestFrom,
        stableInboxLatestRequestTo,
        stableInboxLatestRequestBacklogId,
        stableInboxLatestRequestPreview,
        stableInboxLatestRequestExpectedDeliverable,
        stableInboxLatestRequestSuccessCriteria,
        stableInboxLatestRequestSafety,
        stableInboxFreshnessDetail,
        expediteBoardPath,
        expediteBoardOpenPath,
        expediteSignal,
        expediteNextAction,
        expeditePrPreviewLabel,
        flowAgent,
        flowContainmentChipLabel,
        flowQuarantineTrustStateChipLabel,
        flowQuarantineTrustStateDetail,
        flowQuarantineProofChipLabel,
        flowQuarantineProofDetail,
        flowQuarantineProofFloorDetail,
        flowQuarantineClearanceChecklistDetail,
        flowQuarantineClearanceChecklistCopyText,
        flowQuarantineRequirementDetail,
        flowQuarantineGuidanceDetail,
        flowQuarantineRequiredProof,
        flowQuarantineOperatorGuidance,
        flowQuarantineThreadId,
        flowQuarantineStatus,
        flowQuarantineOperatorLabel,
        flowQuarantineSessionGoalStatus,
        flowQuarantineSessionPath,
        flowQuarantineActivityState,
        flowQuarantineTargetLastWriteUtc,
        flowQuarantineProofFloorUtc,
        flowQuarantineProofNextCheckUtc,
        flowQuarantineProofRefreshDetail,
        flowPausedAutomationChipLabel,
        flowPausedAutomationDetail,
        flowPausedAutomationGuidanceDetail,
        flowPausedAutomationId,
        flowPausedAutomationName,
        flowPausedAutomationStatus,
        flowPausedAutomationThreadId,
        flowPausedAutomationGoalStatus,
        flowPausedAutomationSessionPath,
        flowPausedAutomationGuidance,
        flowPausedAutomationHandoffLabel,
        flowPausedAutomationHandoffCopy,
        flowSourceIsolationChipLabel,
        flowSourceTrustChipLabel,
        flowSourceIsolationLabel,
        flowSourceTrustDetail,
        flowSourceTrustEvidenceLabel,
        deliveryFocusChipLabel,
        deliveryFocusLabel,
        deliveryFocusDetail,
        flowRepositoryRootBranch,
        flowWorktreeChipLabel,
        flowCaptainChipLabel,
        flowUrgentChipLabel,
        flowStableChipLabel,
        flowIssueChipLabel,
        flowCoordinationDriftChipLabel,
        flowCoordinationDriftLabel,
        flowCoordinationDriftDetail,
        flowSignal,
        flowNextAction,
        flowNextActionPath,
        flowNextActionOpenPath,
        flowNextActionHandoffLabel,
        flowSafetyActionLabel,
        flowSafetyPriorityReason,
        benchActionStackLabel,
        permissionBoundaryChipLabel,
        permissionBoundaryStackLabel,
        ...permissionBoundarySearchTerms,
        flowNextActionHandoffCopy,
      ].where((label) => label.isNotEmpty).toList(growable: false);
}

class _BenchReleaseTrustPriority {
  const _BenchReleaseTrustPriority({
    required this.score,
    required this.targets,
  });

  const _BenchReleaseTrustPriority.empty()
      : score = 0,
        targets = const <String>[];

  final int score;
  final List<String> targets;

  bool get active => score > 0 && targets.isNotEmpty;
}

class AutopilotAgentBenchReleaseTrustFocus {
  const AutopilotAgentBenchReleaseTrustFocus({
    this.active = false,
    this.chipLabel = '',
    this.gateChipLabel = '',
    this.gateSummaryLabel = '',
    this.gateDetail = '',
    this.priorityLabel = '',
    this.detail = '',
    this.actionLabel = '',
    this.path = '',
    this.openPath = '',
    this.handoffLabel = '',
    this.handoffCopy = '',
    this.item = const <String, dynamic>{},
  });

  final bool active;
  final String chipLabel;
  final String gateChipLabel;
  final String gateSummaryLabel;
  final String gateDetail;
  final String priorityLabel;
  final String detail;
  final String actionLabel;
  final String path;
  final String openPath;
  final String handoffLabel;
  final String handoffCopy;
  final Map<String, dynamic> item;

  bool get hasAction =>
      active &&
      (path.trim().isNotEmpty ||
          openPath.trim().isNotEmpty ||
          handoffCopy.trim().isNotEmpty);

  bool get hasGateSignal =>
      active &&
      (gateChipLabel.trim().isNotEmpty ||
          gateSummaryLabel.trim().isNotEmpty ||
          gateDetail.trim().isNotEmpty);
}

class AutopilotAgentBenchActivityPresenter {
  static AutopilotAgentBenchActivityPresentation fromAgent(
    Map<String, dynamic> agent,
  ) {
    final operatingState = _asMap(agent['operating_state']);
    final activeRun = _asMap(agent['active_run_preview']);
    final activeGoal = _asMap(activeRun['pursuing_goal']);
    final blockedRun = _asMap(agent['blocked_run_preview']);
    final schedule = _asMap(agent['schedule']);
    final scheduledQuality = _asMap(agent['scheduled_quality_pressure']);
    final codingBenchmark = _firstMap([
      agent['coding_benchmark'],
      agent['coding_benchmark_signal'],
      agent['coding_benchmark_readiness'],
      agent['agent_coding_benchmark'],
      agent['benchmark_readiness'],
      scheduledQuality['coding_benchmark'],
      scheduledQuality['coding_benchmark_signal'],
      scheduledQuality['benchmark'],
      scheduledQuality['benchmark_readiness'],
    ]);
    final goalHealth = _asMap(agent['goal_health_pressure']);
    final kpi = _asMap(agent['kpi_lane_pressure']);
    final expedite = _asMap(agent['expedite_lane_pressure']);
    final flow = _asMap(agent['agent_flow_pressure']);
    final benchmarkEvidenceGapLabels = _dedupeStrings([
      ..._asStringList(codingBenchmark['frontier_evidence_gap_labels']),
      ..._asStringList(scheduledQuality['frontier_evidence_gap_labels']),
      for (final gap in _asMapList(codingBenchmark['frontier_evidence_gaps']))
        _machineLabel(_firstClean([gap['label'], gap['gate']])),
      for (final gap in _asMapList(scheduledQuality['frontier_evidence_gaps']))
        _machineLabel(_firstClean([gap['label'], gap['gate']])),
    ]);
    final benchmarkEvidenceNextAction = _firstClean([
      codingBenchmark['frontier_evidence_next_action'],
      scheduledQuality['frontier_evidence_next_action'],
      for (final gap in _asMapList(codingBenchmark['frontier_evidence_gaps']))
        gap['next_action'],
      for (final gap in _asMapList(scheduledQuality['frontier_evidence_gaps']))
        gap['next_action'],
    ]);
    final benchmarkEvidenceIntake = _firstMap([
      codingBenchmark['frontier_model_evidence_intake'],
      codingBenchmark['frontier_evidence_intake'],
      scheduledQuality['frontier_model_evidence_intake'],
      scheduledQuality['frontier_evidence_intake'],
    ]);
    final benchmarkLocalModelCandidateRun =
        _asMap(benchmarkEvidenceIntake['local_model_candidate_run']);
    final benchmarkEvidenceIntakeSources =
        _asMapList(benchmarkEvidenceIntake['sources']);
    final benchmarkEvidenceIntakeMissingSources = _dedupeStrings([
      for (final source in benchmarkEvidenceIntakeSources)
        if (_statusKey(source['status']) != 'ready')
          _firstClean(
              [source['source_kind'], source['source'], source['path']]),
    ]);
    final benchmarkEvidenceRecoveryRoutes =
        _benchmarkEvidenceRecoveryRoutes(benchmarkEvidenceIntake);
    final benchmarkEvidenceRecoverySources = _dedupeStrings([
      for (final route in benchmarkEvidenceRecoveryRoutes)
        _firstClean([route['source_kind'], route['source']]),
    ]);
    final benchmarkEvidenceRecoveryDetail =
        _benchmarkEvidenceRecoveryDetail(benchmarkEvidenceRecoveryRoutes);
    final benchmarkEvidenceHandoffCopy = _benchmarkEvidenceHandoffCopy(
      codingBenchmark,
      scheduledQuality,
      gapLabels: benchmarkEvidenceGapLabels,
      nextAction: benchmarkEvidenceNextAction,
      recoveryRoutes: benchmarkEvidenceRecoveryRoutes,
    );
    final benchmarkEvidenceHandoffLabel = _firstClean([
      codingBenchmark['frontier_evidence_handoff_label'],
      scheduledQuality['frontier_evidence_handoff_label'],
      if (benchmarkEvidenceHandoffCopy.isNotEmpty) 'Copy frontier proof packet',
    ]);
    return AutopilotAgentBenchActivityPresentation(
      agentName: _firstClean([
        agent['name'],
        agent['role'],
        agent['profile_key'],
      ]),
      activeRunCount: _asInt(agent['active_run_count']),
      openRunCount: _asInt(agent['open_run_count']),
      queuedRunCount: _asInt(agent['queued_run_count']),
      workerActiveRunCount: _asInt(agent['worker_active_run_count']),
      waitingRunCount: _asInt(agent['waiting_run_count']),
      pendingQuestionCount: _asInt(agent['pending_question_count']),
      activeRunId: _clean(activeRun['run_id']),
      activeRunStatus: _clean(activeRun['status']),
      activeRunStage: _clean(activeRun['current_stage']),
      activeRunPlanStatus: _clean(activeRun['plan_status']),
      activeRunTitle: _clean(activeRun['title']),
      activeRunGoalObjective: _clean(activeGoal['objective']),
      activeRunGoalStatus: _clean(activeGoal['status']),
      activeRunGoalProgressPercent: _asInt(activeGoal['progress_percent']),
      activeRunGoalCurrentStep: _clean(activeGoal['current_step']),
      activeRunGoalNextAction: _clean(activeGoal['next_action_label']),
      activeRunGoalCompletionGate: _clean(activeGoal['completion_gate']),
      goalHealthAutomation: _clean(goalHealth['automation']),
      goalHealthStatus: _clean(goalHealth['status']),
      goalHealthSeverity: _clean(goalHealth['severity']),
      goalHealthTargetThread: _clean(goalHealth['target_thread']),
      goalHealthGoalStatus: _clean(goalHealth['goal']),
      goalHealthTokens: _clean(goalHealth['tokens']),
      goalHealthGoalHours: _clean(goalHealth['goal_hours']),
      goalHealthSessionMb: _clean(goalHealth['session_mb']),
      goalHealthSessionAge: _clean(goalHealth['session_age']),
      goalHealthControlRisk: _clean(goalHealth['control_risk']),
      goalHealthPressure: _clean(goalHealth['pressure']),
      goalHealthStopAction: _clean(goalHealth['stop_action']),
      goalHealthReason: _clean(goalHealth['reason']),
      goalHealthSignal: _clean(goalHealth['current_signal']),
      goalHealthNextAction: _clean(goalHealth['next_action']),
      goalHealthPath: _clean(goalHealth['path']),
      goalHealthOpenPath: _clean(goalHealth['open_path']),
      goalHealthProvidedHandoffLabel: _firstClean([
        goalHealth['handoff_label'],
        goalHealth['report_goal_health_handoff_label'],
      ]),
      goalHealthProvidedHandoffCopy: _firstClean([
        goalHealth['handoff_copy'],
        goalHealth['report_goal_health_handoff_copy'],
      ]),
      activeRunUpdatedAt: _clean(activeRun['updated_at']),
      activeRunLatestStepTitle: _clean(activeRun['latest_step_title']),
      activeRunLatestStepStatus: _clean(activeRun['latest_step_status']),
      activeRunLatestStepStage: _clean(activeRun['latest_step_stage']),
      activeRunStale: activeRun['stale'] == true,
      activeRunStaleKind: _clean(activeRun['stale_kind']),
      activeRunLastSeenAgeMinutes: _asInt(activeRun['last_seen_age_minutes']),
      activeRunStaleAfterMinutes: _asInt(activeRun['stale_after_minutes']),
      activeRunStaleAction: _clean(activeRun['stale_action']),
      activeRunStaleActionLabel: _clean(activeRun['stale_action_label']),
      activeRunStaleDetail: _clean(activeRun['stale_detail']),
      activeRunStaleSafeNextStep: _clean(activeRun['stale_safe_next_step']),
      activeRunStaleHandoffLabel: _clean(activeRun['stale_handoff_label']),
      activeRunStaleHandoffCopy: _clean(activeRun['stale_handoff_copy']),
      blockedRunId: _clean(blockedRun['run_id']),
      blockedRunStatus: _clean(blockedRun['status']),
      blockedRunStage: _clean(blockedRun['current_stage']),
      blockedRunTitle: _clean(blockedRun['title']),
      blockedRunReason: _clean(blockedRun['reason']),
      blockedRunUpdatedAt: _clean(blockedRun['updated_at']),
      blockedRunRecoveryAction: _clean(blockedRun['recovery_action']),
      blockedRunRecoveryActionLabel:
          _clean(blockedRun['recovery_action_label']),
      blockedRunRecoveryCategory: _clean(blockedRun['recovery_category']),
      blockedRunRecoveryCanRerun: blockedRun['recovery_can_rerun'] == true,
      blockedRunRecoveryDecisionLabel:
          _clean(blockedRun['recovery_decision_label']),
      blockedRunRecoverySafeNextStep:
          _clean(blockedRun['recovery_safe_next_step']),
      blockedRunRecoveryLastFailedStep:
          _clean(blockedRun['recovery_last_failed_step']),
      blockedRunRecoveryLastFailedExitCode:
          _clean(blockedRun['recovery_last_failed_exit_code']),
      blockedRunRecoveryLastFailedSummary:
          _clean(blockedRun['recovery_last_failed_summary']),
      blockedRunRecoveryHandoffLabel:
          _clean(blockedRun['recovery_handoff_label']),
      blockedRunRecoveryHandoffCopy:
          _clean(blockedRun['recovery_handoff_copy']),
      operatingNeedsInput: _clean(operatingState['state']) == 'needs_input',
      statusBlocked: _clean(agent['status']) == 'blocked',
      scheduleEnabled: agent['schedule_enabled'] == true,
      sourceActive: _clean(schedule['source_status']).toUpperCase() == 'ACTIVE',
      scheduledQualityStatus: _clean(scheduledQuality['status']),
      scheduledQualityTotal: _asInt(scheduledQuality['total']),
      scheduledQualityPassedCount: _asInt(scheduledQuality['passed']),
      scheduledQualityRepairedCount: _asInt(scheduledQuality['repaired_count']),
      scheduledQualityLowQualityCount:
          _asInt(scheduledQuality['low_quality_count']),
      scheduledQualityAverageScoreLabel:
          _scoreLabel(scheduledQuality['average_score']),
      scheduledQualityIssue: _clean(scheduledQuality['issue']),
      scheduledQualityIssueLabel: _clean(scheduledQuality['issue_label']),
      scheduledQualityIssueCount: _asInt(scheduledQuality['issue_count']),
      scheduledQualityNextAction: _clean(scheduledQuality['next_action']),
      scheduledQualityLatestRunId: _clean(scheduledQuality['latest_run_id']),
      scheduledQualityLatestStatus: _clean(scheduledQuality['latest_status']),
      scheduledQualityLatestScoreLabel:
          _scoreLabel(scheduledQuality['latest_score']),
      scheduledQualityLatestInitialScoreLabel:
          _scoreLabel(scheduledQuality['latest_initial_score']),
      scheduledQualityHandoffLabel:
          _clean(scheduledQuality['scheduled_quality_handoff_label']),
      scheduledQualityHandoffCopy:
          _clean(scheduledQuality['scheduled_quality_handoff_copy']),
      benchmarkProfile: _firstClean([
        codingBenchmark['profile'],
        scheduledQuality['benchmark_profile'],
        scheduledQuality['profile'],
      ]),
      benchmarkPromotionStatus: _firstClean([
        codingBenchmark['promotion_status'],
        codingBenchmark['benchmark_status'],
        codingBenchmark['full_status'],
        scheduledQuality['promotion_status'],
        scheduledQuality['benchmark_status'],
        scheduledQuality['full_status'],
      ]),
      benchmarkSelectedScenariosStatus: _firstClean([
        codingBenchmark['selected_scenarios_status'],
        codingBenchmark['selected_status'],
        scheduledQuality['selected_scenarios_status'],
        scheduledQuality['selected_status'],
      ]),
      benchmarkPromotionScope: _firstClean([
        codingBenchmark['promotion_scope'],
        codingBenchmark['benchmark_scope'],
        scheduledQuality['promotion_scope'],
        scheduledQuality['benchmark_scope'],
      ]),
      benchmarkPassRate: _firstClean([
        codingBenchmark['effective_pass_rate'],
        codingBenchmark['pass_rate'],
        scheduledQuality['effective_pass_rate'],
        scheduledQuality['pass_rate'],
      ]),
      benchmarkEvidenceGapLabels: benchmarkEvidenceGapLabels,
      benchmarkEvidenceNextAction: benchmarkEvidenceNextAction,
      benchmarkEvidenceHandoffLabel: benchmarkEvidenceHandoffLabel,
      benchmarkEvidenceHandoffCopy: benchmarkEvidenceHandoffCopy,
      benchmarkEvidenceIntakeStatus: _clean(benchmarkEvidenceIntake['status']),
      benchmarkEvidenceIntakeReadyCount:
          _asInt(benchmarkEvidenceIntake['ready_source_count']),
      benchmarkEvidenceIntakeRequiredCount:
          _asInt(benchmarkEvidenceIntake['required_source_count']),
      benchmarkEvidenceIntakeMissingCount:
          _asInt(benchmarkEvidenceIntake['missing_source_count']),
      benchmarkEvidenceIntakeNextAction:
          _clean(benchmarkEvidenceIntake['next_action']),
      benchmarkEvidenceIntakeSourceRoot:
          _clean(benchmarkEvidenceIntake['raw_source_root']),
      benchmarkEvidenceIntakePromptPackManifest:
          _clean(benchmarkEvidenceIntake['prompt_pack_manifest']),
      benchmarkEvidenceIntakeMissingSources:
          benchmarkEvidenceIntakeMissingSources,
      benchmarkEvidenceRecoverySources: benchmarkEvidenceRecoverySources,
      benchmarkEvidenceRecoveryDetail: benchmarkEvidenceRecoveryDetail,
      benchmarkEvidenceIntakeLocalModelStatus:
          _clean(benchmarkLocalModelCandidateRun['status']),
      benchmarkEvidenceIntakeLocalModelTimeoutSalvagedCases: _asStringList(
        benchmarkLocalModelCandidateRun['timeout_salvaged_cases'],
      ),
      benchmarkSourceStability: _firstClean([
        codingBenchmark['source_stability'],
        scheduledQuality['source_stability'],
      ]),
      kpiBlockedPrCount: _asInt(kpi['blocked_pr_count']),
      kpiReadyCandidateCount: _asInt(kpi['ready_candidate_count']),
      kpiTempArtifactCount: _asInt(kpi['temp_artifact_count']),
      kpiScoreLabel: _scoreLabel(kpi['score']),
      kpiSeverity: _clean(kpi['severity']),
      kpiSignal: _clean(kpi['current_signal']),
      kpiNextAction: _clean(kpi['next_action']),
      kpiPrNumbers: _asStringList(kpi['pr_numbers']),
      kpiPrBlockerDirtyCount: _asInt(kpi['pr_blocker_dirty_count']),
      kpiPrBlockerNoChecksCount: _asInt(kpi['pr_blocker_no_checks_count']),
      kpiPrBlockerFailingCount: _asInt(kpi['pr_blocker_failing_count']),
      kpiPrBlockerNonDraftCount: _asInt(kpi['pr_blocker_non_draft_count']),
      kpiPrBlockerTopPr: _clean(kpi['pr_blocker_top_pr']),
      kpiPrBlockerTopBranch: _clean(kpi['pr_blocker_top_branch']),
      kpiPrBlockerTopMerge: _clean(kpi['pr_blocker_top_merge']),
      kpiPrBlockerTopCi: _clean(kpi['pr_blocker_top_ci']),
      kpiPrBlockerTopPosture: _clean(kpi['pr_blocker_top_posture']),
      kpiPrBlockerProofFloor: _clean(kpi['pr_blocker_proof_floor']),
      kpiPrBlockerGateState: _clean(kpi['pr_blocker_gate_state']),
      kpiPrBlockerGateLabel: _clean(kpi['pr_blocker_gate_label']),
      kpiPrBlockerAllowedDecisions:
          _asStringList(kpi['pr_blocker_allowed_decisions']),
      kpiPrBlockerRequiredProof: _clean(kpi['pr_blocker_required_proof']),
      kpiPrBlockerBlockedAction: _clean(kpi['pr_blocker_blocked_action']),
      kpiGeneratedStateRefreshRequired:
          kpi['generated_state_refresh_required'] == true,
      kpiGeneratedStateRefreshTarget:
          _clean(kpi['generated_state_refresh_target']),
      kpiGeneratedStateDriftCount: _asInt(kpi['generated_state_drift_count']),
      kpiGeneratedStateDriftKind: _clean(kpi['generated_state_drift_kind']),
      kpiGeneratedStateDriftLabel: _clean(kpi['generated_state_drift_label']),
      kpiGeneratedStateDriftSummary:
          _clean(kpi['generated_state_drift_summary']),
      kpiGeneratedStateBoardGenerated:
          _clean(kpi['generated_state_board_generated']),
      kpiGeneratedStateScorecardGenerated:
          _clean(kpi['generated_state_scorecard_generated']),
      kpiGeneratedStateScorecardTopAction:
          _clean(kpi['generated_state_scorecard_top_action']),
      kpiGeneratedStateBoardTopAction:
          _clean(kpi['generated_state_board_top_action']),
      kpiGeneratedStateNextAction: _clean(kpi['next_action']),
      kpiGeneratedStatePath: _clean(kpi['generated_state_path']),
      kpiGeneratedStateOpenPath: _clean(kpi['generated_state_open_path']),
      expediteOpenPrBlockerCount: _asInt(expedite['open_pr_blocker_count']),
      expediteReadyCandidateCount: _asInt(expedite['ready_candidate_count']),
      expediteStableInboxRequestCount: _asInt(
        expedite['stable_inbox_request_count'],
      ),
      expediteControlPlaneCount: _asInt(expedite['control_plane_count']),
      expediteSeverity: _clean(expedite['severity']),
      expediteSignal: _clean(expedite['current_signal']),
      expediteNextAction: _clean(expedite['next_action']),
      expeditePrNumbers: _asStringList(expedite['pr_numbers']),
      expediteTopRank: _asInt(expedite['top_rank']),
      expediteTopType: _clean(expedite['top_type']),
      expediteTopEvidence: _clean(expedite['top_evidence']),
      expediteTopOwner: _clean(expedite['top_owner']),
      expediteBoardPath: _clean(expedite['path']),
      expediteBoardOpenPath: _clean(expedite['open_path']),
      ownerReadyFirstCount: _asInt(expedite['owner_ready_first_count']),
      ownerReadyFirstPrimaryOwnerCount:
          _asInt(expedite['owner_ready_first_primary_owner_count']),
      ownerReadyFirstSupportCount:
          _asInt(expedite['owner_ready_first_support_count']),
      ownerReadyFirstTopRank: _asInt(expedite['owner_ready_first_top_rank']),
      ownerReadyFirstPr: _clean(expedite['owner_ready_first_pr']),
      ownerReadyFirstPrNumber: _clean(expedite['owner_ready_first_pr_number']),
      ownerReadyFirstTitle: _clean(expedite['owner_ready_first_title']),
      ownerReadyFirstOwner: _clean(expedite['owner_ready_first_owner']),
      ownerReadyFirstState: _clean(expedite['owner_ready_first_state']),
      ownerReadyFirstRequiredLanes:
          _clean(expedite['owner_ready_first_required_lanes']),
      ownerReadyFirstExistingRequest:
          _clean(expedite['owner_ready_first_existing_request']),
      ownerReadyFirstExistingRequestOpenPath:
          _clean(expedite['owner_ready_first_existing_request_open_path']),
      ownerReadyFirstNextAction:
          _clean(expedite['owner_ready_first_next_action']),
      ownerReadyFirstLaneRole: _clean(expedite['owner_ready_first_lane_role']),
      ownerReadyFirstPath: _clean(expedite['owner_ready_first_path']),
      ownerReadyFirstOpenPath: _clean(expedite['owner_ready_first_open_path']),
      ownerReadyFirstGeneratedAt:
          _clean(expedite['owner_ready_first_generated_at']),
      ownerReadyFirstSha256: _clean(expedite['owner_ready_first_sha256']),
      ownerReadyFirstSafety: _clean(expedite['owner_ready_first_safety']),
      ownerReadyFirstHandoffCopy:
          _clean(expedite['owner_ready_first_handoff_copy']),
      stableInboxTopRank: _asInt(expedite['stable_inbox_top_rank']),
      stableInboxRequestPath: _clean(expedite['stable_inbox_request_path']),
      stableInboxRequestOpenPath:
          _clean(expedite['stable_inbox_request_open_path']),
      stableInboxRequestSha256: _clean(expedite['stable_inbox_request_sha256']),
      stableInboxRequestPriority:
          _clean(expedite['stable_inbox_request_priority']),
      stableInboxRequestFrom: _clean(expedite['stable_inbox_request_from']),
      stableInboxRequestTo: _clean(expedite['stable_inbox_request_to']),
      stableInboxRequestBacklogId:
          _clean(expedite['stable_inbox_request_backlog_id']),
      stableInboxRequestPreview:
          _clean(expedite['stable_inbox_request_preview']),
      stableInboxRequestExpectedDeliverable:
          _clean(expedite['stable_inbox_request_expected_deliverable']),
      stableInboxRequestSuccessCriteria:
          _clean(expedite['stable_inbox_request_success_criteria']),
      stableInboxRequestSafety: _clean(expedite['stable_inbox_request_safety']),
      stableInboxStaleForLatest:
          expedite['stable_inbox_request_stale_for_latest'] == true,
      stableInboxLatestRequestPath:
          _clean(expedite['stable_inbox_latest_request_path']),
      stableInboxLatestRequestOpenPath:
          _clean(expedite['stable_inbox_latest_request_open_path']),
      stableInboxLatestRequestSha256:
          _clean(expedite['stable_inbox_latest_request_sha256']),
      stableInboxLatestRequestCreated:
          _clean(expedite['stable_inbox_latest_request_created']),
      stableInboxLatestRequestPriority:
          _clean(expedite['stable_inbox_latest_request_priority']),
      stableInboxLatestRequestFrom:
          _clean(expedite['stable_inbox_latest_request_from']),
      stableInboxLatestRequestTo:
          _clean(expedite['stable_inbox_latest_request_to']),
      stableInboxLatestRequestBacklogId:
          _clean(expedite['stable_inbox_latest_request_backlog_id']),
      stableInboxLatestRequestPreview:
          _clean(expedite['stable_inbox_latest_request_preview']),
      stableInboxLatestRequestExpectedDeliverable:
          _clean(expedite['stable_inbox_latest_request_expected_deliverable']),
      stableInboxLatestRequestSuccessCriteria:
          _clean(expedite['stable_inbox_latest_request_success_criteria']),
      stableInboxLatestRequestSafety:
          _clean(expedite['stable_inbox_latest_request_safety']),
      stableInboxLatestHandoffCopy:
          _clean(expedite['stable_inbox_latest_handoff_copy']),
      stableInboxFreshnessDetail:
          _clean(expedite['stable_inbox_freshness_detail']),
      stableInboxRequestProcessed:
          expedite['stable_inbox_request_processed'] == true,
      stableInboxRequestProcessedUtc:
          _clean(expedite['stable_inbox_request_processed_utc']),
      stableInboxRequestProcessedStatus:
          _clean(expedite['stable_inbox_request_processed_status']),
      stableInboxRequestProcessedResult:
          _clean(expedite['stable_inbox_request_processed_result']),
      stableInboxRequestProcessedReportPath:
          _clean(expedite['stable_inbox_request_processed_report_path']),
      stableInboxRequestProcessedSummary:
          _clean(expedite['stable_inbox_request_processed_summary']),
      stableInboxLatestRequestProcessed:
          expedite['stable_inbox_latest_request_processed'] == true,
      stableInboxLatestRequestProcessedUtc:
          _clean(expedite['stable_inbox_latest_request_processed_utc']),
      stableInboxLatestRequestProcessedStatus:
          _clean(expedite['stable_inbox_latest_request_processed_status']),
      stableInboxLatestRequestProcessedResult:
          _clean(expedite['stable_inbox_latest_request_processed_result']),
      stableInboxLatestRequestProcessedReportPath:
          _clean(expedite['stable_inbox_latest_request_processed_report_path']),
      stableInboxLatestRequestProcessedSummary:
          _clean(expedite['stable_inbox_latest_request_processed_summary']),
      stableInboxProcessedDetail:
          _clean(expedite['stable_inbox_processed_detail']),
      stableInboxHandoffLabel: _clean(expedite['stable_inbox_handoff_label']),
      stableInboxHandoffCopy: _clean(expedite['stable_inbox_handoff_copy']),
      stableInboxLiveBrokerActionCount:
          _asInt(expedite['stable_inbox_live_broker_action_count']),
      stableInboxLiveBrokerLabel:
          _clean(expedite['stable_inbox_live_broker_label']),
      stableInboxLiveBrokerDetail:
          _clean(expedite['stable_inbox_live_broker_detail']),
      stableInboxLiveBrokerProofFloorUtc:
          _clean(expedite['stable_inbox_live_broker_proof_floor_utc']),
      stableInboxControlPlaneActionCount:
          _asInt(expedite['stable_inbox_control_plane_action_count']),
      stableInboxControlPlaneLabel:
          _clean(expedite['stable_inbox_control_plane_label']),
      stableInboxControlPlaneDetail:
          _clean(expedite['stable_inbox_control_plane_detail']),
      stableInboxControlPlaneProofFloorUtc:
          _clean(expedite['stable_inbox_control_plane_proof_floor_utc']),
      stableInboxControlPlaneThreadIds: _asStringList(
        expedite['stable_inbox_control_plane_thread_ids'],
      ),
      flowAgent: _clean(flow['agent']),
      flowPendingCount: _asInt(flow['pending_count']),
      flowStablePendingCount: _asInt(flow['stable_pending_count']),
      flowUrgentCount: _asInt(flow['urgent_count']),
      flowControlPlaneBlockerCount: _asInt(
        flow['control_plane_blocker_count'],
      ),
      flowQuarantinedTargetCount: _asInt(flow['quarantined_target_count']),
      flowQuarantineThreadId: _clean(flow['quarantine_thread_id']),
      flowQuarantineStatus: _clean(flow['quarantine_status']),
      flowQuarantineOperatorLabel: _clean(flow['quarantine_operator_label']),
      flowQuarantineRequiredProof: _clean(flow['quarantine_required_proof']),
      flowQuarantineOperatorGuidance:
          _clean(flow['quarantine_operator_guidance']),
      flowQuarantineSessionAgeMinutes:
          _asNullableInt(flow['quarantine_session_age_minutes']) ?? -1,
      flowQuarantineSessionGoalStatus:
          _clean(flow['quarantine_session_goal_status']),
      flowQuarantineSessionPath: _clean(flow['quarantine_session_path']),
      flowQuarantineActivityState: _clean(flow['quarantine_activity_state']),
      flowQuarantineTargetLastWriteUtc:
          _clean(flow['quarantine_target_last_write_utc']),
      flowQuarantineProofFloorUtc: _clean(flow['quarantine_proof_floor_utc']),
      flowQuarantineProofWindowMinutes: _asInt(
        flow['quarantine_proof_window_minutes'],
      ),
      flowQuarantineProofRemainingMinutes: _asNullableInt(
            flow['quarantine_proof_remaining_minutes'],
          ) ??
          -1,
      flowQuarantineProofNextCheckUtc:
          _clean(flow['quarantine_proof_next_check_utc']),
      flowQuarantineProofSatisfied: flow['quarantine_proof_satisfied'] == true,
      flowPausedAutomationCount: _asInt(flow['paused_automation_count']),
      flowPausedAutomationId: _clean(flow['paused_automation_id']),
      flowPausedAutomationName: _clean(flow['paused_automation_name']),
      flowPausedAutomationStatus: _clean(flow['paused_automation_status']),
      flowPausedAutomationThreadId: _clean(flow['paused_automation_thread_id']),
      flowPausedAutomationSessionAgeMinutes:
          _asNullableInt(flow['paused_automation_session_age_minutes']) ?? -1,
      flowPausedAutomationThresholdMinutes:
          _asNullableInt(flow['paused_automation_threshold_minutes']) ?? -1,
      flowPausedAutomationGoalStatus:
          _clean(flow['paused_automation_goal_status']),
      flowPausedAutomationSessionPath:
          _clean(flow['paused_automation_session_path']),
      flowPausedAutomationGuidance: _clean(flow['paused_automation_guidance']),
      flowPausedAutomationCoveredByQuarantine:
          flow['paused_automation_covered_by_quarantine'] == true,
      flowPausedAutomationHandoffLabel:
          _clean(flow['paused_automation_operator_handoff_label']),
      flowPausedAutomationHandoffCopy:
          _clean(flow['paused_automation_operator_handoff_copy']),
      flowPausedAutomationHandoffMutatesControlPlane:
          flow['paused_automation_operator_handoff_mutates_control_plane'] ==
              true,
      flowWorktreeHygieneCount: _asInt(flow['worktree_hygiene_count']),
      flowDirtyWorktreeCount: _asInt(flow['dirty_worktree_count']),
      flowDetachedWorktreeCount: _asInt(
        flow['detached_uncontained_worktree_count'],
      ),
      flowWorktreeMergeConflictCount:
          _asInt(flow['worktree_merge_conflict_count']),
      flowWorktreeChangeCount: _asInt(flow['worktree_change_count']),
      flowRepositoryRootDirtyCount: _asInt(
        flow['repository_root_dirty_count'],
      ),
      flowRepositoryRootChangeCount: _asInt(
        flow['repository_root_change_count'],
      ),
      flowRepositoryRootMergeConflictCount:
          _asInt(flow['repository_root_merge_conflict_count']),
      flowRepositoryRootBranch: _clean(flow['repository_root_branch']),
      flowSourceIsolationBlocked: flow['source_isolation_blocked'] == true,
      flowCaptainExpiredCount: _asInt(flow['captain_expired_count']),
      flowShapeInvalidCount: _asInt(flow['shape_invalid_count']),
      flowMailboxMalformedCorrectionCount:
          _asInt(flow['mailbox_malformed_correction_count']),
      flowSupersededShapeInvalidCount:
          _asInt(flow['superseded_shape_invalid_count']),
      flowReviewMismatchCount: _asInt(flow['review_head_mismatch_count']),
      flowActiveLockStarvationCount: _asInt(
        flow['active_lock_starvation_count'],
      ),
      flowStaleTempPublishCount: _asInt(
        flow['stale_temp_publish_artifact_count'],
      ),
      flowSignal: _clean(flow['current_signal']),
      flowNextAction: _clean(flow['next_action']),
      flowNextActionPath: _clean(flow['next_action_path']),
      flowNextActionOpenPath: _clean(flow['next_action_open_path']),
      flowNextActionHandoffLabel: _clean(
        flow['next_action_handoff_label'],
      ),
      flowNextActionHandoffCopy: _clean(flow['next_action_handoff_copy']),
      flowNextActionHandoffMutatesControlPlane:
          flow['next_action_handoff_mutates_control_plane'] == true,
      flowSeverity: _clean(flow['severity']),
    );
  }

  static List<Map<String, dynamic>> sortAttentionFirst(
    Iterable<Map<String, dynamic>> agents, {
    Map<String, dynamic>? readiness,
  }) {
    final sorted = List<Map<String, dynamic>>.of(agents);
    final releaseTrust = _releaseTrustPriorityFromReadiness(readiness);
    sorted.sort(
      (left, right) => _compareAttentionFirst(
        left,
        right,
        releaseTrust: releaseTrust,
      ),
    );
    return sorted;
  }

  static AutopilotAgentBenchReleaseTrustFocus releaseTrustFocusForAgent(
    Map<String, dynamic> agent, {
    Map<String, dynamic>? readiness,
  }) {
    final inbox = _asMap(readiness?['operator_inbox']);
    final releaseTrust = _asMap(inbox['release_trust_summary']);
    final blockerCount = _asInt(releaseTrust['blocker_count']);
    if (blockerCount <= 0) {
      return const AutopilotAgentBenchReleaseTrustFocus();
    }

    final priority = _releaseTrustPriorityFromReadiness(readiness);
    if (_releaseTrustAgentScore(agent, priority) <= 0) {
      return const AutopilotAgentBenchReleaseTrustFocus();
    }

    final focusItem = _releaseTrustItemForAgent(
      agent,
      releaseTrust,
      inbox,
    );
    final groups = _asMap(releaseTrust['group_counts']);
    final category = _firstClean([
      focusItem['category'],
      focusItem['report_blocker_category'],
      releaseTrust['next_action_category'],
    ]);
    final groupLabel = _releaseTrustGroupLabel(groups, category);
    final rawHandoffLabel = _firstClean([
      focusItem['handoff_label'],
      releaseTrust['next_action_handoff_label'],
      inbox['next_action_handoff_label'],
    ]);
    final rawHandoffCopy = _firstClean([
      focusItem['handoff_copy'],
      releaseTrust['next_action_handoff_copy'],
      inbox['next_action_handoff_copy'],
    ]);
    final rawPrPublicationPacketLabel = _firstClean([
      focusItem['pr_publish_packet_label'],
      focusItem['report_pr_publish_packet_label'],
      releaseTrust['pr_publish_packet_label'],
      releaseTrust['report_pr_publish_packet_label'],
      inbox['pr_publish_packet_label'],
      inbox['report_pr_publish_packet_label'],
    ]);
    final rawPrPublicationPacketCopy = _firstClean([
      focusItem['pr_publish_packet_copy'],
      focusItem['report_pr_publish_packet_copy'],
      releaseTrust['pr_publish_packet_copy'],
      releaseTrust['report_pr_publish_packet_copy'],
      inbox['pr_publish_packet_copy'],
      inbox['report_pr_publish_packet_copy'],
    ]);
    final handoffLooksLikePrPublicationPacket =
        rawHandoffLabel.toLowerCase().contains('pr publication') ||
            rawHandoffCopy.contains(
              'Project Autopilot PR publication decision packet',
            );
    final prPublicationPacketCopy = rawPrPublicationPacketCopy.isNotEmpty
        ? rawPrPublicationPacketCopy
        : handoffLooksLikePrPublicationPacket
            ? rawHandoffCopy
            : '';
    final prPublicationPacketLabel = prPublicationPacketCopy.isEmpty
        ? ''
        : _firstClean([
            rawPrPublicationPacketLabel,
            handoffLooksLikePrPublicationPacket ? rawHandoffLabel : '',
            'Copy PR publication gate',
          ]);
    final actionLabel = _firstClean([
      prPublicationPacketLabel,
      focusItem['action_label'],
      focusItem['label'],
      releaseTrust['next_action_button_label'],
      releaseTrust['next_action_label'],
      inbox['next_action_button_label'],
      inbox['next_action_label'],
      rawHandoffLabel,
      'Review trust report',
    ]);
    final path = _firstClean([
      focusItem['path'],
      releaseTrust['next_action_path'],
      inbox['next_action_path'],
    ]);
    final openPath = _firstClean([
      focusItem['open_path'],
      releaseTrust['next_action_open_path'],
      inbox['next_action_open_path'],
    ]);
    final detail = _releaseTrustFocusDetail(
      focusItem,
      releaseTrust,
      inbox,
      path,
    );
    final gateLadder =
        AutopilotReleaseGateLadderPresentation.fromReleaseTrust(releaseTrust);
    return AutopilotAgentBenchReleaseTrustFocus(
      active: true,
      chipLabel: groupLabel,
      gateChipLabel: _releaseTrustGateChipLabel(gateLadder),
      gateSummaryLabel: gateLadder.hasSignal ? gateLadder.summaryLabel : '',
      gateDetail: _releaseTrustGateDetail(gateLadder),
      priorityLabel: AutopilotAgentBenchActivityPresenter._clip(
        'Release trust: $actionLabel',
        96,
      ),
      detail: detail,
      actionLabel: actionLabel,
      path: path,
      openPath: openPath,
      handoffLabel: _firstClean([
        prPublicationPacketLabel,
        rawHandoffLabel,
      ]),
      handoffCopy: _firstClean([
        prPublicationPacketCopy,
        rawHandoffCopy,
      ]),
      item: Map<String, dynamic>.from(focusItem),
    );
  }

  static String _releaseTrustGateChipLabel(
    AutopilotReleaseGateLadderPresentation ladder,
  ) {
    if (!ladder.hasSignal) return '';
    final blocked = ladder.steps.where((step) => step.blocked).length;
    if (blocked >= 3) return '3 gates blocked';
    if (blocked > 0) return '$blocked gate${blocked == 1 ? '' : 's'} blocked';
    return 'gates separate';
  }

  static String _releaseTrustGateDetail(
    AutopilotReleaseGateLadderPresentation ladder,
  ) {
    if (!ladder.hasSignal) return '';
    final steps = ladder.steps
        .map((step) => '${step.label} ${step.stateLabel}: ${step.detail}')
        .where((step) => step.trim().isNotEmpty);
    return AutopilotAgentBenchActivityPresenter._clip(
      [
        'Gate ladder',
        ladder.summaryLabel,
        ...steps,
      ].join(' | '),
      520,
    );
  }

  static int _compareAttentionFirst(
    Map<String, dynamic> left,
    Map<String, dynamic> right, {
    _BenchReleaseTrustPriority releaseTrust =
        const _BenchReleaseTrustPriority.empty(),
  }) {
    final leftActivity = fromAgent(left);
    final rightActivity = fromAgent(right);
    final leftTrustScore = _releaseTrustAgentScore(left, releaseTrust);
    final rightTrustScore = _releaseTrustAgentScore(right, releaseTrust);
    if (leftTrustScore != rightTrustScore) {
      return rightTrustScore.compareTo(leftTrustScore);
    }
    final leftBoardRank = leftActivity.boardPriorityRank;
    final rightBoardRank = rightActivity.boardPriorityRank;
    if (leftBoardRank > 0 &&
        rightBoardRank > 0 &&
        leftBoardRank != rightBoardRank) {
      return leftBoardRank.compareTo(rightBoardRank);
    }
    final scoreCompare =
        rightActivity.attentionScore.compareTo(leftActivity.attentionScore);
    if (scoreCompare != 0) return scoreCompare;
    return _agentSortLabel(left).compareTo(_agentSortLabel(right));
  }

  static _BenchReleaseTrustPriority _releaseTrustPriorityFromReadiness(
    Map<String, dynamic>? readiness,
  ) {
    final inbox = _asMap(readiness?['operator_inbox']);
    final releaseTrust = _asMap(inbox['release_trust_summary']);
    final blockerCount = _asInt(releaseTrust['blocker_count']);
    if (blockerCount <= 0) {
      return const _BenchReleaseTrustPriority.empty();
    }
    final groups = _asMap(releaseTrust['group_counts']);
    final releaseTrustCount = _asInt(groups['release_trust']);
    final prHealthCount = _asInt(groups['pr_health']);
    final baseScore = releaseTrustCount > 0
        ? 50000
        : prHealthCount > 0
            ? 42000
            : 36000;
    final targets = <String>[];
    void addTarget(Object? value) {
      final clean = _identityKey(value);
      if (clean.isNotEmpty && !targets.contains(clean)) {
        targets.add(clean);
      }
    }

    addTarget(releaseTrust['next_action_agent']);
    addTarget(inbox['next_action_agent']);
    addTarget(_agentLabelFromReportPath(releaseTrust['next_action_path']));
    addTarget(_agentLabelFromReportPath(releaseTrust['next_action_open_path']));
    addTarget(_agentLabelFromReportPath(inbox['next_action_path']));
    addTarget(_agentLabelFromReportPath(inbox['next_action_open_path']));
    for (final item in _asMapList(releaseTrust['items'])) {
      addTarget(item['agent']);
      addTarget(item['report_expedite_owner']);
      addTarget(_agentLabelFromReportPath(item['path']));
      addTarget(_agentLabelFromReportPath(item['open_path']));
    }
    final categoryTargets = _asMap(releaseTrust['category_targets']);
    for (final value in categoryTargets.values) {
      final target = _asMap(value);
      addTarget(target['agent']);
      addTarget(target['report_expedite_owner']);
      addTarget(_agentLabelFromReportPath(target['path']));
      addTarget(_agentLabelFromReportPath(target['open_path']));
    }
    return targets.isEmpty
        ? const _BenchReleaseTrustPriority.empty()
        : _BenchReleaseTrustPriority(
            score: baseScore + blockerCount,
            targets: targets,
          );
  }

  static int _releaseTrustAgentScore(
    Map<String, dynamic> agent,
    _BenchReleaseTrustPriority priority,
  ) {
    if (!priority.active) return 0;
    final labels = _releaseTrustAgentLabels(agent);
    if (labels.isEmpty) return 0;
    if (_releaseTrustLabelsMatchTargets(labels, priority.targets)) {
      return priority.score;
    }
    return 0;
  }

  static Map<String, dynamic> _releaseTrustItemForAgent(
    Map<String, dynamic> agent,
    Map<String, dynamic> releaseTrust,
    Map<String, dynamic> inbox,
  ) {
    final labels = _releaseTrustAgentLabels(agent);
    final candidates = <Map<String, dynamic>>[];
    final categoryTargets = _asMap(releaseTrust['category_targets']);
    for (final value in categoryTargets.values) {
      final target = _asMap(value);
      if (target.isNotEmpty) candidates.add(target);
    }
    candidates.addAll(_asMapList(releaseTrust['items']));
    final nextItem = {
      'agent': _clean(releaseTrust['next_action_agent']),
      'category': _clean(releaseTrust['next_action_category']),
      'path': _clean(releaseTrust['next_action_path']),
      'open_path': _clean(releaseTrust['next_action_open_path']),
      'label': _clean(releaseTrust['next_action_label']),
      'detail': _clean(releaseTrust['next_action_detail']),
      'action_label': _clean(releaseTrust['next_action_button_label']),
      'handoff_label': _clean(releaseTrust['next_action_handoff_label']),
      'handoff_copy': _clean(releaseTrust['next_action_handoff_copy']),
      'pr_publish_packet_label': _firstClean([
        releaseTrust['pr_publish_packet_label'],
        releaseTrust['report_pr_publish_packet_label'],
      ]),
      'pr_publish_packet_copy': _firstClean([
        releaseTrust['pr_publish_packet_copy'],
        releaseTrust['report_pr_publish_packet_copy'],
      ]),
    };
    if (nextItem.values.any((value) => value.toString().trim().isNotEmpty)) {
      candidates.add(nextItem);
    }

    for (final candidate in candidates) {
      final targets = _releaseTrustItemTargetLabels(candidate);
      if (_releaseTrustLabelsMatchTargets(labels, targets)) {
        return candidate;
      }
    }

    return {
      'agent': _firstClean([
        releaseTrust['next_action_agent'],
        inbox['next_action_agent'],
      ]),
      'category': _clean(releaseTrust['next_action_category']),
      'path': _firstClean([
        releaseTrust['next_action_path'],
        inbox['next_action_path'],
      ]),
      'open_path': _firstClean([
        releaseTrust['next_action_open_path'],
        inbox['next_action_open_path'],
      ]),
      'label': _firstClean([
        releaseTrust['next_action_label'],
        inbox['next_action_label'],
      ]),
      'detail': _firstClean([
        releaseTrust['next_action_detail'],
        inbox['next_action_detail'],
        releaseTrust['detail'],
      ]),
      'action_label': _firstClean([
        releaseTrust['next_action_button_label'],
        inbox['next_action_button_label'],
      ]),
      'handoff_label': _firstClean([
        releaseTrust['next_action_handoff_label'],
        inbox['next_action_handoff_label'],
      ]),
      'handoff_copy': _firstClean([
        releaseTrust['next_action_handoff_copy'],
        inbox['next_action_handoff_copy'],
      ]),
      'pr_publish_packet_label': _firstClean([
        releaseTrust['pr_publish_packet_label'],
        releaseTrust['report_pr_publish_packet_label'],
        inbox['pr_publish_packet_label'],
        inbox['report_pr_publish_packet_label'],
      ]),
      'pr_publish_packet_copy': _firstClean([
        releaseTrust['pr_publish_packet_copy'],
        releaseTrust['report_pr_publish_packet_copy'],
        inbox['pr_publish_packet_copy'],
        inbox['report_pr_publish_packet_copy'],
      ]),
    };
  }

  static List<String> _releaseTrustAgentLabels(Map<String, dynamic> agent) {
    final flow = _asMap(agent['agent_flow_pressure']);
    final expedite = _asMap(agent['expedite_lane_pressure']);
    final kpi = _asMap(agent['kpi_lane_pressure']);
    return [
      agent['name'],
      agent['profile_key'],
      agent['role'],
      agent['tier'],
      flow['agent'],
      expedite['top_owner'],
      kpi['current_signal'],
      expedite['current_signal'],
      flow['current_signal'],
    ].map(_identityKey).where((item) => item.isNotEmpty).toList();
  }

  static List<String> _releaseTrustItemTargetLabels(
    Map<String, dynamic> item,
  ) {
    final targets = [
      item['agent'],
      item['report_expedite_owner'],
      item['owner'],
      item['label'],
      item['report_blocker_label'],
      _agentLabelFromReportPath(item['path']),
      _agentLabelFromReportPath(item['open_path']),
    ].map(_identityKey).where((item) => item.isNotEmpty).toList();
    return targets.toSet().toList(growable: false);
  }

  static bool _releaseTrustLabelsMatchTargets(
    List<String> labels,
    List<String> targets,
  ) {
    if (labels.isEmpty || targets.isEmpty) return false;
    for (final target in targets) {
      if (labels.any((label) => _identityMatches(label, target))) {
        return true;
      }
    }
    return false;
  }

  static String _releaseTrustGroupLabel(
    Map<String, dynamic> groups,
    String category,
  ) {
    if (_asInt(groups['release_trust']) > 0 ||
        category == 'source_trust' ||
        category == 'queue_health' ||
        category == 'blocked') {
      return 'trust gate';
    }
    if (_asInt(groups['pr_health']) > 0 ||
        category == 'pr_health' ||
        category == 'ci_blocked') {
      return 'PR gate';
    }
    if (_asInt(groups['evidence_quality']) > 0 ||
        category == 'review_conflict' ||
        category == 'report_quality' ||
        category == 'evidence_policy' ||
        category == 'review_changes') {
      return 'evidence gate';
    }
    return 'release gate';
  }

  static String _releaseTrustFocusDetail(
    Map<String, dynamic> item,
    Map<String, dynamic> releaseTrust,
    Map<String, dynamic> inbox,
    String path,
  ) {
    final owner = _firstClean([
      item['agent'],
      item['report_expedite_owner'],
      releaseTrust['next_action_agent'],
      inbox['next_action_agent'],
    ]);
    final category = _firstClean([
      item['category'],
      item['report_blocker_category'],
      releaseTrust['next_action_category'],
    ]).replaceAll('_', ' ');
    final detail = _firstClean([
      item['detail'],
      item['report_next_action_detail'],
      item['reason'],
      releaseTrust['next_action_detail'],
      inbox['next_action_detail'],
      releaseTrust['detail'],
    ]);
    final pathTail = _pathTail(path);
    return AutopilotAgentBenchActivityPresenter._clip(
      [
        if (owner.isNotEmpty) 'Owner: $owner',
        if (category.isNotEmpty) category,
        if (detail.isNotEmpty) detail,
        if (pathTail.isNotEmpty) pathTail,
      ].join(' | '),
      240,
    );
  }

  static String _agentLabelFromReportPath(Object? value) {
    final clean = _clean(value).replaceAll('\\', '/');
    if (clean.isEmpty) return '';
    final parts =
        clean.split('/').where((part) => part.trim().isNotEmpty).toList();
    for (var index = 0; index < parts.length - 1; index += 1) {
      if (parts[index].toLowerCase() == 'project_ws') {
        return parts[index + 1];
      }
    }
    for (final part in parts) {
      final lower = part.toLowerCase();
      if (lower == 'agentops' ||
          lower == 'pm' ||
          lower == 'devops' ||
          lower == 'sswe' ||
          lower == 'qa' ||
          lower == 'sre' ||
          lower == 'risk' ||
          lower == 'frontend' ||
          lower == 'mlops' ||
          lower == 'sec') {
        return part;
      }
    }
    return '';
  }

  static String _pathTail(String value) {
    final clean = value.trim().replaceAll('\\', '/');
    if (clean.isEmpty) return '';
    final parts =
        clean.split('/').where((part) => part.trim().isNotEmpty).toList();
    if (parts.length <= 3) return parts.join('/');
    return parts.sublist(parts.length - 3).join('/');
  }

  static List<Map<String, dynamic>> _asMapList(Object? value) {
    if (value is Iterable) {
      return [
        for (final item in value)
          if (_asMap(item).isNotEmpty) _asMap(item),
      ];
    }
    return const <Map<String, dynamic>>[];
  }

  static String _identityKey(Object? value) {
    return _clean(value)
        .toLowerCase()
        .replaceAll(RegExp(r'[^a-z0-9]+'), ' ')
        .trim();
  }

  static String _firstClean(Iterable<Object?> values) {
    for (final value in values) {
      final clean = _clean(value);
      if (clean.isNotEmpty) return clean;
    }
    return '';
  }

  static Map<String, dynamic> _firstMap(Iterable<Object?> values) {
    for (final value in values) {
      final map = _asMap(value);
      if (map.isNotEmpty) return map;
    }
    return const <String, dynamic>{};
  }

  static String _statusKey(Object? value) =>
      _clean(value).toLowerCase().replaceAll(RegExp(r'[\s-]+'), '_');

  static bool _identityMatches(String label, String target) {
    if (label.isEmpty || target.isEmpty) return false;
    if (label == target || label.contains(target) || target.contains(label)) {
      return true;
    }
    final labelTokens =
        label.split(' ').where((token) => token.length > 2).toSet();
    final targetTokens =
        target.split(' ').where((token) => token.length > 2).toSet();
    return labelTokens.intersection(targetTokens).isNotEmpty;
  }

  static bool _isGoalReceiptIssue(String value) {
    final clean =
        _clean(value).toLowerCase().replaceAll(RegExp(r'[\s-]+'), '_');
    if (clean.isEmpty) return false;
    return clean.startsWith('missing_goal_') ||
        clean.startsWith('goal_') ||
        clean.contains('goal_receipt') ||
        clean.contains('pursuing_goal') ||
        clean.contains('active_goal_objective') ||
        clean.contains('completion_gate');
  }

  static bool _isPursuingGoalProofIssue(String value) {
    final clean =
        _clean(value).toLowerCase().replaceAll(RegExp(r'[\s-]+'), '_');
    if (clean.isEmpty) return false;
    return clean.contains('goal_evidence_unbound') ||
        clean.contains('goal_progress_overclaimed') ||
        clean.contains('goal_objective_mismatch') ||
        clean.contains('objective_tied_evidence') ||
        clean.contains('objective_proof') ||
        clean.contains('goal_proof') ||
        clean.contains('completion_gate_proof');
  }

  static bool _isPrPublicationReceiptIssue(String value) {
    final clean =
        _clean(value).toLowerCase().replaceAll(RegExp(r'[\s-]+'), '_');
    if (clean.isEmpty) return false;
    return clean == 'missing_pr_publication_receipt' ||
        clean.contains('pr_publication_receipt') ||
        clean.contains('pr_receipt_missing') ||
        clean.contains('pr_receipt_gate') ||
        clean.contains('current_head_pr_publication_receipt') ||
        clean.contains('current_head_check_receipt') ||
        clean.contains('current_head_pr_receipt') ||
        clean.contains('publish_ready') ||
        clean.contains('merge_ready');
  }

  static String _agentSortLabel(Map<String, dynamic> agent) {
    for (final key in ['name', 'profile_key', 'role']) {
      final value = _clean(agent[key]).toLowerCase();
      if (value.isNotEmpty) return value;
    }
    return '';
  }

  static Map<String, dynamic> _asMap(Object? value) {
    if (value is Map<String, dynamic>) return value;
    if (value is Map) return Map<String, dynamic>.from(value);
    return const <String, dynamic>{};
  }

  static int _asInt(Object? value) {
    if (value is int) return value < 0 ? 0 : value;
    if (value is num) return value < 0 ? 0 : value.toInt();
    final parsed = int.tryParse(_clean(value));
    if (parsed == null || parsed < 0) return 0;
    return parsed;
  }

  static int? _asNullableInt(Object? value) {
    if (value == null) return null;
    if (value is int) return value < 0 ? 0 : value;
    if (value is num) return value < 0 ? 0 : value.toInt();
    final clean = _clean(value);
    if (clean.isEmpty) return null;
    final parsed = int.tryParse(clean);
    if (parsed == null) return null;
    return parsed < 0 ? 0 : parsed;
  }

  static List<String> _asStringList(Object? value) {
    if (value is Iterable) {
      return value
          .map((item) => _clean(item))
          .where((item) => item.isNotEmpty)
          .toList(growable: false);
    }
    final clean = _clean(value);
    return clean.isEmpty ? const <String>[] : <String>[clean];
  }

  static List<String> _dedupeStrings(Iterable<String> values) {
    final out = <String>[];
    final seen = <String>{};
    for (final value in values) {
      final clean = value.trim();
      if (clean.isEmpty) continue;
      if (seen.add(clean.toLowerCase())) out.add(clean);
    }
    return out;
  }

  static String _machineLabel(Object? value) =>
      _clean(value).replaceAll('_', ' ');

  static String _benchmarkEvidenceHandoffCopy(
    Map<String, dynamic> codingBenchmark,
    Map<String, dynamic> scheduledQuality, {
    required List<String> gapLabels,
    required String nextAction,
    List<Map<String, dynamic>> recoveryRoutes = const <Map<String, dynamic>>[],
  }) {
    final provided = _firstClean([
      codingBenchmark['frontier_evidence_handoff_copy'],
      scheduledQuality['frontier_evidence_handoff_copy'],
    ]);
    if (provided.isNotEmpty) return provided;
    final gaps = <Map<String, dynamic>>[
      ..._asMapList(codingBenchmark['frontier_evidence_gaps']),
      ..._asMapList(scheduledQuality['frontier_evidence_gaps']),
    ];
    if (gaps.isEmpty &&
        gapLabels.isEmpty &&
        nextAction.trim().isEmpty &&
        recoveryRoutes.isEmpty) {
      return '';
    }
    final effectivePassRate = _firstClean([
      codingBenchmark['effective_pass_rate'],
      scheduledQuality['effective_pass_rate'],
      codingBenchmark['pass_rate'],
      scheduledQuality['pass_rate'],
    ]);
    final lines = <String>[
      'Project Autopilot frontier evidence proof packet',
      'Purpose: collect or verify the real evidence blocking Codex/Claude-class promotion readiness.',
      'Do not summarize this as promotion-ready until every listed proof gap has fresh evidence.',
      '',
      'Current benchmark evidence:',
      if (_firstClean([codingBenchmark['profile'], scheduledQuality['profile']])
          .isNotEmpty)
        'Profile: ${_firstClean([
              codingBenchmark['profile'],
              scheduledQuality['profile']
            ])}',
      if (_firstClean([
        codingBenchmark['promotion_status'],
        scheduledQuality['promotion_status'],
      ]).isNotEmpty)
        'Promotion status: ${_firstClean([
              codingBenchmark['promotion_status'],
              scheduledQuality['promotion_status'],
            ])}',
      if (effectivePassRate.isNotEmpty) 'Pass rate: $effectivePassRate',
      '',
      'Proof gaps to close:',
    ];
    if (gaps.isNotEmpty) {
      for (var index = 0; index < gaps.length; index += 1) {
        final gap = gaps[index];
        lines.add(
          '${index + 1}. ${_firstClean([
                gap['label'],
                gap['gate'],
                'frontier evidence'
              ])}',
        );
        if (_firstClean([gap['required']]).isNotEmpty) {
          lines.add('   Required: ${_firstClean([gap['required']])}');
        }
        if (_firstClean([gap['actual']]).isNotEmpty) {
          lines.add('   Actual: ${_firstClean([gap['actual']])}');
        }
        if (_firstClean([gap['path']]).isNotEmpty) {
          lines.add('   Evidence path: ${_firstClean([gap['path']])}');
        }
        if (_firstClean([gap['next_action']]).isNotEmpty) {
          lines.add('   Next action: ${_firstClean([gap['next_action']])}');
        }
      }
    } else {
      for (var index = 0; index < gapLabels.length; index += 1) {
        lines.add('${index + 1}. ${gapLabels[index]}');
      }
      if (nextAction.trim().isNotEmpty) {
        lines.add('Next action: ${nextAction.trim()}');
      }
    }
    if (recoveryRoutes.isNotEmpty) {
      lines.addAll([
        '',
        'Recovery routes:',
      ]);
      for (var index = 0; index < recoveryRoutes.length; index += 1) {
        final route = recoveryRoutes[index];
        final source = _firstClean([
          route['source_kind'],
          route['source'],
          'frontier source',
        ]);
        final action = _firstClean([
          route['action_label'],
          route['action'],
          'Import saved response',
        ]);
        lines.add('${index + 1}. $source: $action');
        if (_firstClean([route['response_staging_file']]).isNotEmpty) {
          lines.add(
            '   Save all-cases response to: ${route['response_staging_file']}',
          );
        }
        if (_firstClean([route['dry_run_command']]).isNotEmpty) {
          lines.add('   Dry-run import: ${route['dry_run_command']}');
        }
        if (_firstClean([route['all_cases_command']]).isNotEmpty) {
          lines.add('   All-cases import: ${route['all_cases_command']}');
        }
        if (_firstClean([route['single_case_fallback']]).isNotEmpty) {
          lines.add(
            '   Single-case fallback: ${route['single_case_fallback']}',
          );
        }
        if (_firstClean([route['validation_command']]).isNotEmpty) {
          lines.add('   Validate after import: ${route['validation_command']}');
        }
        if (_firstClean([route['publish_command']]).isNotEmpty) {
          lines.add(
            '   Publish when all sources are ready: ${route['publish_command']}',
          );
        }
        if (_firstClean([route['boundary']]).isNotEmpty) {
          lines.add('   Boundary: ${route['boundary']}');
        }
      }
    }
    lines.addAll([
      '',
      'Success criteria: report exact regenerated scorecard or manifest paths and hashes; do not rely on self-test fixtures.',
      'Permission boundary: evidence collection and verification only. This packet does not authorize source/test edits, runtime restart, Docker, database/migration, broker/API, PR mutation, commit, push, merge, release, deploy, route/model changes, or live-trading behavior.',
    ]);
    return lines.join('\n');
  }

  static List<Map<String, dynamic>> _benchmarkEvidenceRecoveryRoutes(
    Map<String, dynamic> intake,
  ) {
    final routes = <Map<String, dynamic>>[];
    void addRoute(Map<String, dynamic> route) {
      final sourceKind = _firstClean([route['source_kind'], route['source']]);
      final allCasesCommand = _firstClean([
        route['preflight_recovery_all_cases_command'],
        route['all_cases_response_import_command'],
        route['response_import_command'],
        route['all_cases_command'],
      ]);
      final dryRunCommand = _firstClean([
        route['preflight_recovery_dry_run_command'],
        route['dry_run_response_import_command'],
        route['dry_run_all_cases_command'],
        route['dry_run_command'],
      ]);
      if (sourceKind.isEmpty || allCasesCommand.isEmpty) return;
      final normalized = <String, dynamic>{
        'source_kind': sourceKind,
        'action_label': _firstClean([
          route['preflight_recovery_action_label'],
          route['action_label'],
          route['preflight_recovery_action'],
          route['action'],
          'Import saved $sourceKind response',
        ]),
        'response_staging_file': _firstClean([
          route['preflight_recovery_response_staging_file'],
          route['response_staging_file'],
        ]),
        'dry_run_command': dryRunCommand,
        'all_cases_command': allCasesCommand,
        'single_case_fallback': _firstClean([
          route['preflight_recovery_single_case_command'],
          route['preflight_recovery_single_case_fallback'],
          route['single_case_response_import_command'],
          route['single_case_fallback'],
        ]),
        'validation_command': _firstClean([
          route['preflight_recovery_validation_command'],
          route['validation_command'],
          route['post_import_validation_command'],
        ]),
        'publish_command': _firstClean([
          route['preflight_recovery_publish_command'],
          route['publish_command'],
          route['post_import_publish_command'],
        ]),
        'boundary': _firstClean([
          route['preflight_recovery_boundary'],
          route['permission_boundary'],
          route['boundary'],
        ]),
      };
      final key =
          '${normalized['source_kind']}|${normalized['all_cases_command']}';
      if (routes.any((item) =>
          '${item['source_kind']}|${item['all_cases_command']}' == key)) {
        return;
      }
      routes.add(normalized);
    }

    for (final source in _asMapList(intake['sources'])) {
      addRoute(source);
    }
    for (final route
        in _asMapList(intake['frontier_preflight_recovery_routes'])) {
      addRoute(route);
    }
    for (final route in _asMapList(intake['preflight_recovery_routes'])) {
      addRoute(route);
    }
    return routes;
  }

  static String _benchmarkEvidenceRecoveryDetail(
    List<Map<String, dynamic>> routes,
  ) {
    if (routes.isEmpty) return '';
    final parts = <String>[
      for (final route in routes.take(2))
        _firstClean([
          route['source_kind'],
          route['source'],
        ]),
    ].where((item) => item.isNotEmpty).toList(growable: false);
    final prefix = routes.length == 1
        ? '1 source has safe recovery'
        : '${routes.length} sources have safe recovery';
    final sources = parts.isEmpty ? '' : ': ${parts.join(', ')}';
    return '$prefix$sources';
  }

  static String _scoreLabel(Object? value) {
    if (value is num) {
      final numeric = value.toDouble();
      return numeric == numeric.roundToDouble()
          ? numeric.toInt().toString()
          : numeric.toStringAsFixed(1);
    }
    return _clean(value);
  }

  static String _compactLargeNumberLabel(Object? value) {
    final clean = _clean(value).replaceAll(',', '');
    if (clean.isEmpty) return '';
    final numeric = double.tryParse(clean);
    if (numeric == null || numeric <= 0) return '';
    if (numeric >= 1000000) {
      final millions = numeric / 1000000;
      final label = millions >= 10
          ? millions.toStringAsFixed(0)
          : millions.toStringAsFixed(1).replaceAll(RegExp(r'\.0$'), '');
      return '${label}M';
    }
    if (numeric >= 1000) {
      final thousands = numeric / 1000;
      final label = thousands >= 10
          ? thousands.toStringAsFixed(0)
          : thousands.toStringAsFixed(1).replaceAll(RegExp(r'\.0$'), '');
      return '${label}k';
    }
    return numeric.toStringAsFixed(0);
  }

  static String _clean(Object? value) => value?.toString().trim() ?? '';

  static String _stageLabel(String value) {
    switch (_clean(value).toLowerCase()) {
      case 'chatting':
      case 'chat':
        return 'chat';
      case 'queued':
        return 'queue';
      case 'repo_scan':
        return 'repo scan';
      case 'assign_roles':
        return 'agent routing';
      case 'architect_review':
        return 'architect review';
      case 'plan':
        return 'plan';
      case 'implement':
        return 'implementation';
      case 'validate':
        return 'validation';
      case 'merge':
        return 'merge safety';
      default:
        return _clean(value).replaceAll('_', ' ');
    }
  }

  static String _statusLabel(String value) =>
      _clean(value).replaceAll('_', ' ');

  static String _clip(String value, int maxLength) {
    final clean = _clean(value);
    if (clean.length <= maxLength) return clean;
    if (maxLength <= 3) return clean.substring(0, maxLength);
    return '${clean.substring(0, maxLength - 3).trimRight()}...';
  }

  static String _shortId(String value) {
    final clean = _clean(value);
    if (clean.length <= 8) return clean;
    return clean.substring(0, 8);
  }

  static String _permissionBoundaryDetail(String value) {
    for (final line in value.split('\n')) {
      final clean = _clean(line);
      final lower = clean.toLowerCase();
      if (lower.startsWith('permission boundary:') ||
          lower.startsWith('safety boundary:')) {
        return clean;
      }
    }
    return '';
  }

  static String _permissionBoundaryLabel(String value) {
    final detail = _permissionBoundaryDetail(value).toLowerCase();
    if (detail.isEmpty) return '';
    if (detail.contains('operator/control-plane required')) {
      return 'control-plane required';
    }
    if (detail.contains('runtime review only')) return 'runtime review only';
    if (detail.contains('recovery review only')) return 'recovery review only';
    if (detail.contains('readiness review only')) {
      return 'readiness review only';
    }
    if (detail.contains('goal containment only')) {
      return 'goal containment only';
    }
    if (detail.contains('source review only')) return 'source review only';
    if (detail.contains('approval-first guidance')) return 'approval-first';
    if (detail.startsWith('safety boundary:')) return 'safety boundary';
    return 'permission boundary';
  }
}

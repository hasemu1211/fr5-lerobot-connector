const VIEW_SCHEMA = "data_factory.operator_session_view.v2";
const INTENT_SCHEMA = "data_factory.operator_intent.v1";
const RESULT_SCHEMA = "data_factory.operator_intent_result.v1";
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
const FLOW = ["environment", "plan", "review", "execution", "results", "next"];
const CATALOG_AXES = ["workspace", "frame", "task", "object", "grasp", "start", "motion", "variant", "camera", "data_mode", "split"];
const WORKSPACE_CAPTURE_ROLES = [
  ["CENTER", "중심점", "새 좌표계의 원점"],
  ["X_REF", "X축 기준점", "CENTER에서 +X 방향"],
  ["Y_CHECK", "Y축 확인점", "계산된 +Y 방향 확인"],
];
const CAMERA_ROLES = [
  ["UP", "상단"],
  ["SIDE", "측면"],
  ["WRIST", "손목"],
  ["UNUSED", "사용 안 함"],
];
const CAMERA_ROLE_IDS = new Set(CAMERA_ROLES.map(([role]) => role));
const START_POSE_STATUSES = new Set(["CANDIDATE", "AVAILABLE", "QUALIFICATION_REQUIRED"]);
const YAW_SAMPLE_BINDING_FIELDS = [
  "schema_version", "yaw_sampling_profile_id", "yaw_sampling_profile_digest", "sampling_seed",
  "sample_identity_digest", "sample_rank", "design_size", "yaw_sample_quantile", "raw_yaw_deg",
  "source_object_yaw_deg", "canonical_object_yaw_deg", "grasp_yaw_deg",
  "yaw_equivalence_period_deg", "sample_origin", "binding_digest",
];
const STATE_SPACE_SLOT_FIELDS = [
  ...YAW_SAMPLE_BINDING_FIELDS,
  "state_space_design_profile_id", "state_space_design_profile_digest",
  "spatial_cell_index", "spatial_row", "spatial_column",
];
const ENUMS = {
  connection_state: new Set(["READY", "STALE", "RECONNECTING", "BLOCKED"]),
  effect_scope: new Set(["FAKE", "PHYSICAL"]),
  lifecycle_action: new Set(["AUTHOR_ONLY", "PLAN_ONLY", "LIVE_COLLECT"]),
  data_disposition: new Set(["TEST_ONLY", "PRODUCTION"]),
  authoring_mode: new Set(["ASSISTED", "DIRECT_EDIT"]),
  workflow_state: new Set(["PREPARING", "AUTHORING", "REVIEW_CAMPAIGN", "AWAITING_APPROVAL", "RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR", "REVIEW_RESULTS", "BLOCKED", "TERMINAL"]),
};

const connectionBanner = document.querySelector("#connection-banner");
const announcer = document.querySelector("#announcer");
const cancelButton = document.querySelector("#cancel-campaign");
let currentView;
let lastSession;
let lastRevision = -1;
let lastDigest;
let intentBusy = false;
let cancelPending = false;
let watchController;
let viewStale = false;
let viewController;
let recoveryTimer;
let recoveryAttempts = 0;
let disabledBeforeFailure;
let manualStep;
let renderedWorkflow;
let renderedWorkflowStep;

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);
const message = (group, key, fallback = "확인 필요") => MESSAGE_CATALOG[group]?.[key] ?? fallback;
const isPickPlace = (view) => view?.draft?.selection?.task === "pick_place";
const yawNear = (left, right) => Number.isFinite(left) && Number.isFinite(right)
  && Math.abs(left - right) <= 1e-9;
const spatialNodeCount = (view) => view.draft.requested_count + Number(isPickPlace(view));
const poseText = (pose) => `X ${pose.x_mm} · Y ${pose.y_mm} · ${pose.yaw_deg}°`;
const workspaceRoute = (view) => view?.draft?.workspace_route ?? [];
const workspaceName = (view, workspaceId) => catalogOption(view, "workspace", workspaceId)?.label ?? workspaceId;
const nextDirectPairIndex = (view) => {
  const pairs = view?.draft?.direct_pairs ?? [];
  const route = workspaceRoute(view);
  const mismatch = pairs.findIndex(
    (pair, index) => pair.place_id !== route[index]?.workspace_id,
  );
  return mismatch >= 0 ? mismatch : pairs.length;
};
const operatorToken = () => document.querySelector('meta[name="operator-token"]')?.content.trim() ?? "";
const tokenHeaders = () => {
  const token = operatorToken();
  if (!token) throw new Error("OPERATOR_TOKEN_MISSING");
  return {"X-Operator-Token": token};
};

function assertEnum(name, value) {
  if (!ENUMS[name].has(value)) throw new TypeError(`UNKNOWN_VIEW_ENUM:${name}:${value}`);
}

function assertObject(value, code) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(code);
}

function validateCatalog(catalog, draft) {
  assertObject(catalog, "CATALOG_INVALID");
  assertObject(catalog.axes, "CATALOG_INVALID");
  if (!DIGEST_PATTERN.test(catalog.compatibility_digest)) throw new TypeError("CATALOG_INVALID");
  assertObject(draft.selection, "DRAFT_SELECTION_INVALID");
  CATALOG_AXES.forEach((axis) => {
    const options = catalog.axes[axis];
    if (!Array.isArray(options) || !options.length) throw new TypeError(`CATALOG_AXIS_INVALID:${axis}`);
    const ids = new Set();
    options.forEach((option) => {
      assertObject(option, `CATALOG_OPTION_INVALID:${axis}`);
      if (typeof option.id !== "string" || !option.id || typeof option.label !== "string" || !option.label
          || typeof option.available !== "boolean" || option.available === false && (typeof option.reason !== "string" || !option.reason)
          || ids.has(option.id)) throw new TypeError(`CATALOG_OPTION_INVALID:${axis}`);
      ids.add(option.id);
    });
    const selected = options.find((option) => option.id === draft.selection[axis]);
    if (!selected) throw new TypeError(`DRAFT_SELECTION_INVALID:${axis}`);
  });
}

function validateView(value) {
  const view = unwrapViewEnvelope(value);
  if (!view || view.schema_version !== VIEW_SCHEMA) throw new TypeError("VIEW_SCHEMA_MISMATCH");
  if (typeof view.session_id !== "string" || !view.session_id || !Number.isInteger(view.revision) || view.revision < 0
      || !DIGEST_PATTERN.test(view.view_digest)) throw new TypeError("VIEW_IDENTITY_INVALID");
  assertEnum("connection_state", view.connection_state);
  assertEnum("effect_scope", view.effect_scope);
  assertEnum("lifecycle_action", view.lifecycle_action);
  assertEnum("data_disposition", view.data_disposition);
  assertObject(view.runtime, "RUNTIME_INVALID");
  assertEnum("workflow_state", view.runtime.workflow_state);
  if (view.runtime.motion !== undefined) {
    assertObject(view.runtime.motion, "RUNTIME_MOTION_INVALID");
    if (![
      "NOT_AUTHORIZED", "DISPATCHING", "ACTIVE", "PAUSED_AT_GATE",
      "ACTIVE_POST_RECORDING", "COMPLETE",
    ].includes(view.runtime.motion.status)
        || typeof view.runtime.motion.label !== "string"
        || !view.runtime.motion.label) throw new TypeError("RUNTIME_MOTION_INVALID");
  }
  assertObject(view.setup, "SETUP_INVALID");
  if (typeof view.setup.host_status !== "string" || !Array.isArray(view.setup.subsystems) || !view.setup.subsystems.length) throw new TypeError("SETUP_INVALID");
  view.setup.subsystems.forEach((item) => {
    assertObject(item, "SETUP_INVALID");
    if (typeof item.label !== "string" || !item.label || typeof item.status !== "string" || !item.status || typeof item.detail !== "string") throw new TypeError("SETUP_INVALID");
  });
  validateCameraSetup(view.camera_setup);
  validateStartPoseSetup(view.start_pose_setup);
  validateStateSpaceSummary(view.state_space_summary);
  validateSamplingProvenance(view.sampling_provenance);
  validateActiveEpisodePlan(view.active_episode_plan);
  assertObject(view.draft, "DRAFT_INVALID");
  if (view.motion_presets !== undefined) {
    if (!Array.isArray(view.motion_presets)) throw new TypeError("MOTION_PRESET_INVALID");
    view.motion_presets.forEach((item) => {
      if (!item || typeof item.id !== "string" || !item.id || !/^sha256:[a-f0-9]{64}$/.test(item.digest)
          || typeof item.purpose !== "string" || !["QUALIFIED", "QUALIFICATION_REQUIRED"].includes(item.status)
          || !item.phase_scaling || typeof item.phase_scaling !== "object") throw new TypeError("MOTION_PRESET_INVALID");
      Object.values(item.phase_scaling).forEach((scaling) => {
        if (!scaling || ![scaling.velocity_scaling, scaling.acceleration_scaling].every((n) => typeof n === "number" && n > 0 && n <= 0.1)) throw new TypeError("MOTION_PRESET_INVALID");
      });
    });
  }
  if (view.draft.motion_preset !== undefined && view.draft.motion_preset !== null) {
    const binding = view.draft.motion_preset;
    if (!binding || typeof binding !== "object" || Object.keys(binding).sort().join() !== "digest,id"
        || typeof binding.id !== "string" || !binding.id || !/^sha256:[a-f0-9]{64}$/.test(binding.digest)) throw new TypeError("MOTION_PRESET_INVALID");
  }
  assertEnum("authoring_mode", view.draft.authoring_mode);
  if (typeof view.draft.draft_id !== "string" || !view.draft.draft_id || !Number.isInteger(view.draft.requested_count)
      || view.draft.requested_count < 1 || view.draft.requested_count > 100 || !Number.isInteger(view.draft.repeat)
      || view.draft.repeat < 1 || !Number.isSafeInteger(view.draft.normalized_seed) || view.draft.normalized_seed < 0
      || !Array.isArray(view.draft.cells)) throw new TypeError("DRAFT_INVALID");
  validateCatalog(view.catalog, view.draft);
  const route = workspaceRoute(view);
  if (!Array.isArray(route) || route.length !== spatialNodeCount(view)
      || route.some((item) => !item || typeof item !== "object" || Array.isArray(item)
        || Object.keys(item).length !== 2 || typeof item.workspace_id !== "string" || !item.workspace_id
        || typeof item.frame_id !== "string" || !item.frame_id)
      || route[0].workspace_id !== view.draft.selection.workspace
      || route[0].frame_id !== view.draft.selection.frame
      || route.some((_item, index) => !directPoseDomain(view, index))) throw new TypeError("WORKSPACE_ROUTE_INVALID");
  const currentObject = view.draft.current_object_pose;
  if (!currentObject || typeof currentObject !== "object" || Array.isArray(currentObject)
      || currentObject.place_id !== view.draft.selection.workspace
      || ![currentObject.x_mm, currentObject.y_mm, currentObject.yaw_deg].every(Number.isFinite)) throw new TypeError("CURRENT_OBJECT_POSE_INVALID");
  const position = view.draft.object_position;
  if (position != null && (!position || typeof position !== "object"
      || !["AVAILABLE", "MISSING", "BLOCKED", "STALE"].includes(position.status)
      || position.status === "AVAILABLE" && (!position.pose
        || !["HUMAN", "ROBOT_RELEASE", "ROBOT_RELEASE_PROXY"].includes(position.source)
        || ![position.pose.x_mm, position.pose.y_mm, position.pose.yaw_deg].every(Number.isFinite)
        || position.pose.place_id !== currentObject.place_id
        || ["x_mm", "y_mm", "yaw_deg"].some((key) => position.pose[key] !== currentObject[key])))) throw new TypeError("OBJECT_POSITION_INVALID");
  if (view.draft.direct_poses !== undefined && (!Array.isArray(view.draft.direct_poses)
      || view.draft.direct_poses.some((pose) => !pose || typeof pose !== "object" || Array.isArray(pose)
        || pose.place_id !== view.draft.selection.workspace
        || ![pose.x_mm, pose.y_mm, pose.yaw_deg].every(Number.isFinite)))) throw new TypeError("DIRECT_POSES_INVALID");
  if (view.draft.direct_pairs !== undefined && (!Array.isArray(view.draft.direct_pairs)
      || view.draft.direct_pairs.length > spatialNodeCount(view)
      || view.draft.direct_pairs.some((pair, index, pairs) => !pair || typeof pair !== "object" || Array.isArray(pair)
        || !(typeof pair.start_pose_id === "string" && pair.start_pose_id
          || isPickPlace(view) && pair.start_pose_id === null && index === pairs.length - 1)
        || pair.place_id !== route[index]?.workspace_id
        || ![pair.x_mm, pair.y_mm, pair.yaw_deg].every(Number.isFinite)))) throw new TypeError("DIRECT_PAIRS_INVALID");
  if (view.draft.direct_pairs !== undefined && view.start_pose_setup) {
    const selectedStartIds = new Set(view.start_pose_setup.selected_start_pose_ids);
    if (view.draft.direct_pairs.some((pair) => pair.start_pose_id !== null && !selectedStartIds.has(pair.start_pose_id))) throw new TypeError("DIRECT_PAIRS_INVALID");
  }
  if (view.state_space_summary && (view.state_space_summary.planned_episode_count !== view.draft.requested_count
      || view.state_space_summary.object_position_count !== spatialNodeCount(view)
      || view.start_pose_setup && view.state_space_summary.selected_start_pose_count !== view.start_pose_setup.selected_start_pose_ids.length
      || view.state_space_summary.eligible_start_condition_pair_count !== view.state_space_summary.selected_start_pose_count * view.state_space_summary.catalog_eligible_condition_count
      || (view.state_space_summary.design_shape !== null
      && view.state_space_summary.per_workspace_target_episode_count !== view.state_space_summary.per_workspace_condition_count * view.draft.repeat)
      || (view.state_space_summary.design_shape !== null
      && view.state_space_summary.workspace_coverage.reduce((sum, item) => sum + item.planned_episode_count, 0) !== view.draft.requested_count))) throw new TypeError("STATE_SPACE_SUMMARY_INVALID");
  if (view.state_space_summary) {
    const shape = view.state_space_summary.design_shape;
    const design = view.sampling_provenance.state_space_design_profile;
    if ((shape === null) !== (design === null)
        || design && (shape.columns !== design.spatial_strata.columns
          || shape.rows !== design.spatial_strata.rows
          || shape.yaw_cdf_strata !== design.yaw_cdf_strata)) throw new TypeError("STATE_SPACE_SUMMARY_INVALID");
  }
  if (view.draft.selection.data_mode !== view.data_disposition) throw new TypeError("DATA_MODE_MISMATCH");
  const cellIds = new Set();
  view.draft.cells.forEach((cell) => {
    assertObject(cell, "DRAFT_CELL_INVALID");
    if (typeof cell.cell_id !== "string" || !cell.cell_id || cellIds.has(cell.cell_id)
        || ![cell.x_mm, cell.y_mm, cell.yaw_deg].every(Number.isFinite)
        || !Number.isInteger(cell.repeat) || cell.repeat < 1 || !Number.isInteger(cell.coverage_count) || cell.coverage_count < 0
        || !["AVAILABLE", "SELECTED", "PINNED", "BLOCKED"].includes(cell.selection_state)
        || !["ELIGIBLE", "BLOCKED"].includes(cell.eligibility_status)
        || !Array.isArray(cell.reason_codes) || cell.reason_codes.some((code) => typeof code !== "string" || !code)) throw new TypeError("DRAFT_CELL_INVALID");
    cellIds.add(cell.cell_id);
  });
  if (!Array.isArray(view.available_ops) || view.available_ops.some((op) => typeof op !== "string" || !op)
      || new Set(view.available_ops).size !== view.available_ops.length) throw new TypeError("AVAILABLE_OPS_INVALID");
  if (view.runtime.workflow_state === "REVIEW_CAMPAIGN") {
    assertObject(view.campaign_envelope, "CAMPAIGN_ENVELOPE_INVALID");
    if (!DIGEST_PATTERN.test(view.campaign_envelope.manifest_digest) || !DIGEST_PATTERN.test(view.campaign_envelope.envelope_digest)
        || view.campaign_envelope.episode_count !== view.draft.requested_count) throw new TypeError("CAMPAIGN_ENVELOPE_INVALID");
  }
  const gripperTuning = view.campaign_review?.gripper_tuning;
  if (gripperTuning !== undefined) {
    assertObject(gripperTuning, "GRIPPER_TUNING_INVALID");
    const candidate = gripperTuning.status === "CANDIDATE_PENDING_HIL";
    const qualified = gripperTuning.status === "QUALIFIED_PROFILE";
    if (!DIGEST_PATTERN.test(gripperTuning.retune_digest)
        || !candidate && !qualified
        || !["TEST_ONLY", "PRODUCTION"].includes(gripperTuning.data_disposition)
        || candidate && gripperTuning.data_disposition !== "TEST_ONLY"
        || gripperTuning.production_authority !== (gripperTuning.data_disposition === "PRODUCTION")
        || gripperTuning.training_authority !== false
        || !Number.isFinite(gripperTuning.command_percent)
        || !Number.isFinite(gripperTuning.acceptable_feedback_percent?.min)
        || !Number.isFinite(gripperTuning.acceptable_feedback_percent?.max)) throw new TypeError("GRIPPER_TUNING_INVALID");
  }
  if (view.runtime.workflow_state === "RUNNING" && !view.available_ops.includes("cancel_session")) throw new TypeError("RUNNING_CANCEL_UNAVAILABLE");
  if (view.episode_history !== undefined && (!Array.isArray(view.episode_history)
      || view.episode_history.some((item) => !item || typeof item !== "object" || Array.isArray(item)
        || item.result_digest !== undefined && !DIGEST_PATTERN.test(item.result_digest)))) throw new TypeError("EPISODE_HISTORY_INVALID");
  const sequence = view.coverage?.sequence;
  if (sequence !== undefined) {
    if (!Array.isArray(sequence)
        || sequence.length === 0 && view.campaign_envelope
        || sequence.length > 0 && sequence.length !== view.draft.requested_count) throw new TypeError("COVERAGE_SEQUENCE_INVALID");
    sequence.forEach((item, index) => {
      validateObjectReposition(item?.object_reposition);
      validateStateSpaceSlot(item?.state_space_slot, view.sampling_provenance?.state_space_design_profile);
      const destination = item?.destination_pose;
      const sourceEndpoint = route[index];
      const destinationEndpoint = route[index + 1];
      const instructionFields = [
        item?.instruction,
        item?.episode_instruction_binding_digest,
        item?.task_binding_digest,
      ];
      const hasInstruction = instructionFields.some((value) => value !== undefined);
      if (!item || typeof item !== "object" || Array.isArray(item) || item.order_index !== index + 1
          || item.place_id !== sourceEndpoint?.workspace_id
          || item.start_pose_id !== undefined && (typeof item.start_pose_id !== "string" || !item.start_pose_id)
          || ![item.x_mm, item.y_mm, item.yaw_deg].every(Number.isFinite)
          || !DIGEST_PATTERN.test(item.coverage_condition_digest)
          || item.state_space_slot !== null && item.state_space_slot !== undefined
            && !yawNear(item.state_space_slot.source_object_yaw_deg, item.yaw_deg)
          || view.draft.authoring_mode === "ASSISTED"
            && view.sampling_provenance?.state_space_design_profile
            && !item.state_space_slot
          || hasInstruction && (typeof item.instruction !== "string" || !item.instruction.trim()
            || !DIGEST_PATTERN.test(item.episode_instruction_binding_digest)
            || !DIGEST_PATTERN.test(item.task_binding_digest))
          || isPickPlace(view) !== (destination !== undefined)
          || destination !== undefined && (!destination || typeof destination !== "object" || Array.isArray(destination)
            || destination.place_id !== destinationEndpoint?.workspace_id
            || ![destination.x_mm, destination.y_mm, destination.yaw_deg].every(Number.isFinite)
            || !DIGEST_PATTERN.test(item.task_binding_digest))) throw new TypeError("COVERAGE_SEQUENCE_INVALID");
    });
  }
  validateActiveEpisodePlanCoherence(view);
  validateRuntimeRepositionEvidence(view);
  if (view.candidate_review !== undefined && view.candidate_review !== null) {
    assertObject(view.candidate_review, "CANDIDATE_REVIEW_INVALID");
    if (!DIGEST_PATTERN.test(view.candidate_review.review_binding_digest)
        || !["PENDING", "PASS", "FAIL", "UNCERTAIN", "UNAVAILABLE"].includes(view.candidate_review.status)
        || !Array.isArray(view.candidate_review.choices) || !view.candidate_review.choices.every((choice) => ["PASS", "FAIL", "UNCERTAIN"].includes(choice))
        || view.candidate_review.episode_number !== undefined && (!Number.isInteger(view.candidate_review.episode_number) || view.candidate_review.episode_number < 1)
        || view.candidate_review.queue_remaining !== undefined && (!Number.isInteger(view.candidate_review.queue_remaining) || view.candidate_review.queue_remaining < 1)) throw new TypeError("CANDIDATE_REVIEW_INVALID");
  }
  if (view.home_recovery !== undefined && view.home_recovery !== null) {
    assertObject(view.home_recovery, "HOME_RECOVERY_INVALID");
    if (view.home_recovery.schema_version !== "data_factory.home_recovery.v1"
        || !["HOME", "ALREADY_HOME"].includes(view.home_recovery.status)
        || view.home_recovery.gripper_open !== true
        || ![0, 1].includes(view.home_recovery.arm_goal_count)) throw new TypeError("HOME_RECOVERY_INVALID");
  }
  validateWorkspaceRegistration(view.workspace_registration);
  if (view.runtime.progress != null && (!Number.isFinite(view.runtime.progress) || view.runtime.progress < 0 || view.runtime.progress > 100)) throw new TypeError("RUNTIME_PROGRESS_INVALID");
  if (lastSession === view.session_id && (view.revision < lastRevision
      || view.revision === lastRevision && lastDigest && lastDigest !== view.view_digest)) throw new TypeError("VIEW_REVISION_ROLLBACK");
  return view;
}

function validateStartPoseSetup(setup) {
  if (setup === undefined || setup === null) return;
  assertObject(setup, "START_POSE_SETUP_INVALID");
  if (!Array.isArray(setup.profiles) || !Array.isArray(setup.selected_start_pose_ids)
      || new Set(setup.selected_start_pose_ids).size !== setup.selected_start_pose_ids.length) throw new TypeError("START_POSE_SETUP_INVALID");
  const ids = new Set();
  setup.profiles.forEach((profile) => {
    assertObject(profile, "START_POSE_PROFILE_INVALID");
    if (typeof profile.start_pose_id !== "string" || !profile.start_pose_id || ids.has(profile.start_pose_id)
        || typeof profile.display_name !== "string" || !profile.display_name.trim()
        || !START_POSE_STATUSES.has(profile.status)
        || profile.reason !== undefined && typeof profile.reason !== "string") throw new TypeError("START_POSE_PROFILE_INVALID");
    ids.add(profile.start_pose_id);
  });
  if (setup.selected_start_pose_ids.some((id) => !ids.has(id)
      || setup.profiles.find((profile) => profile.start_pose_id === id)?.status !== "AVAILABLE")) throw new TypeError("START_POSE_SELECTION_INVALID");
}

function validateStateSpaceSummary(summary) {
  if (summary === undefined || summary === null) return;
  assertObject(summary, "STATE_SPACE_SUMMARY_INVALID");
  const fields = [
    "selected_start_pose_count", "catalog_eligible_condition_count", "eligible_start_condition_pair_count",
    "design_shape", "per_workspace_condition_count", "per_workspace_target_episode_count",
    "planned_episode_count", "object_position_count", "workspace_coverage", "full_coverage_episode_count",
  ];
  if (Object.keys(summary).length !== fields.length
      || ["selected_start_pose_count", "catalog_eligible_condition_count", "eligible_start_condition_pair_count", "planned_episode_count", "object_position_count"].some((field) => !Number.isInteger(summary[field]) || summary[field] < 0)
      || !Array.isArray(summary.workspace_coverage)) throw new TypeError("STATE_SPACE_SUMMARY_INVALID");
  if (summary.design_shape === null) {
    if (summary.per_workspace_condition_count !== null || summary.per_workspace_target_episode_count !== null
        || summary.full_coverage_episode_count !== null || summary.workspace_coverage.length) throw new TypeError("STATE_SPACE_SUMMARY_INVALID");
    return;
  }
  assertObject(summary.design_shape, "STATE_SPACE_SUMMARY_INVALID");
  const shapeFields = ["columns", "rows", "yaw_cdf_strata"];
  if (Object.keys(summary.design_shape).length !== shapeFields.length
      || shapeFields.some((field) => !Number.isInteger(summary.design_shape[field]) || summary.design_shape[field] < 1)
      || !Number.isInteger(summary.per_workspace_condition_count) || summary.per_workspace_condition_count < 1
      || summary.per_workspace_condition_count !== summary.design_shape.columns * summary.design_shape.rows * summary.design_shape.yaw_cdf_strata
      || !Number.isInteger(summary.per_workspace_target_episode_count) || summary.per_workspace_target_episode_count < summary.per_workspace_condition_count
      || !Number.isInteger(summary.full_coverage_episode_count) || summary.full_coverage_episode_count < summary.per_workspace_target_episode_count
      || !summary.workspace_coverage.length) throw new TypeError("STATE_SPACE_SUMMARY_INVALID");
  const endpoints = new Set();
  summary.workspace_coverage.forEach((item) => {
    assertObject(item, "STATE_SPACE_SUMMARY_INVALID");
    const key = `${item.workspace_id}\u0000${item.frame_id}`;
    if (Object.keys(item).length !== 4 || typeof item.workspace_id !== "string" || !item.workspace_id
        || typeof item.frame_id !== "string" || !item.frame_id || endpoints.has(key)
        || !Number.isInteger(item.planned_episode_count) || item.planned_episode_count < 0
        || item.full_coverage_episode_count !== summary.per_workspace_target_episode_count) throw new TypeError("STATE_SPACE_SUMMARY_INVALID");
    endpoints.add(key);
  });
  if (summary.full_coverage_episode_count !== summary.workspace_coverage.length * summary.per_workspace_target_episode_count) throw new TypeError("STATE_SPACE_SUMMARY_INVALID");
}

function validateSamplingProvenance(provenance) {
  assertObject(provenance, "SAMPLING_PROVENANCE_INVALID");
  const fields = ["object_dimensions_mm", "yaw_sampling_profile", "state_space_design_profile", "approach_sampling_profile"];
  if (Object.keys(provenance).length !== fields.length
      || fields.some((field) => !(field in provenance))) throw new TypeError("SAMPLING_PROVENANCE_INVALID");
  const dimensions = provenance.object_dimensions_mm;
  if (dimensions !== null && (!Array.isArray(dimensions) || dimensions.length !== 3
      || dimensions.some((value) => !Number.isFinite(value) || value <= 0))) throw new TypeError("SAMPLING_PROVENANCE_INVALID");
  validateSamplingProfile(provenance.yaw_sampling_profile, false);
  validateStateSpaceDesignProfile(provenance.state_space_design_profile, provenance.yaw_sampling_profile);
  validateSamplingProfile(provenance.approach_sampling_profile, true);
}

function validateSamplingProfile(profile, approach) {
  if (profile === null) return;
  assertObject(profile, "SAMPLING_PROFILE_INVALID");
  const fields = approach
    ? ["approach_sampling_profile_id", "profile_digest", "parameter_distribution", "required_camera_roles"]
    : ["yaw_sampling_profile_id", "profile_digest", "distribution", "canonical_interval_deg", "required_camera_roles"];
  const identifier = profile[fields[0]];
  const distribution = profile[fields[2]];
  const roles = profile.required_camera_roles;
  if (Object.keys(profile).length !== fields.length || fields.some((field) => !(field in profile))
      || typeof identifier !== "string" || !identifier || !DIGEST_PATTERN.test(profile.profile_digest)
      || !distribution || typeof distribution !== "object" || Array.isArray(distribution)
      || !Array.isArray(roles) || new Set(roles).size !== roles.length
      || roles.some((role) => typeof role !== "string" || !role)) throw new TypeError("SAMPLING_PROFILE_INVALID");
  if (!approach) {
    const interval = profile.canonical_interval_deg;
    if (Object.keys(distribution).length !== 1 || distribution.kind !== "STRATIFIED_UNIFORM"
        || !interval || typeof interval !== "object" || Array.isArray(interval)
        || Object.keys(interval).length !== 2
        || !("minimum" in interval) || !("maximum_exclusive" in interval)
        || !Number.isFinite(interval.minimum) || !Number.isFinite(interval.maximum_exclusive)
        || interval.minimum >= interval.maximum_exclusive) throw new TypeError("SAMPLING_PROFILE_INVALID");
    return;
  }
  const clearance = distribution.align_clearance_m;
  const offset = distribution.view_offset_xy_m;
  const distributionFields = ["kind", "align_clearance_m", "view_offset_xy_m"];
  const clearanceFields = ["kind", "mean", "standard_deviation", "minimum", "maximum"];
  const offsetFields = ["kind", "object_axes", "maximum_radius_fraction", "absolute_maximum_radius_m", "mahalanobis_radius"];
  if (Object.keys(distribution).length !== distributionFields.length
      || distributionFields.some((field) => !(field in distribution))
      || distribution.kind !== "STRATIFIED_BOUNDED"
      || !clearance || typeof clearance !== "object" || Array.isArray(clearance)
      || Object.keys(clearance).length !== clearanceFields.length
      || clearanceFields.some((field) => !(field in clearance))
      || clearance.kind !== "TRUNCATED_NORMAL"
      || ![clearance.mean, clearance.standard_deviation, clearance.minimum, clearance.maximum].every(Number.isFinite)
      || clearance.standard_deviation <= 0 || clearance.minimum >= clearance.maximum
      || clearance.mean < clearance.minimum || clearance.mean > clearance.maximum
      || !offset || typeof offset !== "object" || Array.isArray(offset)
      || Object.keys(offset).length !== offsetFields.length
      || offsetFields.some((field) => !(field in offset))
      || offset.kind !== "OBJECT_RELATIVE_TRUNCATED_BIVARIATE_NORMAL"
      || !Array.isArray(offset.object_axes) || offset.object_axes.length !== 2
      || offset.object_axes[0] !== "X" || offset.object_axes[1] !== "Y"
      || !Number.isFinite(offset.maximum_radius_fraction) || offset.maximum_radius_fraction <= 0
      || !Number.isFinite(offset.absolute_maximum_radius_m) || offset.absolute_maximum_radius_m <= 0
      || !Number.isFinite(offset.mahalanobis_radius) || offset.mahalanobis_radius <= 0) throw new TypeError("SAMPLING_PROFILE_INVALID");
}

function validateStateSpaceDesignProfile(profile, yawProfile) {
  if (profile === null) return;
  assertObject(profile, "STATE_SPACE_DESIGN_PROFILE_INVALID");
  const fields = [
    "state_space_design_profile_id", "profile_digest", "object_profile_id", "object_profile_digest",
    "grasp_profile_id", "grasp_profile_digest", "yaw_sampling_profile_id", "yaw_sampling_profile_digest",
    "spatial_strata", "yaw_cdf_strata", "derived_yaw_cdf_tiers", "assignment", "execution_order",
    "initial_source_policy", "per_workspace_repeat_one_sweep_episode_count",
    "full_cell_yaw_coverage_sweeps",
    "per_workspace_repeat_one_full_cell_yaw_coverage_episode_count",
  ];
  const identifiers = [profile.state_space_design_profile_id, profile.object_profile_id, profile.grasp_profile_id, profile.yaw_sampling_profile_id];
  const digests = [profile.profile_digest, profile.object_profile_digest, profile.grasp_profile_digest, profile.yaw_sampling_profile_digest];
  const spatial = profile.spatial_strata;
  if (Object.keys(profile).length !== fields.length || fields.some((field) => !(field in profile))
      || identifiers.some((value) => typeof value !== "string" || !value)
      || digests.some((value) => !DIGEST_PATTERN.test(value))
      || !yawProfile || profile.yaw_sampling_profile_id !== yawProfile.yaw_sampling_profile_id
      || profile.yaw_sampling_profile_digest !== yawProfile.profile_digest
      || !spatial || typeof spatial !== "object" || Array.isArray(spatial)
      || Object.keys(spatial).length !== 2 || !("columns" in spatial) || !("rows" in spatial)
      || !Number.isInteger(spatial.columns) || !Number.isInteger(spatial.rows)
      || spatial.columns < 1 || spatial.rows < 1 || spatial.columns * spatial.rows > 100
      || !Number.isInteger(profile.yaw_cdf_strata) || profile.yaw_cdf_strata < 1
      || profile.yaw_cdf_strata > spatial.columns * spatial.rows
      || profile.assignment !== "ROTATING_BALANCED_FRACTIONAL_FACTORIAL"
      || profile.execution_order !== "CONTIGUOUS_YAW_BLOCKS"
      || profile.initial_source_policy !== "CONDITION_ON_OBSERVED_SOURCE"
      || profile.per_workspace_repeat_one_sweep_episode_count !== spatial.columns * spatial.rows
      || profile.full_cell_yaw_coverage_sweeps !== profile.yaw_cdf_strata
      || profile.per_workspace_repeat_one_full_cell_yaw_coverage_episode_count
        !== profile.per_workspace_repeat_one_sweep_episode_count * profile.yaw_cdf_strata
      || !Array.isArray(profile.derived_yaw_cdf_tiers)
      || profile.derived_yaw_cdf_tiers.length !== profile.yaw_cdf_strata) throw new TypeError("STATE_SPACE_DESIGN_PROFILE_INVALID");
  const interval = yawProfile.canonical_interval_deg;
  profile.derived_yaw_cdf_tiers.forEach((tier, rank, tiers) => {
    assertObject(tier, "STATE_SPACE_DESIGN_PROFILE_INVALID");
    const quantile = tier.quantile;
    const yaw = tier.yaw_deg;
    if (Object.keys(tier).length !== 3 || !("sample_rank" in tier) || !("quantile" in tier) || !("yaw_deg" in tier)
        || tier.sample_rank !== rank || !quantile || typeof quantile !== "object" || Array.isArray(quantile)
        || Object.keys(quantile).length !== 2 || quantile.minimum !== rank / tiers.length
        || quantile.maximum_exclusive !== (rank + 1) / tiers.length
        || !yaw || typeof yaw !== "object" || Array.isArray(yaw) || Object.keys(yaw).length !== 2
        || !Number.isFinite(yaw.minimum) || !Number.isFinite(yaw.maximum_exclusive)
        || yaw.minimum >= yaw.maximum_exclusive
        || yaw.minimum !== (rank === 0 ? interval.minimum : tiers[rank - 1].yaw_deg.maximum_exclusive)
        || rank === tiers.length - 1 && yaw.maximum_exclusive !== interval.maximum_exclusive) throw new TypeError("STATE_SPACE_DESIGN_PROFILE_INVALID");
  });
}

function validateObjectReposition(reposition) {
  if (reposition === undefined || reposition === null) return;
  assertObject(reposition, "OBJECT_REPOSITION_INVALID");
  const fields = ["parent_run_id", "continuation_run_id", "next_run_id", "execution_stage", "recording_scope", "start_state", "source_pose", "target_pose", "yaw_sample", "object_profile_id", "object_profile_digest", "grasp_profile_id", "grasp_profile_digest", "yaw_sampling_profile_id", "yaw_sampling_profile_digest", "motion_recipe", "recorder_authorized", "dataset_write_authorized", "binding_digest"];
  const poseValid = (pose) => pose && typeof pose === "object" && !Array.isArray(pose)
    && typeof pose.place_id === "string" && pose.place_id
    && [pose.x_mm, pose.y_mm, pose.yaw_deg].every(Number.isFinite);
  if (Object.keys(reposition).length !== fields.length || fields.some((field) => !(field in reposition))
      || typeof reposition.parent_run_id !== "string" || !reposition.parent_run_id
      || typeof reposition.continuation_run_id !== "string" || !reposition.continuation_run_id
      || reposition.next_run_id !== null && (typeof reposition.next_run_id !== "string" || !reposition.next_run_id)
      || reposition.execution_stage !== (reposition.start_state === "HELD_OBJECT" ? "PRECOMMIT_POST_RECORDING" : reposition.start_state === "ON_SURFACE" ? "POSTCOMMIT" : null)
      || reposition.recording_scope !== "OUT_OF_DATASET"
      || !["HELD_OBJECT", "ON_SURFACE"].includes(reposition.start_state)
      || !poseValid(reposition.source_pose) || !poseValid(reposition.target_pose)
      || typeof reposition.object_profile_id !== "string" || !reposition.object_profile_id
      || !DIGEST_PATTERN.test(reposition.object_profile_digest)
      || typeof reposition.grasp_profile_id !== "string" || !reposition.grasp_profile_id
      || !DIGEST_PATTERN.test(reposition.grasp_profile_digest)
      || (reposition.yaw_sampling_profile_id === null) !== (reposition.yaw_sampling_profile_digest === null)
      || reposition.yaw_sampling_profile_id !== null && (typeof reposition.yaw_sampling_profile_id !== "string" || !reposition.yaw_sampling_profile_id || !DIGEST_PATTERN.test(reposition.yaw_sampling_profile_digest))
      || reposition.motion_recipe !== "DIRECT" || reposition.recorder_authorized !== false
      || reposition.dataset_write_authorized !== false || !DIGEST_PATTERN.test(reposition.binding_digest)) throw new TypeError("OBJECT_REPOSITION_INVALID");
  if (reposition.yaw_sample !== null) {
    const yaw = reposition.yaw_sample;
    const yawFields = ["yaw_sampling_profile_id", "yaw_sampling_profile_digest", "sampling_seed", "sample_rank", "design_size", "source_object_yaw_deg", "binding_digest"];
    if (!yaw || typeof yaw !== "object" || Array.isArray(yaw)
        || Object.keys(yaw).length !== yawFields.length || yawFields.some((field) => !(field in yaw))
        || typeof yaw.yaw_sampling_profile_id !== "string" || !yaw.yaw_sampling_profile_id
        || !DIGEST_PATTERN.test(yaw.yaw_sampling_profile_digest) || !DIGEST_PATTERN.test(yaw.binding_digest)
        || typeof yaw.sampling_seed !== "string" || !/^(0|[1-9]\d{0,19})$/.test(yaw.sampling_seed)
        || BigInt(yaw.sampling_seed) > 18446744073709551615n
        || !Number.isInteger(yaw.sample_rank) || !Number.isInteger(yaw.design_size)
        || yaw.design_size < 1 || yaw.sample_rank < 0 || yaw.sample_rank >= yaw.design_size
        || yaw.yaw_sampling_profile_id !== reposition.yaw_sampling_profile_id
        || yaw.yaw_sampling_profile_digest !== reposition.yaw_sampling_profile_digest
        || !Number.isFinite(yaw.source_object_yaw_deg)) throw new TypeError("OBJECT_REPOSITION_INVALID");
  } else if (reposition.yaw_sampling_profile_id !== null) {
    throw new TypeError("OBJECT_REPOSITION_INVALID");
  }
}

function validateYawSampleBinding(yaw, requireStateSpaceSlot = false) {
  assertObject(yaw, "YAW_SAMPLE_BINDING_INVALID");
  const stateSpaceSlot = yaw.schema_version === "data_factory.yaw_sample_binding.v4";
  const fields = stateSpaceSlot ? STATE_SPACE_SLOT_FIELDS : YAW_SAMPLE_BINDING_FIELDS;
  if (Object.keys(yaw).length !== fields.length || fields.some((field) => !(field in yaw))
      || !["data_factory.yaw_sample_binding.v3", "data_factory.yaw_sample_binding.v4"].includes(yaw.schema_version)
      || requireStateSpaceSlot && !stateSpaceSlot
      || typeof yaw.yaw_sampling_profile_id !== "string" || !yaw.yaw_sampling_profile_id
      || !DIGEST_PATTERN.test(yaw.yaw_sampling_profile_digest)
      || !DIGEST_PATTERN.test(yaw.sample_identity_digest) || !DIGEST_PATTERN.test(yaw.binding_digest)
      || typeof yaw.sampling_seed !== "string" || !/^(0|[1-9]\d{0,19})$/.test(yaw.sampling_seed)
      || BigInt(yaw.sampling_seed) > 18446744073709551615n
      || !Number.isInteger(yaw.sample_rank) || !Number.isInteger(yaw.design_size)
      || yaw.design_size < 1 || yaw.sample_rank < 0 || yaw.sample_rank >= yaw.design_size
      || !Number.isFinite(yaw.yaw_sample_quantile) || yaw.yaw_sample_quantile < yaw.sample_rank / yaw.design_size
      || yaw.yaw_sample_quantile >= (yaw.sample_rank + 1) / yaw.design_size
      || ![yaw.raw_yaw_deg, yaw.source_object_yaw_deg, yaw.grasp_yaw_deg, yaw.yaw_equivalence_period_deg].every(Number.isFinite)
      || !Number.isFinite(yaw.canonical_object_yaw_deg)
      || !yawNear(yaw.source_object_yaw_deg, yaw.grasp_yaw_deg)
      || yaw.yaw_equivalence_period_deg <= 0
      || (!yawNear(yaw.raw_yaw_deg, yaw.source_object_yaw_deg)
        || Math.abs((yaw.source_object_yaw_deg - yaw.canonical_object_yaw_deg) / yaw.yaw_equivalence_period_deg
          - Math.round((yaw.source_object_yaw_deg - yaw.canonical_object_yaw_deg) / yaw.yaw_equivalence_period_deg)) > 1e-9)
      || !["SEEDED_CDF_STRATUM", "CONDITIONED_SOURCE_ANCHOR"].includes(yaw.sample_origin)) throw new TypeError("YAW_SAMPLE_BINDING_INVALID");
  if (stateSpaceSlot && (typeof yaw.state_space_design_profile_id !== "string" || !yaw.state_space_design_profile_id
      || !DIGEST_PATTERN.test(yaw.state_space_design_profile_digest)
      || !Number.isInteger(yaw.spatial_cell_index) || yaw.spatial_cell_index < 0
      || !Number.isInteger(yaw.spatial_row) || yaw.spatial_row < 0
      || !Number.isInteger(yaw.spatial_column) || yaw.spatial_column < 0)) throw new TypeError("YAW_SAMPLE_BINDING_INVALID");
}

function validateStateSpaceSlot(slot, design) {
  if (slot === undefined || slot === null) return;
  assertObject(slot, "STATE_SPACE_SLOT_INVALID");
  validateYawSampleBinding(slot, true);
  if (!design || slot.state_space_design_profile_id !== design.state_space_design_profile_id
      || slot.state_space_design_profile_digest !== design.profile_digest
      || slot.spatial_row < 0 || slot.spatial_row >= design.spatial_strata.rows
      || slot.spatial_column < 0 || slot.spatial_column >= design.spatial_strata.columns
      || slot.spatial_cell_index !== slot.spatial_row * design.spatial_strata.columns + slot.spatial_column) throw new TypeError("STATE_SPACE_SLOT_INVALID");
  const tier = design.derived_yaw_cdf_tiers[slot.sample_rank];
  if (slot.yaw_sampling_profile_id !== design.yaw_sampling_profile_id
      || slot.yaw_sampling_profile_digest !== design.yaw_sampling_profile_digest
      || slot.design_size !== design.yaw_cdf_strata
      || slot.canonical_object_yaw_deg < tier.yaw_deg.minimum
      || slot.canonical_object_yaw_deg >= tier.yaw_deg.maximum_exclusive) throw new TypeError("STATE_SPACE_SLOT_INVALID");
}

function validateActiveEpisodePlan(plan) {
  if (plan === undefined || plan === null) return;
  assertObject(plan, "ACTIVE_EPISODE_PLAN_INVALID");
  const fields = [
    "schema_version", "plan_digest", "approval_scope",
    "decision_binding_digest", "operator_summary",
    "trajectory_variant_binding", "yaw_sample_binding",
    "precommit_safety", "plan_envelope_digest",
    "preapproval_evidence_digest",
  ];
  const trajectoryFields = [
    "schema_version", "trajectory_variant_id", "variation_profile_digest",
    "sampling_seed", "sample_rank", "design_size", "design_digest",
    "target_yaw_deg", "phase_parameters", "phase_parameters_digest",
    "motion_program_digest", "binding_digest",
  ];
  const safetyFields = [
    "schema_version", "run_id", "approved_plan_digest",
    "scene_binding_digest", "expected_planning_scene_digest",
    "planning_scene_readback_digest", "collision_report_digest",
    "plan_only_no_motion_digest", "post_reset_safe_snapshot_digest",
    "status",
  ];
  const trajectory = plan.trajectory_variant_binding;
  const safety = plan.precommit_safety;
  if (Object.keys(plan).length !== fields.length || fields.some((field) => !(field in plan))
      || plan.schema_version !== "data_factory.active_episode_plan.v1"
      || ![plan.plan_digest, plan.decision_binding_digest, plan.plan_envelope_digest,
        plan.preapproval_evidence_digest].every((value) => typeof value === "string" && DIGEST_PATTERN.test(value))
      || !["HUMAN_GATED", "HIL_NUMERIC_PROXY"].includes(plan.approval_scope)
      || !plan.operator_summary || typeof plan.operator_summary !== "object" || Array.isArray(plan.operator_summary)
      || !Array.isArray(plan.operator_summary.path) || !plan.operator_summary.path.length
      || !trajectory || typeof trajectory !== "object" || Array.isArray(trajectory)
      || Object.keys(trajectory).length !== trajectoryFields.length
      || trajectoryFields.some((field) => !(field in trajectory))
      || trajectory.schema_version !== "data_factory.trajectory_variant_binding.v2"
      || !["DIRECT", "TWO_STAGE_ALIGN_V2"].includes(trajectory.trajectory_variant_id)
      || typeof trajectory.sampling_seed !== "string" || !/^(0|[1-9]\d{0,19})$/.test(trajectory.sampling_seed)
      || BigInt(trajectory.sampling_seed) > 18446744073709551615n
      || !Number.isInteger(trajectory.sample_rank) || !Number.isInteger(trajectory.design_size)
      || trajectory.design_size < 1 || trajectory.sample_rank < 0 || trajectory.sample_rank >= trajectory.design_size
      || !Number.isFinite(trajectory.target_yaw_deg)
      || !trajectory.phase_parameters || typeof trajectory.phase_parameters !== "object" || Array.isArray(trajectory.phase_parameters)
      || ![trajectory.variation_profile_digest, trajectory.design_digest,
        trajectory.phase_parameters_digest, trajectory.motion_program_digest,
        trajectory.binding_digest].every((value) => typeof value === "string" && DIGEST_PATTERN.test(value))
      || !safety || typeof safety !== "object" || Array.isArray(safety)
      || Object.keys(safety).length !== safetyFields.length
      || safetyFields.some((field) => !(field in safety))
      || safety.schema_version !== "data_factory.precommit_safety.v1"
      || safety.approved_plan_digest !== plan.plan_digest || safety.status !== "PENDING"
      || safety.post_reset_safe_snapshot_digest !== null
      || ![safety.scene_binding_digest, safety.expected_planning_scene_digest,
        safety.planning_scene_readback_digest, safety.collision_report_digest,
        safety.plan_only_no_motion_digest].every((value) => typeof value === "string" && DIGEST_PATTERN.test(value))) throw new TypeError("ACTIVE_EPISODE_PLAN_INVALID");
  if (plan.yaw_sample_binding !== null) validateYawSampleBinding(plan.yaw_sample_binding);
}

function validateActiveEpisodePlanCoherence(view) {
  const plan = view.active_episode_plan;
  if (plan === undefined || plan === null) return;
  const trajectory = plan.trajectory_variant_binding;
  const yaw = plan.yaw_sample_binding;
  const profile = view.sampling_provenance.yaw_sampling_profile;
  const runtime = view.runtime;
  if (trajectory.trajectory_variant_id !== view.draft.selection.variant
      || typeof runtime.active_child_id !== "string" || !runtime.active_child_id
      || plan.precommit_safety.run_id !== runtime.active_child_id
      || trajectory.trajectory_variant_id === "TWO_STAGE_ALIGN_V2"
        && view.sampling_provenance.approach_sampling_profile === null
      || yaw !== null && (!profile
        || yaw.yaw_sampling_profile_id !== profile.yaw_sampling_profile_id
        || yaw.yaw_sampling_profile_digest !== profile.profile_digest)) throw new TypeError("ACTIVE_EPISODE_PLAN_INVALID");
  if (runtime.current_episode === undefined || runtime.current_episode === null) return;
  const sequence = view.coverage?.sequence;
  const current = Number.isInteger(runtime.current_episode)
    ? sequence?.[runtime.current_episode - 1] : undefined;
  if (!current || !yawNear(trajectory.target_yaw_deg, current.yaw_deg)
      || yaw !== null && (!yawNear(yaw.source_object_yaw_deg, current.yaw_deg)
        || !yawNear(yaw.grasp_yaw_deg, trajectory.target_yaw_deg))) throw new TypeError("ACTIVE_EPISODE_PLAN_INVALID");
  const slot = current.state_space_slot;
  if (slot === undefined || slot === null) return;
  if (yaw === null) throw new TypeError("ACTIVE_EPISODE_PLAN_INVALID");
  const commonFields = STATE_SPACE_SLOT_FIELDS.filter(
    (field) => field in yaw && field in slot && !["schema_version", "binding_digest"].includes(field),
  );
  if (commonFields.some((field) => yaw[field] !== slot[field])
      || yaw.schema_version === slot.schema_version && yaw.binding_digest !== slot.binding_digest
      || yaw.schema_version === "data_factory.yaw_sample_binding.v4"
        && STATE_SPACE_SLOT_FIELDS.some((field) => yaw[field] !== slot[field])) throw new TypeError("ACTIVE_EPISODE_PLAN_INVALID");
}

function validateRuntimeRepositionEvidence(view) {
  const runtime = view.runtime;
  const evidence = runtime.evidence;
  if (evidence === undefined || evidence === null) {
    if (runtime.phase === "OBJECT_REPOSITION_PLANNED") throw new TypeError("RUNTIME_REPOSITION_EVIDENCE_INVALID");
    return;
  }
  assertObject(evidence, "RUNTIME_REPOSITION_EVIDENCE_INVALID");
  const fields = [
    "object_reposition_binding_digest", "object_reposition_run_id",
    "object_reposition_plan_digest", "object_reposition_plan_artifact_digest",
    "object_reposition_collision_report_digest", "object_reposition_plan_only_no_motion_digest",
  ];
  const current = Number.isInteger(runtime.current_episode)
    ? view.coverage?.sequence?.[runtime.current_episode - 1]?.object_reposition : undefined;
  if (runtime.phase !== "OBJECT_REPOSITION_PLANNED"
      || Object.keys(evidence).length !== fields.length || fields.some((field) => !(field in evidence))
      || typeof evidence.object_reposition_run_id !== "string" || !evidence.object_reposition_run_id
      || fields.filter((field) => field !== "object_reposition_run_id")
        .some((field) => !DIGEST_PATTERN.test(evidence[field]))
      || !current
      || current.start_state !== "ON_SURFACE" || current.execution_stage !== "POSTCOMMIT"
      || evidence.object_reposition_binding_digest !== current.binding_digest
      || evidence.object_reposition_run_id !== current.continuation_run_id) throw new TypeError("RUNTIME_REPOSITION_EVIDENCE_INVALID");
}

function validateCameraSetup(cameraSetup) {
  if (cameraSetup === undefined || cameraSetup === null) return;
  assertObject(cameraSetup, "CAMERA_SETUP_INVALID");
  const availableRoles = cameraSetup.available_roles ?? CAMERA_ROLES.map(([role]) => role);
  if (!Array.isArray(cameraSetup.devices) || typeof cameraSetup.profile_label !== "string"
      || !Array.isArray(cameraSetup.required_roles) || new Set(cameraSetup.required_roles).size !== cameraSetup.required_roles.length
      || cameraSetup.required_roles.some((role) => role === "UNUSED" || !CAMERA_ROLE_IDS.has(role))
      || !Array.isArray(availableRoles) || !availableRoles.includes("UNUSED")
      || new Set(availableRoles).size !== availableRoles.length
      || availableRoles.some((role) => !CAMERA_ROLE_IDS.has(role))
      || cameraSetup.status !== undefined && !["READY", "BINDING_REQUIRED", "NO_CAMERA_CONNECTED"].includes(cameraSetup.status)
      || cameraSetup.reason !== undefined && cameraSetup.reason !== null && typeof cameraSetup.reason !== "string") throw new TypeError("CAMERA_SETUP_INVALID");
  assertObject(cameraSetup.bindings, "CAMERA_SETUP_INVALID");
  const deviceIds = new Set();
  cameraSetup.devices.forEach((device) => {
    assertObject(device, "CAMERA_DEVICE_INVALID");
    if (typeof device.logical_id !== "string" || !device.logical_id || deviceIds.has(device.logical_id)
        || typeof device.label !== "string" || !device.label || typeof device.status !== "string" || !device.status
        || !["CONNECTED", "CONNECTING", "DISCONNECTED"].includes(device.status)
        || device.technical_identity !== undefined && typeof device.technical_identity !== "string") throw new TypeError("CAMERA_DEVICE_INVALID");
    deviceIds.add(device.logical_id);
  });
  if (Object.keys(cameraSetup.bindings).length !== deviceIds.size
      || Object.keys(cameraSetup.bindings).some((logicalId) => !deviceIds.has(logicalId))
      || [...deviceIds].some((logicalId) => !availableRoles.includes(cameraSetup.bindings[logicalId]))) throw new TypeError("CAMERA_BINDINGS_INVALID");
  const assigned = Object.values(cameraSetup.bindings).filter((role) => role !== "UNUSED");
  if (new Set(assigned).size !== assigned.length
      || cameraSetup.status === "READY" && cameraSetup.required_roles.some((role) => !assigned.includes(role))) throw new TypeError("CAMERA_BINDINGS_INVALID");
}

function validateWorkspaceRegistration(workspace) {
  if (workspace === undefined || workspace === null) return;
  assertObject(workspace, "WORKSPACE_REGISTRATION_INVALID");
  if (typeof workspace.calibration_id !== "string" || !workspace.calibration_id) throw new TypeError("WORKSPACE_REGISTRATION_INVALID");
  if (workspace.display_name !== undefined && (typeof workspace.display_name !== "string" || !workspace.display_name.trim())) throw new TypeError("WORKSPACE_REGISTRATION_INVALID");
  assertObject(workspace.captures, "WORKSPACE_REGISTRATION_INVALID");
  const labels = WORKSPACE_CAPTURE_ROLES.map(([label]) => label);
  if (Object.keys(workspace.captures).length !== labels.length
      || labels.some((label) => typeof workspace.captures[label] !== "boolean")) throw new TypeError("WORKSPACE_REGISTRATION_INVALID");
  if (workspace.preview !== null) {
    assertObject(workspace.preview, "WORKSPACE_PREVIEW_INVALID");
    if (!["CANDIDATE_WITHIN_TOLERANCE", "CANDIDATE_OUT_OF_TOLERANCE"].includes(workspace.preview.status)
        || !DIGEST_PATTERN.test(workspace.preview.preview_digest)) throw new TypeError("WORKSPACE_PREVIEW_INVALID");
  }
  if (workspace.promotion !== null) {
    assertObject(workspace.promotion, "WORKSPACE_PROMOTION_INVALID");
    if (typeof workspace.promotion.calibration_id !== "string" || !workspace.promotion.calibration_id) throw new TypeError("WORKSPACE_PROMOTION_INVALID");
  }
  if (!Array.isArray(workspace.history) || workspace.execution_authorized !== false || workspace.training_approved !== false) throw new TypeError("WORKSPACE_REGISTRATION_INVALID");
}

function unwrapViewEnvelope(value) {
  if (!value?.projection) return value;
  const expected = ["schema_version", "session_id", "revision", "projection", "generated_at", "view_digest", "authority"];
  if (Object.keys(value).length !== expected.length || expected.some((field) => !Object.hasOwn(value, field))) throw new TypeError("VIEW_ENVELOPE_INVALID");
  assertObject(value.projection, "VIEW_PROJECTION_INVALID");
  return {...value.projection, schema_version: value.schema_version, session_id: value.session_id, revision: value.revision, generated_at: value.generated_at, view_digest: value.view_digest};
}

async function readViewResponse(response) {
  if (!response.ok) {
    const error = new Error(`HTTP_${response.status}`);
    try {
      const payload = await response.json();
      if (typeof payload.code === "string") error.nativeCode = payload.code;
    } catch (_error) { /* HTTP status remains useful even with a non-JSON error body. */ }
    throw error;
  }
  const body = await response.text().catch((error) => {
    error.bodyReadFailed = true;
    throw error;
  });
  return validateView(JSON.parse(body));
}

function canIntent(op) {
  return Boolean(currentView && !viewStale && currentView.connection_state === "READY" && !intentBusy && currentView.available_ops.includes(op));
}

function canImmediateCancel() {
  return Boolean(currentView && !viewStale && currentView.connection_state === "READY" && !cancelPending
    && ["RUNNING", "PAUSED_AWAITING_OPERATOR"].includes(currentView.runtime.workflow_state)
    && currentView.available_ops.includes("cancel_session"));
}

function setBanner(text, tone = "info", announce = true) {
  connectionBanner.className = `connection-banner ${tone}`;
  connectionBanner.textContent = text;
  if (announce) announcer.textContent = text;
}

function humanReason(code) {
  return message("reason", code, typeof code === "string" && code ? code : "확인 필요");
}

function renderTechnical(rows) {
  document.querySelector("#technical-content").innerHTML = Object.entries(rows)
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd><code>${escapeHtml(typeof value === "object" ? JSON.stringify(value) : value)}</code></dd></div>`).join("");
}

function failClose(code, detail = "", recover = false) {
  const bridgeSessionExpired = code === "BRIDGE_SESSION_EXPIRED";
  stopWatch();
  if (viewController?.releaseIntent) intentBusy = false;
  viewController?.abort();
  viewController = undefined;
  clearTimeout(recoveryTimer);
  viewStale = true;
  document.body.dataset.bridge = "blocked";
  document.querySelector("#connection-dot").className = "connection-dot blocked";
  document.querySelector("#connection-label").textContent = "복구 필요";
  document.querySelector("#session-label").textContent = currentView
    ? `오래된 정보 · 마지막 응답 ${currentView.generated_at}` : "아직 실행 상태를 확인하지 못했습니다";
  disabledBeforeFailure ??= [...document.querySelectorAll("button, input, select")].filter((control) => !control.disabled);
  document.querySelectorAll("button, input, select").forEach((control) => { control.disabled = true; });
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  const reason = {
    BRIDGE_UNAVAILABLE: "서버 연결 응답을 받지 못했습니다",
    BRIDGE_SESSION_EXPIRED: "서버 세션을 다시 연결해야 합니다",
    VIEW_HTTP_ERROR: "서버가 상태 조회 오류를 반환했습니다",
    VIEW_RESPONSE_INVALID: "서버 응답을 현재 화면의 상태로 해석할 수 없습니다",
  }[code] ?? humanReason(code);
  const retrying = recover && recoveryAttempts < 3;
  setBanner(`${reason}. ${currentView ? "아래는 오래된 마지막 확인 정보입니다. " : ""}로봇이 계속 동작할 수 있으며, 현재 동작·정지 여부는 확인되지 않았습니다. 요청을 재전송하지 않습니다. ${retrying ? `상태 조회만 자동 재시도합니다 (${recoveryAttempts + 1}/3).` : "자동 조회를 하지 않습니다. 최신 상태를 다시 확인하세요."}`, "bad");
  connectionBanner.insertAdjacentHTML("beforeend", ` <button id="retry-view" type="button">${bridgeSessionExpired ? "새 서버에 다시 연결" : "최신 상태 다시 불러오기"}</button>`);
  if (currentView) {
    for (const selector of ["#campaign-facts", "#runtime-content"]) {
      const panel = document.querySelector(selector);
      if (!panel.querySelector("[data-stale-label]")) panel.insertAdjacentHTML("afterbegin", '<strong data-stale-label>오래된 마지막 확인 정보 · 현재 상태 미확인</strong>');
    }
    document.querySelectorAll(".pulse").forEach((pulse) => { pulse.hidden = true; });
  }
  const retry = document.querySelector("#retry-view");
  retry.addEventListener("click", () => bridgeSessionExpired ? window.location.reload() : loadView(), {once: true});
  renderTechnical({error_code: code, detail, last_session: currentView?.session_id,
    last_revision: currentView?.revision, last_view_digest: currentView?.view_digest, stale: true});
  if (retrying) recoveryTimer = setTimeout(() => {
    recoveryAttempts += 1;
    loadView({automaticRecovery: true});
  }, 1000 * 2 ** recoveryAttempts);
}

function stopWatch() {
  const controller = watchController;
  watchController = undefined;
  controller?.abort();
}

function failViewRequest(error, stage = "connection") {
  const detail = error instanceof Error ? error.message : String(error);
  const stale = ["OPERATOR_VIEW_REVISION_FUTURE", "RUNNING_CANCEL_UNAVAILABLE"].includes(detail);
  const code = ["HTTP_401", "HTTP_403", "OPERATOR_TOKEN_MISSING"].includes(detail) ? "BRIDGE_SESSION_EXPIRED"
    : detail.startsWith("UNKNOWN_VIEW_ENUM") || stale ? "VIEW_STALE"
      : detail === "VIEW_REVISION_ROLLBACK" ? detail
        : detail.startsWith("HTTP_") ? "VIEW_HTTP_ERROR"
          : stage === "connection" || error.bodyReadFailed || error.name === "TimeoutError" ? "BRIDGE_UNAVAILABLE" : "VIEW_RESPONSE_INVALID";
  failClose(code, error.nativeCode ? `${detail}:${error.nativeCode}` : detail,
    code === "BRIDGE_UNAVAILABLE" || /^HTTP_5\d\d$/.test(detail));
}

function setupReady(view) {
  return ["READY", "READY_WITH_EXCEPTION"].includes(view.setup.host_status)
    && (!view.camera_setup?.status || view.camera_setup.status === "READY");
}

function workflowStep(view) {
  const state = view.runtime.workflow_state;
  if (state === "PREPARING" || !setupReady(view) || view.available_ops.includes("recover_camera_setup")) return "environment";
  if (state === "AUTHORING") return "plan";
  if (["REVIEW_CAMPAIGN", "AWAITING_APPROVAL"].includes(state)) return "review";
  if (["RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR", "BLOCKED"].includes(state)) return "execution";
  if (state === "REVIEW_RESULTS"
      || (state === "TERMINAL" && view.episode_history?.length)) return "results";
  return "next";
}

function unlockedSteps(view) {
  const unlocked = new Set(["environment"]);
  if (setupReady(view)) unlocked.add("plan");
  if (view.campaign_envelope) unlocked.add("review");
  if (view.campaign_authorization || ["RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR", "BLOCKED", "REVIEW_RESULTS", "TERMINAL"].includes(view.runtime.workflow_state)) unlocked.add("execution");
  if (view.episode_history?.length || ["REVIEW_RESULTS", "TERMINAL"].includes(view.runtime.workflow_state)) unlocked.add("results");
  if (view.runtime.workflow_state === "TERMINAL") unlocked.add("next");
  return unlocked;
}

function showStep(name, focus = false) {
  if (!currentView || !unlockedSteps(currentView).has(name)) return;
  document.querySelectorAll(".flow-step").forEach((section) => { section.hidden = section.dataset.step !== name; });
  document.querySelectorAll("[data-step-target]").forEach((button) => {
    const active = button.dataset.stepTarget === name;
    if (active) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
    button.classList.toggle("active", active);
  });
  if (focus) manualStep = name;
  if (focus) document.querySelector(`#step-${name}`).focus({preventScroll: true});
}

function renderSteps(view) {
  const unlocked = unlockedSteps(view);
  const automaticStep = workflowStep(view);
  document.querySelectorAll("[data-step-target]").forEach((button) => {
    button.disabled = !unlocked.has(button.dataset.stepTarget);
    button.closest("li").classList.toggle("complete", FLOW.indexOf(button.dataset.stepTarget) < FLOW.indexOf(workflowStep(view)));
  });
  if (renderedWorkflow !== view.runtime.workflow_state && manualStep === renderedWorkflowStep) manualStep = undefined;
  renderedWorkflow = view.runtime.workflow_state;
  renderedWorkflowStep = automaticStep;
  showStep(manualStep && unlocked.has(manualStep) ? manualStep : automaticStep);
}

function renderConnection(view) {
  const ready = view.connection_state === "READY";
  document.body.dataset.bridge = ready ? "ready" : "blocked";
  document.querySelector("#connection-dot").className = `connection-dot ${ready ? "ready" : "blocked"}`;
  document.querySelector("#connection-label").textContent = message("connection", view.connection_state);
  document.querySelector("#session-label").textContent = ready ? "최신 상태가 연결되었습니다" : "새 요청은 차단됩니다";
  setBanner(ready ? "최신 수집 상태를 표시하고 있습니다." : `${message("connection", view.connection_state)}. 최신 상태 전에는 실행하지 않습니다.`, ready ? "good" : "bad", false);
}

function renderSetup(view) {
  if (canIntent("recover_camera_setup")) {
    document.querySelector("#setup-summary").innerHTML = "<strong>카메라 재연결 필요</strong><span>이전 장치 상태는 다시 확인하기 전까지 사용하지 않습니다.</span>";
    document.querySelector("#setup-subsystems").innerHTML = "";
    renderCameraSetup(view);
    document.querySelector("#environment-next").disabled = true;
    document.querySelector("#prepare-environment").hidden = true;
    document.querySelector("#recover-home").hidden = true;
    return;
  }
  const observed = typeof view.setup.observed_at === "string"
    ? ` · ${new Date(view.setup.observed_at).toLocaleTimeString("ko-KR")}` : "";
  const recovery = view.home_recovery;
  const recoverySummary = recovery
    ? `<span>${escapeHtml(message("status", recovery.status))} · 그리퍼 열림 · ${recovery.arm_goal_count === 1 ? "로봇 이동 1회" : "로봇 이동 없음"}</span>`
    : "";
  document.querySelector("#setup-summary").innerHTML = `<strong>${escapeHtml(message("status", view.setup.host_status, view.setup.summary ?? "환경 상태 확인됨"))}</strong><span>${escapeHtml((view.setup.summary ?? "현재 장치 상태를 아래에서 확인하세요.") + observed)}</span>${recoverySummary}`;
  document.querySelector("#setup-subsystems").innerHTML = view.setup.subsystems.map((item) => {
    const label = message("status", item.status, message("reason", item.status, "확인 필요"));
    const tone = item.status === "READY" ? "ready" : item.status === "CONNECTING" ? "waiting" : "attention";
    return `<li><span class="device-state ${tone}" aria-hidden="true"></span><div><strong>${escapeHtml(item.label)}</strong><p>${escapeHtml(item.detail)}</p></div><b>${escapeHtml(label)}</b></li>`;
  }).join("");
  renderCameraSetup(view);
  document.querySelector("#environment-next").disabled = !setupReady(view);
  document.querySelector("#prepare-environment").hidden = !canIntent("prepare_environment");
  document.querySelector("#recover-home").hidden = !canIntent("recover_home");
}

function renderCameraSetup(view) {
  const setup = view.camera_setup;
  const section = document.querySelector("#camera-setup");
  const recovering = canIntent("recover_camera_setup");
  const recoveryCard = recovering
    ? '<div class="recovery-card"><strong>카메라 연결을 다시 확인하세요.</strong><p>현재 연결과 역할을 새로 읽고 환경 준비로 돌아갑니다.</p><button type="button" data-recovery-op="recover_camera_setup">카메라 다시 연결</button></div>'
    : "";
  section.hidden = !setup && !recovering;
  if (recovering) {
    document.querySelector("#camera-role-list").innerHTML = recoveryCard;
    document.querySelector("#camera-role-status").textContent = "현재 장치와 역할을 다시 확인해야 합니다.";
    document.querySelector("#camera-profile-label").textContent = "재연결 필요";
    return;
  }
  if (!setup) {
    document.querySelector("#camera-role-list").innerHTML = "";
    document.querySelector("#camera-role-status").textContent = "";
    document.querySelector("#camera-profile-label").textContent = "";
    return;
  }
  const editable = canIntent("update_camera_bindings");
  const availableRoles = setup.available_roles ?? CAMERA_ROLES.map(([role]) => role);
  document.querySelector("#camera-profile-label").textContent = setup.profile_label || "녹화 설정 결정 전";
  const deviceRow = (device, index) => {
    const selected = setup.bindings[device.logical_id];
    const options = CAMERA_ROLES.filter(([role]) => availableRoles.includes(role)).map(([role, label]) => `<option value="${role}" ${role === selected ? "selected" : ""}>${label}</option>`).join("");
    const status = {CONNECTED: "연결됨", CONNECTING: "연결 중", DISCONNECTED: "연결 안 됨"}[device.status] ?? "상태 확인 필요";
    return `<div class="camera-role-row"><span class="camera-lens" aria-hidden="true"></span><div><strong>${escapeHtml(device.label || `카메라 ${index + 1}`)}</strong><small>${status}</small></div><label>사용 위치<select data-camera-logical-id="${escapeHtml(device.logical_id)}" aria-label="${escapeHtml(device.label)} 사용 위치" ${editable ? "" : "disabled"}>${options}</select></label></div>`;
  };
  const roleList = document.querySelector("#camera-role-list");
  const focusedSelect = document.activeElement?.matches?.("#camera-role-list select[data-camera-logical-id]") ? document.activeElement : null;
  const focusedIndex = focusedSelect ? setup.devices.findIndex((device) => device.logical_id === focusedSelect.dataset.cameraLogicalId) : -1;
  const preserveFocused = editable && focusedIndex >= 0 && availableRoles.includes(focusedSelect.value);
  if (!preserveFocused) {
    roleList.innerHTML = (setup.devices.length ? setup.devices.map(deviceRow).join("") : '<p class="empty-camera">연결된 카메라가 없습니다.</p>') + recoveryCard;
  } else {
    const focusedRow = focusedSelect.closest(".camera-role-row");
    const device = setup.devices[focusedIndex];
    focusedRow.querySelector("strong").textContent = device.label || `카메라 ${focusedIndex + 1}`;
    focusedRow.querySelector("small").textContent = {CONNECTED: "연결됨", CONNECTING: "연결 중", DISCONNECTED: "연결 안 됨"}[device.status] ?? "상태 확인 필요";
    focusedSelect.setAttribute("aria-label", `${device.label} 사용 위치`);
    focusedSelect.innerHTML = CAMERA_ROLES.filter(([role]) => availableRoles.includes(role)).map(([role, label]) => `<option value="${role}">${label}</option>`).join("");
    focusedSelect.value = setup.bindings[device.logical_id];
    [...roleList.children].filter((row) => row !== focusedRow).forEach((row) => row.remove());
    setup.devices.slice(0, focusedIndex).forEach((item, index) => focusedRow.insertAdjacentHTML("beforebegin", deviceRow(item, index)));
    setup.devices.slice(focusedIndex + 1).forEach((item, index) => roleList.insertAdjacentHTML("beforeend", deviceRow(item, focusedIndex + index + 1)));
  }
  const assigned = new Set(Object.values(setup.bindings));
  const missing = setup.required_roles.filter((role) => !assigned.has(role));
  const used = [...assigned].filter((role) => role !== "UNUSED").map((role) => CAMERA_ROLES.find(([id]) => id === role)?.[1] ?? role);
  document.querySelector("#camera-role-status").textContent = setup.reason
    ? humanReason(setup.reason)
    : missing.length
    ? `${missing.map((role) => CAMERA_ROLES.find(([id]) => id === role)?.[1] ?? role).join(" · ")} 역할을 지정하세요.`
    : setup.devices.length ? `${setup.devices.length}대 연결 · ${used.join(" · ")} 사용${assigned.has("UNUSED") ? " · 나머지 미사용" : ""}` : "카메라를 연결하면 역할을 지정할 수 있습니다.";
}

function startPoseLabel(view, startPoseId) {
  return view.start_pose_setup?.profiles.find((profile) => profile.start_pose_id === startPoseId)?.display_name
    ?? catalogOption(view, "start", startPoseId)?.label
    ?? startPoseId;
}

function selectedStartPoseProfiles(view) {
  if (!view.start_pose_setup) return [];
  const selected = new Set(view.start_pose_setup.selected_start_pose_ids);
  return view.start_pose_setup.profiles.filter((profile) => selected.has(profile.start_pose_id));
}

function renderStateSpaceSummary(view) {
  const summary = view.state_space_summary;
  const element = document.querySelector("#state-space-summary");
  element.hidden = !summary;
  if (!summary) {
    element.innerHTML = "";
    return;
  }
  const shape = summary.design_shape;
  const rows = [
    ["시작 자세", `${summary.selected_start_pose_count}개`],
    ["등록된 적격 조건", `${summary.catalog_eligible_condition_count}개 (catalog)`],
    ["적격 시작자세 × 조건", `${summary.eligible_start_condition_pair_count}개`],
    ["계획", isPickPlace(view)
      ? `${summary.planned_episode_count} 이동 episodes · 물체 위치 ${summary.object_position_count}개 (최초 source + 각 destination)`
      : `${summary.planned_episode_count} episodes`],
  ];
  if (shape) {
    rows.splice(3, 0, [
      "현재 자동 실험 설계",
      `${shape.columns} × ${shape.rows} × ${shape.yaw_cdf_strata} = ${summary.per_workspace_condition_count}개 (작업영역별)`,
    ]);
    summary.workspace_coverage.forEach((item) => {
      const routeIndex = workspaceRoute(view).findIndex((endpoint) =>
        endpoint.workspace_id === item.workspace_id
        && endpoint.frame_id === item.frame_id);
      const region = routeIndex < 0
        ? null
        : directPoseDomain(view, routeIndex)?.coverage_region;
      const binding = {
        VERIFIED: "실제 시트 결속 검증됨",
        PREPARED_NOT_VERIFIED: "실제 색상/시트 결속 검증 전",
        NOT_CONFIGURED: "색상 영역 미설정",
      }[region?.physical_binding_status] ?? "물리 결속 상태 확인 불가";
      rows.push([
        `${workspaceName(view, item.workspace_id)} A4-local 설계 coverage`,
        `${item.planned_episode_count} / ${item.full_coverage_episode_count} episodes`,
      ]);
      rows.push([
        `${workspaceName(view, item.workspace_id)} 물리 A4 결속`,
        `${region?.region_id ?? "영역 미지정"} · ${binding}`,
      ]);
    });
    rows.push([
      "모든 작업영역 A4-local 설계 완전 coverage",
      `${summary.full_coverage_episode_count} episodes 필요`,
    ]);
  }
  element.innerHTML = rows.map(([term, value]) => `<div><dt>${term}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}

function renderExperimentDesign(view, editable) {
  const form = document.querySelector("#experiment-design-form");
  const profile = view.sampling_provenance.state_space_design_profile;
  form.hidden = !profile;
  if (!profile) return;
  const values = {
    "#design-columns-input": profile.spatial_strata.columns,
    "#design-rows-input": profile.spatial_strata.rows,
    "#design-yaw-input": profile.yaw_cdf_strata,
  };
  const direct = view.draft.authoring_mode === "DIRECT_EDIT";
  Object.entries(values).forEach(([selector, value]) => {
    const input = document.querySelector(selector);
    if (document.activeElement !== input) input.value = value;
    input.disabled = !editable || direct;
  });
  document.querySelector("#apply-experiment-design").disabled = !editable || direct;
  document.querySelector("#experiment-design-status").textContent = direct
    ? "직접 선택 좌표를 보존하기 위해 자동 선택 모드에서만 설계를 변경할 수 있습니다."
    : `backend sampler profile ${profile.state_space_design_profile_id}을 사용합니다. 적용 전에는 현재 좌표와 seed가 바뀌지 않습니다.`;
}

function renderMotionPreset(view, editable) {
  const select = document.querySelector("#motion-preset-select");
  if (!select) return;
  const presets = view.motion_presets || [];
  const binding = view.draft.motion_preset;
  const selected = presets.find((item) => item.id === binding?.id && item.digest === binding?.digest);
  select.innerHTML = '<option value="">기존 검증 설정 유지</option>' + presets.map((item) =>
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.purpose)} · ${item.status === "QUALIFIED" ? "자격 결속됨" : "물리 자격 필요"}</option>`).join("");
  if (binding && !selected) select.insertAdjacentHTML("beforeend", '<option value="stale">변경된 정책 — 다시 선택 필요</option>');
  select.value = selected?.id || (binding ? "stale" : "");
  select.disabled = !editable;
  document.querySelector("#motion-preset-summary").textContent = !binding
    ? "기존 자격에 기록된 구간별 요청값을 유지합니다."
    : !selected ? "정책이 변경되었습니다. 최신 정책을 선택하거나 기존 설정으로 돌아가세요."
    : selected.status === "QUALIFIED" ? "선택한 모든 작업영역에 정확히 결속된 자격을 사용합니다. 기존 실행 승인 단계는 유지됩니다."
    : "선택은 초안에만 반영됩니다. 이 정책의 물리 자격이 없어 계획 확정·실행할 수 없습니다.";
  document.querySelector("#motion-preset-phases").innerHTML = Object.entries(selected?.phase_scaling || {}).map(([phase, values]) =>
    `<div><dt>${escapeHtml(phase)}</dt><dd>속도 ${values.velocity_scaling * 100}% · 가속도 ${values.acceleration_scaling * 100}%</dd></div>`).join("");
}

function renderStartPoseSetup(view) {
  const setup = view.runtime.workflow_state === "AUTHORING" ? view.start_pose_setup : null;
  const entry = document.querySelector("#start-pose-entry");
  const dialog = document.querySelector("#start-pose-dialog");
  entry.hidden = !setup;
  if (!setup) {
    if (dialog.open) dialog.close();
    document.querySelector("#start-pose-profile-list").innerHTML = "";
    document.querySelector("#start-pose-status").textContent = "";
    return;
  }
  document.querySelector("#open-start-pose").disabled = intentBusy;
  document.querySelector("#capture-start-pose").disabled = !canIntent("capture_start_pose");
  const selected = new Set(setup.selected_start_pose_ids);
  const canSelect = canIntent("update_start_pose_selection");
  document.querySelector("#start-pose-profile-list").innerHTML = setup.profiles.length ? setup.profiles.map((profile) => {
    const available = profile.status === "AVAILABLE";
    const required = selected.size === 1 && selected.has(profile.start_pose_id);
    const label = {CANDIDATE: "후보", AVAILABLE: "사용 가능", QUALIFICATION_REQUIRED: "검증 필요"}[profile.status];
    const reason = profile.reason ? ` · ${humanReason(profile.reason)}` : "";
    return `<label class="start-pose-profile"><input type="checkbox" data-start-pose-id="${escapeHtml(profile.start_pose_id)}" ${selected.has(profile.start_pose_id) ? "checked" : ""} ${canSelect && available && !required ? "" : "disabled"}><span><strong>${escapeHtml(profile.display_name)}</strong><small>${escapeHtml(label + reason)}</small></span></label>`;
  }).join("") : '<p class="empty-camera">등록된 시작 자세가 없습니다.</p>';
  document.querySelector("#start-pose-status").textContent = `${setup.selected_start_pose_ids.length}개 시작 자세가 수집 범위에 포함됩니다. 최소 1개는 필요합니다.`;
}

function catalogOption(view, axis, id) {
  return view.catalog.axes[axis].find((option) => option.id === id);
}

function directPoseDomain(view, routeIndex = 0) {
  const route = workspaceRoute(view);
  const endpoint = route[routeIndex] ?? {
    workspace_id: view.draft.selection.workspace,
    frame_id: view.draft.selection.frame,
  };
  const domains = view.catalog.workspace_domains ?? [view.catalog.workspace_domain];
  const domain = domains.find((item) => item?.workspace_id === endpoint.workspace_id
    && item?.frame_id === endpoint.frame_id && item?.object_id === view.draft.selection.object);
  const validBounds = (bounds) => bounds && Number.isFinite(bounds.minimum) && Number.isFinite(bounds.maximum) && bounds.minimum <= bounds.maximum;
  if (!domain || typeof domain !== "object" || Array.isArray(domain)
      || domain.workspace_id !== endpoint.workspace_id || domain.frame_id !== endpoint.frame_id
      || domain.object_id !== view.draft.selection.object
      || domain.coordinate_mode !== "CONTINUOUS_A4_PLANE" || !validBounds(domain.x_mm) || !validBounds(domain.y_mm)
      || !domain.yaw_deg || !Number.isFinite(domain.yaw_deg.minimum) || !Number.isFinite(domain.yaw_deg.maximum_exclusive)
      || domain.yaw_deg.minimum >= domain.yaw_deg.maximum_exclusive) return null;
  return domain;
}

function renderCurrentObjectPose(view, editable) {
  const domain = directPoseDomain(view);
  const pose = view.draft.current_object_pose;
  const pickPlace = isPickPlace(view);
  const fields = [
    ["#current-object-x", pose.x_mm, domain?.x_mm],
    ["#current-object-y", pose.y_mm, domain?.y_mm],
    ["#current-object-yaw", pose.yaw_deg, domain?.yaw_deg],
  ];
  fields.forEach(([selector, value, bounds]) => {
    const input = document.querySelector(selector);
    input.value = value;
    if (bounds) {
      input.min = bounds.minimum;
      if (Number.isFinite(bounds.maximum)) input.max = bounds.maximum;
      else input.removeAttribute("max");
    } else {
      input.removeAttribute("min");
      input.removeAttribute("max");
    }
    input.disabled = !editable || !domain;
  });
  document.querySelector("#current-object-title").textContent = pickPlace
    ? "물체 출발점 (SOURCE)" : "지금 놓인 물체";
  document.querySelector("#apply-current-object").textContent = pickPlace
    ? "출발점 적용" : "현재 위치 적용";
  document.querySelector("#apply-current-object").disabled = !editable || !domain;
  document.querySelector("#current-object-status").textContent = domain
    ? `작성안 r${view.draft.revision} · ${selectedLabel(view, "workspace")} · ${pickPlace ? "첫 에피소드의 물체 출발점입니다. 로봇 시작 자세와는 별도입니다." : "첫 에피소드와 작업영역 초기화가 이 위치에서 시작합니다."}`
    : "현재 작업영역의 입력 범위를 사용할 수 없습니다.";
  const position = view.draft.object_position;
  const restore = document.querySelector("#refresh-object-position");
  if (restore) {
    restore.hidden = position == null || position.status !== "STALE";
    restore.disabled = !canIntent("refresh_object_position");
  }
  if (position != null) {
    document.querySelector("#apply-current-object").textContent = "직접 옮긴 위치 저장";
    const meaning = position.status === "AVAILABLE"
      ? position.source === "HUMAN" ? "사람이 보고한 현재 위치입니다."
        : `마지막 실행의 착지 근거로 복원했습니다${position.run_id ? ` · ${position.run_id}` : ""}. 영상으로 측정한 위치는 아닙니다.`
      : position.status === "STALE" ? "저장된 위치 근거가 바뀌었습니다. 최신 위치를 사용하거나 직접 옮긴 위치를 보고하세요."
        : position.status === "MISSING" ? "저장된 물체 위치가 없습니다. 직접 놓은 위치를 입력하세요."
          : `현재 위치를 확정할 수 없습니다 (${position.reason}). 표시된 입력값은 현재 위치의 증거가 아닙니다.`;
    document.querySelector("#current-object-status").textContent = `${meaning} 물체를 직접 옮겼을 때만 위치를 수정하세요. 기존 실행·cell 조건은 별도로 유지됩니다.`;
  }
}

function renderDirectPoseEditor(view, editable) {
  const editor = document.querySelector("#direct-pose-editor");
  const direct = view.draft.authoring_mode === "DIRECT_EDIT";
  const pairMode = Array.isArray(view.draft.direct_pairs);
  const targetIndex = pairMode ? nextDirectPairIndex(view) : 0;
  const route = workspaceRoute(view);
  const targetEndpoint = route[Math.min(targetIndex, route.length - 1)];
  const domain = directPoseDomain(view, Math.min(targetIndex, route.length - 1));
  const pickPlace = isPickPlace(view);
  const startProfiles = selectedStartPoseProfiles(view);
  const enabled = direct && editable && Boolean(domain) && (!pairMode || startProfiles.length > 0);
  editor.hidden = !direct;
  editor.dataset.pairMode = String(pairMode);
  [["#direct-x-input", domain?.x_mm], ["#direct-y-input", domain?.y_mm]].forEach(([selector, bounds]) => {
    const input = document.querySelector(selector);
    if (bounds) {
      input.min = bounds.minimum;
      input.max = bounds.maximum;
    } else {
      input.removeAttribute("min");
      input.removeAttribute("max");
    }
    input.disabled = !enabled;
  });
  const yawInput = document.querySelector("#direct-yaw-input");
  if (domain?.yaw_deg) {
    yawInput.min = domain.yaw_deg.minimum;
    yawInput.removeAttribute("max");
  } else {
    yawInput.removeAttribute("min");
    yawInput.removeAttribute("max");
  }
  yawInput.disabled = !enabled;
  const pairs = pairMode ? view.draft.direct_pairs : null;
  const poses = view.draft.direct_poses ?? [];
  const anchor = view.draft.current_object_pose;
  const required = pairMode ? pairs.length : 1 + poses.length;
  const requiredNodes = spatialNodeCount(view);
  const terminalExists = pairMode && pairs.some((pair) => pair.start_pose_id === null);
  const addingTerminal = pickPlace && pairMode
    && required === view.draft.requested_count && !terminalExists;
  const startLabel = document.querySelector("#direct-start-label");
  const startSelect = document.querySelector("#direct-start-select");
  document.querySelector("#direct-pose-title").textContent = pickPlace
    ? "출발·도착 위치 직접 입력" : "직접 조건 입력";
  startLabel.hidden = !pairMode || addingTerminal;
  startSelect.innerHTML = pairMode ? startProfiles.map((profile) => `<option value="${escapeHtml(profile.start_pose_id)}">${escapeHtml(profile.display_name)}</option>`).join("") : "";
  startSelect.disabled = !enabled || addingTerminal;
  document.querySelector("#add-direct-pose").textContent = addingTerminal
    ? "마지막 도착점 추가" : pickPlace ? "다음 물체 위치 추가" : "자세 추가";
  document.querySelector("#add-direct-pose").disabled = !enabled || required >= requiredNodes;
  document.querySelector("#direct-selection-count").textContent = pickPlace
    ? `${view.draft.requested_count}개 에피소드 · 물체 위치 ${required}/${requiredNodes}개`
    : `${required}개 · 표시 순서대로 실행`;
  document.querySelector("#direct-domain-status").textContent = !domain
    ? "현재 작업영역의 직접 입력 범위를 사용할 수 없습니다."
    : pairMode && !startProfiles.length
      ? "수집에 사용할 시작 자세를 먼저 선택하세요."
    : required > requiredNodes
      ? `현재 ${required}개 위치가 선택되었습니다. 필요한 ${requiredNodes}개에 맞게 삭제하세요.`
      : pickPlace
        ? `다음 입력: ${workspaceName(view, targetEndpoint.workspace_id)} · 각 에피소드는 HOME에서 출발점으로 접근해 도착점에 놓습니다. 직전 도착점은 다음 출발점이 됩니다.`
      : pairMode
        ? `시작 자세와 위치·각도를 한 조건으로 추가합니다. 입력 범위: X ${domain.x_mm.minimum}~${domain.x_mm.maximum} mm, Y ${domain.y_mm.minimum}~${domain.y_mm.maximum} mm.`
        : `현재 물체 위치를 첫 조건으로 두고 표시 순서대로 실행합니다. 입력 범위: X ${domain.x_mm.minimum}~${domain.x_mm.maximum} mm, Y ${domain.y_mm.minimum}~${domain.y_mm.maximum} mm.`;
  if (pairMode) {
    document.querySelector("#direct-pose-list").innerHTML = pairs.length ? pairs.map((pair, index) => {
      if (!pickPlace) return `<li><span>${index + 1}</span><strong>${escapeHtml(startPoseLabel(view, pair.start_pose_id))} · ${escapeHtml(poseText(pair))}</strong><button type="button" class="secondary-button" data-pair-index="${index}" aria-label="${index + 1}번째 직접 조건 삭제" ${enabled ? "" : "disabled"}>삭제</button></li>`;
      const role = index === 0
        ? "출발점 1"
        : pair.start_pose_id === null
          ? `도착점 ${view.draft.requested_count}`
          : `도착점 ${index} · 다음 출발점 ${index + 1}`;
      const robotStart = pair.start_pose_id === null
        ? "" : `로봇 시작: ${startPoseLabel(view, pair.start_pose_id)} · `;
      const action = index === 0
        ? "<em>현재 출발점</em>"
        : `<button type="button" class="secondary-button" data-pair-index="${index}" aria-label="${escapeHtml(role)} 삭제" ${enabled ? "" : "disabled"}>삭제</button>`;
      return `<li><span>${index + 1}</span><strong>${escapeHtml(`${role} · ${workspaceName(view, pair.place_id)} · ${robotStart}${poseText(pair)}`)}</strong>${action}</li>`;
    }).join("") : '<li class="empty-pose">선택한 위치가 없습니다.</li>';
    return;
  }
  const anchorRow = `<li><span>1</span><strong>${escapeHtml(selectedLabel(view, "start"))} · X ${escapeHtml(anchor?.x_mm ?? 0)} · Y ${escapeHtml(anchor?.y_mm ?? 0)} · ${escapeHtml(anchor?.yaw_deg ?? 0)}°</strong><em>현재 물체 위치</em></li>`;
  document.querySelector("#direct-pose-list").innerHTML = anchorRow + poses.map((pose, index) => `<li><span>${index + 2}</span><strong>${escapeHtml(selectedLabel(view, "start"))} · X ${escapeHtml(pose.x_mm)} · Y ${escapeHtml(pose.y_mm)} · ${escapeHtml(pose.yaw_deg)}°</strong><button type="button" class="secondary-button" data-pose-index="${index}" aria-label="${index + 2}번째 직접 조건 삭제" ${enabled ? "" : "disabled"}>삭제</button></li>`).join("");
}

function renderCatalog(view) {
  const editable = view.runtime.workflow_state === "AUTHORING" && canIntent("update_draft");
  const direct = view.draft.authoring_mode === "DIRECT_EDIT";
  const domain = directPoseDomain(view);
  const anchor = view.draft.current_object_pose;
  const pairMode = Array.isArray(view.draft.direct_pairs);
  const directPairs = pairMode ? view.draft.direct_pairs : [];
  const directPoses = view.draft.direct_poses ?? [];
  const samePose = (cell, pose) => Boolean(pose) && cell.x_mm === pose.x_mm && cell.y_mm === pose.y_mm && cell.yaw_deg === pose.yaw_deg;
  const directFull = (pairMode ? directPairs.length : 1 + directPoses.length) >= spatialNodeCount(view);
  CATALOG_AXES.forEach((axis) => {
    const select = document.querySelector(`[data-axis="${axis}"]`);
    const selected = view.draft.selection[axis];
    select.innerHTML = view.catalog.axes[axis].map((option) => {
      const unavailable = option.available ? "" : ` — ${humanReason(option.reason)}`;
      return `<option value="${escapeHtml(option.id)}" ${option.id === selected ? "selected" : ""} ${option.available ? "" : "disabled"}>${escapeHtml(option.label + unavailable)}</option>`;
    }).join("");
    select.disabled = !editable || axis === "camera";
    const hint = document.querySelector(`[data-axis-hint="${axis}"]`);
    const selectedOption = catalogOption(view, axis, selected);
    if (hint) hint.textContent = [
      selectedOption?.description,
      selectedOption?.execution_reason ? humanReason(selectedOption.execution_reason) : "",
    ].filter(Boolean).join(" · ");
  });
  const pickPlace = isPickPlace(view);
  const workspaceLabel = selectedLabel(view, "workspace");
  const route = workspaceRoute(view);
  const destination = route.find((item) => item.workspace_id !== route[0]?.workspace_id);
  document.querySelector("#workspace-select-label").textContent = pickPlace
    ? "물체 출발 작업영역" : "작업영역";
  document.querySelector("#workspace-route-summary").textContent = pickPlace
    ? `${workspaceLabel} → ${workspaceName(view, destination?.workspace_id ?? route[0]?.workspace_id)}를 번갈아 이동합니다. 각 작업영역의 내부 좌표계는 자동 결속하며, 놓은 뒤 위로 후퇴하고 HOME에서 다음 episode를 시작합니다.`
    : `${workspaceLabel} 안에서 집고 다음 수집 위치에 놓은 뒤 위로 후퇴하고 HOME에서 다음 episode를 시작합니다. 내부 좌표계는 자동 적용됩니다.`;
  document.querySelector("#camera-selection-label").textContent = view.camera_setup?.profile_label || selectedLabel(view, "camera");
  document.querySelectorAll('[name="authoring_mode"]').forEach((control) => {
    control.checked = control.value === view.draft.authoring_mode;
    control.disabled = !editable;
  });
  document.querySelector("#count-input").value = view.draft.requested_count;
  document.querySelector("#repeat-input").value = view.draft.repeat;
  document.querySelector("#seed-input").value = view.draft.normalized_seed;
  document.querySelector("#count-input").disabled = !editable;
  document.querySelector("#repeat-input").disabled = !editable || direct;
  document.querySelector("#seed-input").disabled = !editable;
  document.querySelector("#repeat-input").closest("label").hidden = direct;
  document.querySelector("#workspace-domain-summary").textContent = domain
    ? `현재 작성 범위: X ${domain.x_mm.minimum}~${domain.x_mm.maximum} mm, Y ${domain.y_mm.minimum}~${domain.y_mm.maximum} mm, yaw ${domain.yaw_deg.minimum}° 이상 ${domain.yaw_deg.maximum_exclusive}° 미만. 등록된 셀은 빠른 기준점입니다.`
    : "현재 작업영역의 작성 범위를 사용할 수 없습니다.";
  const executionBlocked = view.draft.execution_ready === false;
  const executionReason = view.draft.execution_reason ?? view.catalog.selection_execution?.reason ?? view.draft.draft_reason;
  document.querySelector("#plan-lock-reason").textContent = executionBlocked
    ? humanReason(executionReason)
    : view.draft.draft_ready === false ? humanReason(view.draft.draft_reason)
    : editable ? "변경할 때마다 최신 조합을 다시 확인합니다." : "현재 단계에서는 계획을 변경할 수 없습니다.";
  document.querySelector("#compile-campaign").hidden = false;
  document.querySelector("#compile-campaign").disabled = !canIntent("compile_draft");

  renderCurrentObjectPose(view, editable);
  renderExperimentDesign(view, editable);
  renderMotionPreset(view, editable);
  renderStateSpaceSummary(view);
  renderDirectPoseEditor(view, editable);
  const disclosure = document.querySelector("#cell-grid-disclosure");
  disclosure.hidden = !direct;
  if (!direct) disclosure.open = false;
  document.querySelector("#cell-grid").innerHTML = direct ? view.draft.cells.map((cell) => {
    const selected = direct
      ? pairMode ? directPairs.some((pair) => samePose(cell, pair)) : samePose(cell, anchor) || directPoses.some((pose) => samePose(cell, pose))
      : ["SELECTED", "PINNED"].includes(cell.selection_state);
    const fixedAnchor = direct && samePose(cell, anchor)
      && (!pairMode || isPickPlace(view));
    const available = cell.eligibility_status === "ELIGIBLE";
    const disabled = !editable || !direct || !available || fixedAnchor || !selected && directFull;
    const reason = available ? (selected ? "수집에 포함됨" : "선택 가능") : humanReason(cell.reason_codes?.[0]);
    const cellDetail = direct ? `${cell.split} · ${cell.repeat}회` : selected ? "현재 시작점" : "빠른 기준점";
    return `<button type="button" class="cell ${selected ? "selected" : ""} ${available ? "" : "blocked"}" data-cell-id="${escapeHtml(cell.cell_id)}" aria-pressed="${selected}" aria-label="X ${escapeHtml(cell.x_mm)} mm, Y ${escapeHtml(cell.y_mm)} mm, ${escapeHtml(cell.yaw_deg)}도, ${escapeHtml(reason)}" ${disabled ? "disabled" : ""}>
      <span>X ${escapeHtml(cell.x_mm)} · Y ${escapeHtml(cell.y_mm)}</span><strong>${escapeHtml(cell.yaw_deg)}°</strong><small>${escapeHtml(cellDetail)}</small><em>${escapeHtml(reason)}</em></button>`;
  }).join("") : "";
}

function renderWorkspaceRegistration(view) {
  const workspace = view.runtime.workflow_state === "AUTHORING" ? view.workspace_registration : null;
  const entry = document.querySelector("#workspace-entry");
  const dialog = document.querySelector("#workspace-dialog");
  const named = Boolean(workspace?.display_name?.trim());
  const canBegin = canIntent("new_workspace_registration");
  entry.hidden = !named && !canBegin;
  document.querySelector("#open-workspace").disabled = intentBusy || !named && !canBegin;
  document.querySelector("#open-workspace").textContent = named && !workspace.promotion ? "등록 계속" : workspace?.promotion && !canBegin ? "등록 결과 보기" : "새 작업영역 등록";
  const nameForm = document.querySelector("#workspace-name-form");
  nameForm.hidden = named && (!workspace.promotion || !canBegin);
  nameForm.querySelector("button").disabled = !canBegin;
  document.querySelector("#workspace-registration-content").hidden = !named;
  if (!workspace || !named) {
    document.querySelector("#workspace-selection").innerHTML = "";
    document.querySelector("#workspace-captures").innerHTML = "";
    document.querySelector("#workspace-preview-form").hidden = true;
    document.querySelector("#workspace-preview-status").innerHTML = "";
    document.querySelector("#workspace-promotion").innerHTML = "";
    document.querySelector("#workspace-next-action").textContent = "";
    if (!named && dialog.open && entry.hidden) dialog.close();
    return;
  }

  document.querySelector("#workspace-selection").innerHTML = `<div><dt>작업영역 이름</dt><dd>${escapeHtml(workspace.display_name)}</dd></div>`;

  const canCapture = canIntent("capture_workspace_point");
  document.querySelector("#workspace-captures").innerHTML = WORKSPACE_CAPTURE_ROLES.map(([label, title, detail]) => {
    const captured = workspace.captures[label];
    const action = canCapture ? `<button type="button" class="secondary-button" data-capture-label="${label}">${captured ? "다시 캡처" : "이 위치 캡처"}</button>` : "";
    return `<li data-capture-role="${label}"><span>${label}</span><div><strong>${title}</strong><p>${detail}</p></div><b>${captured ? "캡처됨" : "미캡처"}</b>${action}</li>`;
  }).join("");

  const allCaptured = WORKSPACE_CAPTURE_ROLES.every(([label]) => workspace.captures[label]);
  const previewForm = document.querySelector("#workspace-preview-form");
  const canPreview = canIntent("preview_workspace");
  previewForm.hidden = !allCaptured || Boolean(workspace.preview) || Boolean(workspace.promotion);
  previewForm.querySelectorAll("input").forEach((input) => { input.disabled = !canPreview; });
  document.querySelector("#preview-workspace").hidden = !canPreview;

  const previewStatus = document.querySelector("#workspace-preview-status");
  if (workspace.preview) {
    const withinTolerance = workspace.preview.status === "CANDIDATE_WITHIN_TOLERANCE";
    const saveOp = canIntent("save_workspace") ? "save_workspace" : canIntent("save_workspace_revision") ? "save_workspace_revision" : null;
    const discardOp = canIntent("discard_workspace_preview") ? "discard_workspace_preview" : null;
    previewStatus.innerHTML = `<div class="notice ${withinTolerance ? "workspace-pass" : "workspace-fail"}"><strong>${withinTolerance ? "계산 결과 사용 가능" : "계산 결과 저장 불가"}</strong><span>${withinTolerance ? "세 기준점과 두 실측값이 등록 허용 범위 안에 있습니다." : "측정값이 등록 허용 범위를 벗어났습니다."}</span></div>${saveOp ? `<button type="button" data-workspace-op="${saveOp}">${message("action", "save_workspace")}</button>` : discardOp ? `<button type="button" data-workspace-op="${discardOp}">${message("action", discardOp)}</button>` : ""}`;
  } else {
    previewStatus.innerHTML = "";
  }

  const promotion = document.querySelector("#workspace-promotion");
  promotion.innerHTML = workspace.promotion ? `<div class="notice workspace-pass"><strong>${escapeHtml(workspace.display_name)} 작업영역이 저장되었습니다.</strong><span>저장된 작업영역은 수집 계획에서 선택할 수 있습니다. 실제 동작 검증이 필요한 상태는 별도로 표시됩니다.</span></div>` : "";

  let nextAction;
  if (workspace.promotion) nextAction = "수집 계획으로 돌아가 저장된 작업영역을 선택하세요.";
  else if (workspace.preview) nextAction = canIntent("save_workspace") || canIntent("save_workspace_revision") ? "계산 결과를 저장하세요." : canIntent("discard_workspace_preview") ? "이 계산 결과를 폐기한 뒤 기준점을 다시 캡처하세요." : "현재 계산 결과는 저장할 수 없습니다.";
  else if (allCaptured) nextAction = canPreview ? "두 100 mm 눈금의 실측값을 입력해 계산 결과를 확인하세요." : "현재 상태에서는 계산 결과를 만들 수 없습니다.";
  else {
    const [label, title] = WORKSPACE_CAPTURE_ROLES.find(([role]) => !workspace.captures[role]);
    nextAction = canCapture ? `${label} ${title}을 캡처하세요.` : `${label} 캡처를 현재 사용할 수 없습니다.`;
  }
  document.querySelector("#workspace-next-action").textContent = nextAction;
}

function selectedLabel(view, axis) {
  return catalogOption(view, axis, view.draft.selection[axis])?.label ?? "확인 필요";
}

function samplingProfileText(profile, approach) {
  if (!profile) return "선택 조합에 프로필 없음";
  const distribution = approach ? profile.parameter_distribution : profile.distribution;
  const parts = [profile[approach ? "approach_sampling_profile_id" : "yaw_sampling_profile_id"], distribution.kind ?? "분포 종류 미지정"];
  if (!approach) {
    const interval = profile.canonical_interval_deg;
    parts.push(`${interval.minimum}° ≤ yaw < ${interval.maximum_exclusive}°`);
    if (Number.isFinite(distribution.mean_deg) && Number.isFinite(distribution.standard_deviation_deg)) parts.push(`μ ${distribution.mean_deg}° · σ ${distribution.standard_deviation_deg}°`);
  } else {
    const clearance = distribution.align_clearance_m;
    if (clearance && Number.isFinite(clearance.minimum) && Number.isFinite(clearance.maximum)) parts.push(`정렬 높이 ${(clearance.minimum * 1000).toFixed(1)}–${(clearance.maximum * 1000).toFixed(1)} mm`);
    const offset = distribution.view_offset_xy_m;
    if (offset && Number.isFinite(offset.maximum_radius_fraction) && Number.isFinite(offset.absolute_maximum_radius_m)) parts.push(`XY 반경 ≤ 물체축 ${offset.maximum_radius_fraction}× · ${(offset.absolute_maximum_radius_m * 1000).toFixed(1)} mm`);
  }
  parts.push(profile.required_camera_roles ? `필요 카메라 ${profile.required_camera_roles.join("+")}` : "필요 카메라 미지정");
  return parts.join(" · ");
}

function stateSpaceDesignText(profile) {
  if (!profile) return "선택 조합에 실험 설계 없음";
  const spatial = profile.spatial_strata;
  const tiers = profile.derived_yaw_cdf_tiers.map((tier) => (
    `[${tier.yaw_deg.minimum}°, ${tier.yaw_deg.maximum_exclusive}°)`
  )).join(" / ");
  return `${profile.state_space_design_profile_id} · ${profile.profile_digest.slice(0, 19)}… · Nₓ×Nᵧ×N_yaw 설정값 ${spatial.columns}×${spatial.rows}×${profile.yaw_cdf_strata} · 설계 정격(endpoint당, repeat=1): 1 sweep ${profile.per_workspace_repeat_one_sweep_episode_count}회 · 전체 cell×yaw ${profile.full_cell_yaw_coverage_sweeps} sweeps/${profile.per_workspace_repeat_one_full_cell_yaw_coverage_episode_count}회 · 실제 campaign prefix는 아래 slot binding 기준 · 물체 yaw profile 파생 ${tiers} · 동일 yaw 최소 전환 실행`;
}

function objectRepositionText(view, reposition) {
  if (!reposition) return "다음 물체 재배치 없음";
  const source = `${workspaceName(view, reposition.source_pose.place_id)} ${poseText(reposition.source_pose)}`;
  const target = `${workspaceName(view, reposition.target_pose.place_id)} ${poseText(reposition.target_pose)}`;
  const yaw = reposition.yaw_sample
    ? ` · yaw 표본 ${reposition.yaw_sample.sample_rank + 1}/${reposition.yaw_sample.design_size} · seed ${reposition.yaw_sample.sampling_seed}` : "";
  return `녹화 밖 재배치 · ${reposition.execution_stage} · ${reposition.start_state} · ${source} → ${target}${yaw} · binding ${reposition.binding_digest.slice(0, 19)}… · recorder/dataset 쓰기 없음`;
}

function stateSpaceSlotText(slot) {
  if (!slot) return "";
  const canonical = slot.canonical_object_yaw_deg === slot.source_object_yaw_deg
    ? "" : ` (canonical ${slot.canonical_object_yaw_deg.toFixed(3)}°)`;
  return `설계 cell r${slot.spatial_row + 1}c${slot.spatial_column + 1} (#${slot.spatial_cell_index}) · yaw CDF층 ${slot.sample_rank + 1}/${slot.design_size} · q=${slot.yaw_sample_quantile.toFixed(6)} · 실제 yaw ${slot.source_object_yaw_deg.toFixed(3)}°${canonical} · seed ${slot.sampling_seed} · binding ${slot.binding_digest.slice(0, 19)}…`;
}

function renderReview(view) {
  const direct = view.draft.authoring_mode === "DIRECT_EDIT";
  const pickPlace = isPickPlace(view);
  const plannedCells = view.coverage?.cells ?? [];
  const sequence = view.coverage?.sequence ?? [];
  const directConditionCount = Array.isArray(view.draft.direct_pairs)
    ? view.draft.direct_pairs.length : 1 + (view.draft.direct_poses?.length ?? 0);
  const range = pickPlace
    ? `${view.draft.requested_count}회 · ${spatialNodeCount(view)}개 물체 위치를 ${direct ? "직접" : "자동 공간 우선"} 출발→도착 순서로 실행 · ${selectedLabel(view, "split")}`
    : direct
      ? `${view.draft.requested_count}회 · ${plannedCells.length || directConditionCount}개 조건을 표시 순서로 실행 · ${selectedLabel(view, "split")}`
      : `${view.draft.requested_count}회 · 조건별 최대 ${view.draft.repeat}회 · ${selectedLabel(view, "split")}`;
  const rows = [
    [pickPlace ? "출발→도착 작업영역" : "작업영역", pickPlace
      ? workspaceRoute(view).slice(0, 2).map((item) => workspaceName(view, item.workspace_id)).join(" → ")
      : `${selectedLabel(view, "workspace")} · 내부 좌표계 ${selectedLabel(view, "frame")}`],
    [pickPlace ? "첫 물체 출발점" : "현재 물체 위치", poseText(view.draft.current_object_pose)],
    ["작업", selectedLabel(view, "task")],
    ["물체와 잡기", `${selectedLabel(view, "object")} · ${selectedLabel(view, "grasp")}`],
    ["최종 집기 접근", selectedLabel(view, "variant")],
    ["시작 자세", view.start_pose_setup ? `${view.start_pose_setup.selected_start_pose_ids.length}개 선택` : selectedLabel(view, "start")],
    ["카메라", selectedLabel(view, "camera")],
    ["수집 범위", range],
    ["Campaign seed", String(view.draft.normalized_seed)],
    ["데이터 모드", selectedLabel(view, "data_mode")],
  ];
  const provenance = view.sampling_provenance;
  if (provenance) {
    rows.splice(5, 0,
      ["물체 크기", provenance.object_dimensions_mm ? `${provenance.object_dimensions_mm.join(" × ")} mm` : "선택 조합에 크기 없음"],
      ["Yaw 표본", samplingProfileText(provenance.yaw_sampling_profile, false)],
      ["상태공간 실험 설계", stateSpaceDesignText(provenance.state_space_design_profile)],
      ["접근 표본", samplingProfileText(provenance.approach_sampling_profile, true)],
    );
  }
  const manifestDigest = view.campaign_review?.manifest_digest
    ?? view.campaign_envelope?.manifest_digest;
  rows.splice(1, 0, [
    "실행 계획",
    manifestDigest
      ? `작성안 r${view.draft.revision}에서 고정 · ${manifestDigest.slice(0, 19)}…`
      : `현재 작성안 r${view.draft.revision} · 아직 고정되지 않음`,
  ]);
  const tuning = view.campaign_review?.gripper_tuning;
  if (tuning) {
    const tuningLabel = tuning.status === "CANDIDATE_PENDING_HIL"
      ? "TEST_ONLY 조정 후보" : "검증된 등록 프로필";
    rows.splice(4, 0, [
      "그리퍼 설정",
      `${tuning.command_percent}% · 허용 피드백 ${tuning.acceptable_feedback_percent.min}–${tuning.acceptable_feedback_percent.max}% · 닫기 속도 ${tuning.velocity_percent}% / 힘 ${tuning.force_percent}% · 열기 속도 ${tuning.open_velocity_percent}% / 힘 ${tuning.open_force_percent}% · ${tuningLabel}`,
    ]);
  }
  if (view.campaign_review?.speed_limit) rows.push(["속도 상한", view.campaign_review.speed_limit]);
  document.querySelector("#review-summary").innerHTML = rows.map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
  const reviewPlan = document.querySelector("#review-plan");
  reviewPlan.hidden = sequence.length === 0;
  document.querySelector("#review-plan-list").innerHTML = sequence.map((item) => {
    const start = item.start_pose_id
      ? `로봇 시작: ${startPoseLabel(view, item.start_pose_id)} · ` : "";
    const route = item.destination_pose
      ? `출발 ${workspaceName(view, item.place_id)} · ${poseText(item)} → 도착 ${workspaceName(view, item.destination_pose.place_id)} · ${poseText(item.destination_pose)}`
      : poseText(item);
    const instruction = item.instruction
      ? `<small>VLA 지시문 · ${escapeHtml(item.instruction)}</small>` : "";
    const stateSpace = item.state_space_slot
      ? `<small>${escapeHtml(stateSpaceSlotText(item.state_space_slot))}</small>` : "";
    const reposition = `<small>${escapeHtml(objectRepositionText(view, item.object_reposition))}</small>`;
    return `<li><span>${escapeHtml(item.order_index)}</span><strong>${escapeHtml(start + route)}</strong>${instruction}${stateSpace}${reposition}</li>`;
  }).join("");
  document.querySelector("#review-actions").innerHTML = canIntent("authorize_campaign") ? `<button type="button" data-op="authorize_campaign">${message("action", "authorize_campaign")}</button>` : "";
  document.querySelector("#review-back").disabled = !canIntent("edit_campaign_draft");
}

function campaignStatus(view) {
  return view.campaign_session?.campaign ?? view.campaign_operator?.campaign ?? view.campaign_status ?? {};
}

function renderFacts(view) {
  const state = campaignStatus(view);
  const total = view.campaign_envelope?.episode_count ?? view.draft.requested_count;
  const completed = state.completed_intents ?? view.episode_history?.length ?? 0;
  const campaignProgress = Number.isFinite(view.runtime.campaign_progress)
    ? Math.round(view.runtime.campaign_progress)
    : (total > 0 ? Math.round(100 * completed / total) : 0);
  const current = view.runtime.current_episode ?? (view.runtime.workflow_state === "RUNNING" ? completed + 1 : null);
  const next = view.runtime.next_episode ?? (current && current < total ? current + 1 : null);
  const recorder = view.runtime.recorder?.label ?? view.runtime.recorder?.status;
  const motion = view.runtime.motion?.label ?? view.runtime.motion?.status;
  const facts = document.querySelector("#campaign-facts");
  facts.hidden = !view.campaign_envelope && !view.episode_history?.length;
  facts.innerHTML = `<div data-fact="completed"><span>전체 진행</span><strong>${escapeHtml(completed)}/${escapeHtml(total)} · ${escapeHtml(campaignProgress)}%</strong></div>
    <div data-fact="current"><span>현재 에피소드</span><strong>${current ? `${escapeHtml(current)}/${escapeHtml(total)}` : "없음"}</strong></div>
    <div data-fact="next"><span>다음 에피소드</span><strong>${next ? `${escapeHtml(next)}/${escapeHtml(total)}` : "없음"}</strong></div>
    ${motion ? `<div data-fact="motion"><span>로봇 동작</span><strong>${escapeHtml(motion)}</strong></div>` : ""}
    ${recorder ? `<div data-fact="recorder"><span>기록기</span><strong>${escapeHtml(recorder)}</strong></div>` : ""}`;
}

function renderRuntime(view) {
  const runtime = view.runtime;
  const stateText = message("workflow", runtime.workflow_state);
  const reason = runtime.reason_codes?.length ? humanReason(runtime.reason_codes[0]) : "";
  let html = `<div class="runtime-state"><span class="pulse" aria-hidden="true"></span><div><strong>${escapeHtml(stateText)}</strong>${reason ? `<p>${escapeHtml(reason)}</p>` : ""}</div></div>`;
  if (Number.isFinite(runtime.progress) && runtime.progress >= 0 && runtime.progress <= 100) {
    html += `<div class="progress-block"><div><span>현재 에피소드 · ${escapeHtml(runtime.phase_label ?? "진행 중")}</span><strong>${escapeHtml(runtime.progress)}%</strong></div><progress max="100" value="${escapeHtml(runtime.progress)}">${escapeHtml(runtime.progress)}%</progress><p>${escapeHtml(runtime.detail ?? "")}</p></div>`;
  }
  if (runtime.motion || runtime.recorder) {
    const runtimeRows = [];
    if (runtime.motion) runtimeRows.push(["로봇 동작", runtime.motion.label]);
    if (runtime.recorder) runtimeRows.push(["기록기", runtime.recorder.label ?? message("status", runtime.recorder.status)]);
    if (Number.isInteger(runtime.recorder?.frames)) runtimeRows.push(["기록 프레임", `${runtime.recorder.frames}`]);
    if (Number.isFinite(runtime.recorder?.fps)) runtimeRows.push(["관측 속도", `${runtime.recorder.fps} fps`]);
    html += `<dl class="runtime-evidence">${runtimeRows.map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl>`;
  }
  const activePlan = view.active_episode_plan;
  if (activePlan) {
    const trajectory = activePlan.trajectory_variant_binding;
    const yaw = activePlan.yaw_sample_binding;
    const yawText = yaw
      ? ` · yaw ${yaw.source_object_yaw_deg.toFixed(3)}° (CDF ${yaw.sample_rank + 1}/${yaw.design_size}, seed ${yaw.sampling_seed})`
      : "";
    html += `<details class="runtime-evidence"><summary>현재 실행할 정확한 궤적</summary><dl>
      <div><dt>경로</dt><dd>${escapeHtml(activePlan.operator_summary.path.join(" → "))}</dd></div>
      <div><dt>변형</dt><dd>${escapeHtml(trajectory.trajectory_variant_id)} · ${escapeHtml(trajectory.sample_rank + 1)}/${escapeHtml(trajectory.design_size)} · seed ${escapeHtml(trajectory.sampling_seed)}${escapeHtml(yawText)}</dd></div>
      <div><dt>목표 yaw</dt><dd>${escapeHtml(trajectory.target_yaw_deg)}°</dd></div>
      <div><dt>해석된 매개변수</dt><dd><code>${escapeHtml(JSON.stringify(trajectory.phase_parameters))}</code></dd></div>
      <div><dt>충돌 증거</dt><dd>${escapeHtml(activePlan.precommit_safety.collision_report_digest)}</dd></div>
      <div><dt>Plan-only 무동작 증거</dt><dd>${escapeHtml(activePlan.precommit_safety.plan_only_no_motion_digest)}</dd></div>
      <div><dt>계획 digest</dt><dd>${escapeHtml(activePlan.plan_digest)}</dd></div>
    </dl></details>`;
  }
  if (runtime.evidence) {
    const evidence = runtime.evidence;
    const rows = [
      ["재배치 binding", evidence.object_reposition_binding_digest],
      ["재배치 run", evidence.object_reposition_run_id],
      ["재배치 계획", evidence.object_reposition_plan_digest],
      ["계획 artifact", evidence.object_reposition_plan_artifact_digest],
      ["충돌 증거", evidence.object_reposition_collision_report_digest],
      ["Plan-only 무동작 증거", evidence.object_reposition_plan_only_no_motion_digest],
    ];
    html += `<details class="runtime-evidence" open><summary>녹화 밖 물체 재배치 계획 증거</summary><dl>${rows.map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl></details>`;
  }
  if (runtime.workflow_state === "BLOCKED" && canIntent("new_campaign_same_settings")) {
    html += `<div class="recovery-card"><strong>이번 실행은 종료되었습니다.</strong><p>필요하면 로봇을 HOME으로 복귀한 뒤 같은 설정의 새 계획을 만드세요.</p>${canIntent("recover_home") ? '<button type="button" data-recovery-op="recover_home">그리퍼 열고 HOME 복귀</button>' : ""}<button type="button" data-recovery-op="new_campaign_same_settings">종료된 실행 닫고 새 계획</button></div>`;
  }
  document.querySelector("#runtime-content").innerHTML = html;
  const showCancel = ["RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR"].includes(runtime.workflow_state);
  const cancelling = runtime.workflow_state === "CANCELLING" || cancelPending;
  cancelButton.hidden = !showCancel;
  cancelButton.disabled = cancelling || !canImmediateCancel();
  cancelButton.textContent = cancelling ? "중단 처리 중" : message("action", "cancel_session");
}

function measurementLabel(value) {
  if (value === undefined || value === null || value === "") return "측정 자료 없음";
  return message("status", value, "확인 필요");
}

function semanticReviewLabel(value) {
  return {PASS: "사용 후보", FAIL: "제외", UNCERTAIN: "보류"}[value] ?? measurementLabel(value);
}

function episodeNoteLabel(item) {
  const ledgerReview = item.episode_ledger?.review_status;
  const semantic = ledgerReview && ledgerReview !== "NOT_MEASURED"
    ? ledgerReview
    : item.human_semantic ?? ledgerReview;
  return semantic === "NOT_MEASURED" ? message("status", semantic) : semanticReviewLabel(semantic);
}

function episodeRetentionLabel(item) {
  const retention = {PRESERVE: "보존"}[item.episode_ledger?.retention_state];
  const reclaim = {NOT_EVALUATED: "회수 미평가", REPACK_REQUIRED: "재패킹 필요"}[item.episode_ledger?.reclaim_state];
  return [retention, reclaim].filter(Boolean).join(" · ");
}

function renderResults(view) {
  const history = view.episode_history ?? [];
  document.querySelector("#episode-results").innerHTML = history.length ? history.map((item, index) => {
    const technical = item.technical_evidence?.status ?? item.technical_status;
    const retention = episodeRetentionLabel(item);
    return `<li><span>${index + 1}</span><div><strong>에피소드 ${index + 1}</strong><p>기술 검사 ${escapeHtml(measurementLabel(technical))}${retention ? ` · ${escapeHtml(retention)}` : ""}</p></div><b>${escapeHtml(episodeNoteLabel(item))}</b></li>`;
  }).join("") : "<li class=\"empty-result\">완료된 에피소드가 없습니다.</li>";

  const cells = view.coverage?.cells ?? view.draft.cells;
  document.querySelector("#coverage-summary").innerHTML = cells.map((cell) => {
    const count = Number.isInteger(cell.collected_count) ? cell.collected_count : Number.isInteger(cell.coverage_count) ? cell.coverage_count : 0;
    const target = Number.isInteger(cell.target_count) && cell.target_count > 0 ? cell.target_count : Math.max(view.draft.repeat, 1);
    return `<div><span>X ${escapeHtml(cell.x_mm)} · Y ${escapeHtml(cell.y_mm)} · ${escapeHtml(cell.yaw_deg)}°</span><progress max="${escapeHtml(target)}" value="${escapeHtml(Math.min(count, target))}">${escapeHtml(count)}/${escapeHtml(target)}</progress><strong>${escapeHtml(count)}/${escapeHtml(target)}</strong></div>`;
  }).join("");

  const review = view.candidate_review;
  const reviewQueue = document.querySelector("#review-queue");
  if (!review) {
    const passed = history.filter((item) => (item.technical_evidence?.status ?? item.technical_status) === "PASS").length;
    delete reviewQueue.dataset.reviewBindingDigest;
    delete reviewQueue.dataset.reviewRenderKey;
    delete reviewQueue.dataset.reviewReasonDraft;
    reviewQueue.innerHTML = `<div class="notice"><strong>분류 대기 0개</strong><span>${escapeHtml(passed)}개 에피소드가 기술 검사를 통과했습니다. 보존 상태와 학습 사용 승인은 별도입니다.</span></div>`;
    return;
  }
  const pending = review.status === "PENDING" && canIntent("review_candidate");
  if (review.status === "UNAVAILABLE") {
    delete reviewQueue.dataset.reviewRenderKey;
    reviewQueue.innerHTML = `<div class="notice"><strong>분류 상태를 확인할 수 없습니다</strong><span>저장된 검토 결과를 다시 확인할 때까지 분류 요청을 보내지 않습니다.</span></div>`;
    return;
  }
  const reasons = Array.isArray(review.reasons) ? review.reasons : [];
  const pose = review.coverage_condition;
  const context = [
    Number.isInteger(review.episode_number) ? `에피소드 ${review.episode_number}` : null,
    Number.isInteger(review.queue_remaining) ? `남은 분류 ${review.queue_remaining}개` : null,
    pose && Number.isFinite(pose.x_mm) && Number.isFinite(pose.y_mm) && Number.isFinite(pose.yaw_deg)
      ? `X ${pose.x_mm} · Y ${pose.y_mm} · ${pose.yaw_deg}°`
      : null,
  ].filter(Boolean).join(" · ");
  const reviewRenderKey = JSON.stringify([
    review.review_binding_digest, review.status, review.queue_remaining,
    pending, reasons, pose,
  ]);
  if (reviewQueue.dataset.reviewRenderKey === reviewRenderKey) return;
  const previousReason = document.querySelector("#candidate-reason");
  const sameBinding = reviewQueue.dataset.reviewBindingDigest === review.review_binding_digest;
  if (sameBinding && previousReason) reviewQueue.dataset.reviewReasonDraft = previousReason.value;
  if (!sameBinding) delete reviewQueue.dataset.reviewReasonDraft;
  const reasonDraft = reviewQueue.dataset.reviewReasonDraft ?? "";
  const restoreFocus = document.activeElement === previousReason;
  reviewQueue.dataset.reviewBindingDigest = review.review_binding_digest;
  reviewQueue.dataset.reviewRenderKey = reviewRenderKey;
  reviewQueue.innerHTML = `<section class="review-card" aria-labelledby="review-queue-title"><div><p class="step-number">수집 데이터 분류</p><h3 id="review-queue-title">기술 검사를 통과한 에피소드를 분류하세요.</h3>${context ? `<p>${escapeHtml(context)}</p>` : ""}</div>
    ${pending ? `<label for="candidate-reason">실패 또는 보류 이유<select id="candidate-reason" required><option value="">이유 선택</option>${reasons.map((reason) => `<option value="${escapeHtml(reason)}">${escapeHtml(message("review_reason", reason))}</option>`).join("")}</select></label>
    <div class="admission-actions"><button type="button" data-review-choice="PASS">사용 후보</button><button type="button" data-review-choice="FAIL">제외</button><button type="button" data-review-choice="UNCERTAIN">보류</button></div>` : `<p>${escapeHtml(semanticReviewLabel(review.status))}</p>`}
    <p class="cell-help">이 분류는 데이터의 사후 사용 후보 상태만 기록합니다. 파일 보존과 학습 사용 승인은 바꾸지 않습니다.</p></section>`;
  const nextReason = document.querySelector("#candidate-reason");
  if (nextReason && reasons.includes(reasonDraft)) nextReason.value = reasonDraft;
  if (restoreFocus) nextReason?.focus({preventScroll: true});
}

function renderNext(view) {
  document.querySelector("#results-next").disabled = view.runtime.workflow_state !== "TERMINAL";
  document.querySelector("#same-settings-action").innerHTML = canIntent("new_campaign_same_settings") ? `<button type="button" data-op="new_campaign_same_settings">${message("action", "new_campaign_same_settings")}</button>` : "<p>현재 상태에서는 새 수집 계획을 만들 수 없습니다.</p>";
}

function renderTechnicalDetails(view) {
  renderTechnical({
    session_id: view.session_id,
    view_revision: view.revision,
    view_digest: view.view_digest,
    draft_id: view.draft.draft_id,
    draft_revision: view.draft.revision,
    normalized_seed: view.draft.normalized_seed,
    sampling_provenance: view.sampling_provenance,
    active_episode_plan: view.active_episode_plan,
    object_reposition_bindings: view.coverage?.sequence?.map((item) => item.object_reposition),
    object_reposition_runtime_evidence: view.runtime.evidence,
    workspace_id: view.draft.selection.workspace,
    frame_id: view.draft.selection.frame,
    workspace_registration_session_id: view.workspace_registration?.session_id,
    workspace_registration_calibration_id: view.workspace_registration?.calibration_id,
    workspace_registration_display_name: view.workspace_registration?.display_name,
    selected_start_pose_ids: view.start_pose_setup?.selected_start_pose_ids,
    start_pose_profiles: view.start_pose_setup?.profiles.map(({start_pose_id, status}) => ({start_pose_id, status})),
    state_space_summary: view.state_space_summary,
    camera_devices: view.camera_setup?.devices.map(({logical_id, status, technical_identity}) => ({logical_id, status, technical_identity})),
    camera_bindings: view.camera_setup?.bindings,
    compatibility_digest: view.catalog.compatibility_digest,
    manifest_digest: view.campaign_envelope?.manifest_digest,
    envelope_digest: view.campaign_envelope?.envelope_digest,
    authorization_digest: view.campaign_authorization?.authorization_digest,
    workspace_preview_digest: view.workspace_registration?.preview?.preview_digest,
    workspace_promotion_digest: view.workspace_registration?.promotion?.promotion_digest,
    reason_codes: view.runtime.reason_codes,
    result_digests: view.episode_history?.map((item) => item.result_digest).filter(Boolean),
    ledger_digests: view.episode_history?.map((item) => item.episode_ledger?.ledger_digest).filter(Boolean),
    retention_states: view.episode_history?.map((item) => item.episode_ledger?.retention_state).filter(Boolean),
    reclaim_states: view.episode_history?.map((item) => item.episode_ledger?.reclaim_state).filter(Boolean),
    effect_counts: view.effect_counts,
  });
}

function renderCollectionAdvice(view) {
  const panel = document.querySelector("#collection-advice");
  const advice = view.collection_advice;
  panel.hidden = !advice;
  if (!advice) return;
  const labels = {
    NOT_CHECKED: "이전 수집의 저장 근거를 확인하면 다음 조건을 살펴볼 수 있습니다.",
    READY: "기존 고정 조건을 유지하면서 미수집 조건을 제안합니다. 비교한 뒤 적용하거나 현재 설정을 유지하세요.",
    UNAVAILABLE: "현재 근거와 설정으로 적용할 수 있는 추천이 없습니다. 현재 설정으로 계속할 수 있습니다.",
    DRAFT_CHANGED: "추천 확인 뒤 작성안이 바뀌었습니다. 현재 수정은 유지됩니다. 근거를 다시 확인하세요.",
    APPLIED: "추천 조건을 작성안에 적용했습니다. 아직 수집을 실행하지 않았습니다.",
    KEPT: "현재 설정을 유지했습니다. 추천으로 작성안을 변경하지 않았습니다.",
  };
  const reasons = {
    COLLECTION_ADVICE_NO_STORED_EVIDENCE: "이전 실행에 저장된 수집 근거가 없습니다.",
    COLLECTION_ADVICE_NO_ELIGIBLE_CHANGE: "수집하지 않은 적격 조건이 없거나, 기존 고정·제외 조건을 유지해야 합니다.",
    COLLECTION_ADVICE_SELECTION_CHANGED: "이전 수집과 현재 대상·설정이 다릅니다.",
    COLLECTION_ADVICE_PLACEMENT_OR_SPLIT_MISMATCH: "추천의 첫 위치 또는 데이터 분할이 현재 작성안과 다릅니다. 물체 위치는 바꾸지 않았습니다.",
    COLLECTION_ADVICE_START_SELECTION_MISMATCH: "추천에 필요한 시작 자세가 현재 선택에 없습니다.",
    COLLECTION_ADVICE_TRANSITION_NOT_REPRESENTABLE: "이 추천에는 놓기 작업의 도착점·이동 결속이 없어 적용할 수 없습니다.",
    COLLECTION_ADVICE_SEQUENCE_NOT_REPRESENTABLE: "추천의 조건 순서를 현재 작성안으로 보존할 수 없습니다.",
    COLLECTION_ADVICE_SOURCE_CHANGED: "저장 근거가 이전 수집의 결속과 달라졌습니다.",
  };
  const status = advice.status === "DRAFT_CHANGED" && advice.last_choice
    ? "이미 처리한 추천입니다. 이후 수정한 작성안을 유지하며, 같은 추천을 다시 적용하지 않습니다."
    : labels[advice.status] ?? "추천 상태를 다시 확인하세요.";
  document.querySelector("#collection-advice-status").textContent = [status,
    ...(advice.reason_codes ?? []).map((code) => reasons[code] ?? "저장 근거를 확인할 수 없습니다. 세부 근거를 확인하세요.")].join(" ");
  document.querySelector("#collection-advice-conditions").innerHTML = (advice.conditions ?? []).map((item) =>
    `<li><strong>${advice.native_selection?.pinned.includes(item.slot.slot_id) ? "고정 조건 유지" : "미수집 조건"}</strong> · ${escapeHtml(poseText(item.condition))} · ${escapeHtml(item.slot.robot_start_pose_id)} · ${escapeHtml(item.slot.split_group)}</li>`).join("");
  document.querySelector("#collection-advice-refresh").disabled = !canIntent("refresh_collection_advice");
  for (const choice of ["apply", "keep"]) {
    const button = document.querySelector(`#collection-advice-${choice}`);
    button.hidden = advice.status !== "READY";
    button.disabled = !canIntent("choose_collection_advice");
  }
  document.querySelector("#collection-advice-evidence").textContent = JSON.stringify({
    recommendation: advice.recommendation, coverage: advice.data_quality_analysis,
    reason_codes: advice.reason_codes, last_choice: advice.last_choice,
  }, null, 2);
}

function render(view) {
  viewStale = false;
  clearTimeout(recoveryTimer);
  recoveryAttempts = 0;
  disabledBeforeFailure?.forEach((control) => { if (control.isConnected) control.disabled = false; });
  disabledBeforeFailure = undefined;
  if (view.session_id !== lastSession
      || !["RUNNING", "CANCELLING", "PAUSED_AWAITING_OPERATOR"].includes(view.runtime.workflow_state)) cancelPending = false;
  currentView = view;
  lastSession = view.session_id;
  lastRevision = view.revision;
  lastDigest = view.view_digest;
  renderConnection(view);
  renderSetup(view);
  renderCatalog(view);
  renderWorkspaceRegistration(view);
  renderStartPoseSetup(view);
  renderReview(view);
  renderFacts(view);
  renderRuntime(view);
  renderResults(view);
  renderNext(view);
  renderCollectionAdvice(view);
  renderTechnicalDetails(view);
  renderSteps(view);
}

async function watchView() {
  if (!currentView || viewStale || currentView.connection_state !== "READY" || watchController) return;
  const boundSession = currentView.session_id;
  const afterRevision = currentView.revision;
  const controller = new AbortController();
  watchController = controller;
  const timeout = setTimeout(() => controller.abort(new DOMException("State watch timed out", "TimeoutError")), 70000);
  let stage = "connection";
  try {
    const response = await fetch(`/api/view/watch?after_revision=${afterRevision}`, {
      method: "GET", credentials: "same-origin", cache: "no-store",
      headers: tokenHeaders(), signal: controller.signal,
    });
    stage = "response";
    const view = await readViewResponse(response);
    if (controller.signal.aborted) return;
    if (controller.signal.aborted || !currentView || currentView.session_id !== boundSession) return;
    if (view.connection_state !== "READY") return failClose(view.runtime.reason_codes?.[0] ?? "VIEW_STALE", view.connection_state);
    if (view.revision !== currentView.revision || view.view_digest !== currentView.view_digest) render(view);
  } catch (error) {
    if (watchController === controller && (!controller.signal.aborted || controller.signal.reason?.name === "TimeoutError")) failViewRequest(error, stage);
  } finally {
    clearTimeout(timeout);
    if (watchController === controller) {
      watchController = undefined;
      watchView();
    }
  }
}

function intentPayload(op) {
  if (op === "compile_draft") return {draft_id: currentView.draft.draft_id, data_disposition: currentView.data_disposition};
  if (op === "authorize_campaign") return {
    draft_id: currentView.draft.draft_id,
    manifest_digest: currentView.campaign_envelope.manifest_digest,
    envelope_digest: currentView.campaign_envelope.envelope_digest,
    data_disposition: currentView.data_disposition,
  };
  return currentView.next_campaign?.actions?.[op]?.payload ?? {};
}

async function submitIntent(op, payload = intentPayload(op)) {
  if (!canIntent(op)) return failClose("VIEW_STALE", "OP_NOT_AVAILABLE");
  const boundView = currentView;
  intentBusy = true;
  render(boundView);
  const envelope = {
    schema_version: INTENT_SCHEMA,
    intent_id: crypto.randomUUID(),
    session_id: boundView.session_id,
    view_revision: boundView.revision,
    view_digest: boundView.view_digest,
    op,
    payload,
  };
  let rejectionCode, recoveryNotice;
  try {
    const response = await fetch("/api/intent", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {...tokenHeaders(), "Content-Type": "application/json"},
      body: JSON.stringify(envelope),
    });
    const result = await response.json();
    if (result.schema_version !== RESULT_SCHEMA || typeof result.ok !== "boolean" || typeof result.consumed !== "boolean" || result.ok !== result.consumed) throw new TypeError("INTENT_RESULT_INVALID");
    if (!response.ok || !result.ok) {
      const code = typeof result.code === "string" ? result.code : "VERSION_CONFLICT";
      rejectionCode = code;
      if (op === "review_candidate") recoveryNotice = `${humanReason(code)}. 최신 분류 대상을 다시 확인하고 분류를 다시 선택하세요. 요청은 실행되지 않았습니다.`;
    }
  } catch (error) {
    recoveryNotice = "요청 응답을 받지 못해 최신 상태로 다시 맞췄습니다. 요청을 재전송하지 않았습니다.";
  }
  await loadView({releaseIntent: true, rejectionCode, recoveryNotice});
}

async function submitImmediateCancel() {
  if (cancelPending) return;
  if (!canImmediateCancel()) return failClose("VIEW_STALE", "CANCEL_NOT_AVAILABLE");
  const boundView = currentView;
  cancelPending = true;
  render(boundView);
  const envelope = {
    schema_version: INTENT_SCHEMA,
    intent_id: crypto.randomUUID(),
    session_id: boundView.session_id,
    view_revision: boundView.revision,
    view_digest: boundView.view_digest,
    op: "cancel_session",
    payload: {active_child_id: boundView.runtime.active_child_id},
  };
  let rejectionCode, recoveryNotice;
  try {
    const response = await fetch("/api/intent", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {...tokenHeaders(), "Content-Type": "application/json"},
      body: JSON.stringify(envelope),
    });
    const result = await response.json();
    if (result.schema_version !== RESULT_SCHEMA || typeof result.ok !== "boolean" || typeof result.consumed !== "boolean" || result.ok !== result.consumed) throw new TypeError("INTENT_RESULT_INVALID");
    if (!response.ok || !result.ok) {
      rejectionCode = typeof result.code === "string" ? result.code : "VERSION_CONFLICT";
      cancelPending = false;
    }
  } catch (_error) {
    recoveryNotice = "중단 요청 응답을 받지 못했습니다. 중복 전송하지 않고 최신 상태만 확인합니다.";
  }
  await loadView({rejectionCode, recoveryNotice});
}

async function loadView({releaseIntent = false, rejectionCode, recoveryNotice, automaticRecovery = false} = {}) {
  stopWatch();
  clearTimeout(recoveryTimer);
  if (!automaticRecovery) recoveryAttempts = 0;
  releaseIntent ||= viewController?.releaseIntent;
  viewController?.abort();
  const controller = new AbortController();
  controller.releaseIntent = releaseIntent;
  viewController = controller;
  const timeout = setTimeout(() => controller.abort(new DOMException("State read timed out", "TimeoutError")), 10000);
  let stage = "connection";
  if (!viewStale) setBanner("최신 상태를 다시 읽고 있습니다. 이 동안 요청을 보내지 않습니다.", "info", false);
  try {
    const response = await fetch("/api/view", {method: "GET", credentials: "same-origin", cache: "no-store", headers: tokenHeaders(), signal: controller.signal});
    stage = "response";
    const view = await readViewResponse(response);
    if (viewController !== controller) return;
    if (releaseIntent) intentBusy = false;
    if (view.connection_state !== "READY") {
      failClose(view.runtime.reason_codes?.[0] ?? "VIEW_STALE", view.connection_state);
      return;
    }
    render(view);
    if (recoveryNotice) setBanner(recoveryNotice, "bad");
    else if (rejectionCode) setBanner(`${humanReason(rejectionCode)}. 요청은 실행되지 않았습니다.`, "bad");
    watchView();
  } catch (error) {
    if (viewController !== controller) return;
    if (releaseIntent) intentBusy = false;
    failViewRequest(error, stage);
  } finally {
    clearTimeout(timeout);
    if (viewController === controller) viewController = undefined;
  }
}

document.querySelector(".step-rail").addEventListener("click", (event) => {
  const button = event.target.closest("[data-step-target]");
  if (button) showStep(button.dataset.stepTarget, true);
});
document.querySelector("#environment-next").addEventListener("click", () => showStep("plan", true));
document.querySelector("#prepare-environment").addEventListener("click", () => submitIntent("prepare_environment", {}));
document.querySelector("#recover-home").addEventListener("click", () => submitIntent("recover_home", {}));
document.querySelector("#camera-role-list").addEventListener("change", (event) => {
  const select = event.target.closest("select[data-camera-logical-id]");
  if (!select || !currentView?.camera_setup || !canIntent("update_camera_bindings")) return;
  const logicalId = select.dataset.cameraLogicalId;
  const role = select.value;
  const bindings = {...currentView.camera_setup.bindings};
  const previousRole = bindings[logicalId];
  const availableRoles = currentView.camera_setup.available_roles ?? CAMERA_ROLES.map(([id]) => id);
  if (!Object.hasOwn(bindings, logicalId) || !availableRoles.includes(role)) return renderCameraSetup(currentView);
  if (role !== "UNUSED") {
    const occupied = Object.entries(bindings).find(([deviceId, assignedRole]) => deviceId !== logicalId && assignedRole === role);
    if (occupied) bindings[occupied[0]] = previousRole;
  }
  bindings[logicalId] = role;
  const assigned = Object.values(bindings).filter((assignedRole) => assignedRole !== "UNUSED");
  if (new Set(assigned).size !== assigned.length) return renderCameraSetup(currentView);
  submitIntent("update_camera_bindings", {bindings});
});
document.querySelector("#open-workspace").addEventListener("click", () => {
  if (currentView?.runtime.workflow_state === "AUTHORING" && !document.querySelector("#workspace-entry").hidden) document.querySelector("#workspace-dialog").showModal();
});
document.querySelector("#close-workspace").addEventListener("click", () => document.querySelector("#workspace-dialog").close());
document.querySelector("#workspace-name-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const displayName = document.querySelector("#workspace-display-name").value.trim();
  if (!canIntent("new_workspace_registration") || !displayName || !form.checkValidity()) return form.reportValidity();
  submitIntent("new_workspace_registration", {display_name: displayName});
  form.reset();
});
document.querySelector("#workspace-captures").addEventListener("click", (event) => {
  const button = event.target.closest("[data-capture-label]");
  if (button && WORKSPACE_CAPTURE_ROLES.some(([label]) => label === button.dataset.captureLabel)) {
    submitIntent("capture_workspace_point", {label: button.dataset.captureLabel});
  }
});
document.querySelector("#workspace-preview-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const source = document.querySelector("#source-scale-bar").valueAsNumber;
  const final = document.querySelector("#final-scale-bar").valueAsNumber;
  if (!canIntent("preview_workspace") || !Number.isFinite(source) || !Number.isFinite(final) || !form.checkValidity()) return form.reportValidity();
  submitIntent("preview_workspace", {source_scale_bar_mm: source, final_scale_bar_mm: final});
});
document.querySelector("#workspace-preview-status").addEventListener("click", (event) => {
  const button = event.target.closest("[data-workspace-op]");
  const digest = currentView?.workspace_registration?.preview?.preview_digest;
  if (button && ["save_workspace", "save_workspace_revision", "discard_workspace_preview"].includes(button.dataset.workspaceOp) && DIGEST_PATTERN.test(digest)) submitIntent(button.dataset.workspaceOp, {preview_digest: digest});
});
document.querySelector("#open-start-pose").addEventListener("click", () => {
  if (currentView?.runtime.workflow_state === "AUTHORING" && currentView.start_pose_setup) document.querySelector("#start-pose-dialog").showModal();
});
document.querySelector("#close-start-pose").addEventListener("click", () => document.querySelector("#start-pose-dialog").close());
document.querySelector("#start-pose-capture-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const displayName = document.querySelector("#start-pose-display-name").value.trim();
  if (!canIntent("capture_start_pose") || !displayName || !form.checkValidity()) return form.reportValidity();
  submitIntent("capture_start_pose", {display_name: displayName});
  form.reset();
});
document.querySelector("#start-pose-profile-list").addEventListener("change", (event) => {
  const input = event.target.closest("[data-start-pose-id]");
  if (!input || !currentView?.start_pose_setup || !canIntent("update_start_pose_selection")) return;
  const selected = new Set(currentView.start_pose_setup.selected_start_pose_ids);
  if (input.checked) selected.add(input.dataset.startPoseId);
  else if (selected.size > 1) selected.delete(input.dataset.startPoseId);
  else {
    input.checked = true;
    return;
  }
  submitIntent("update_start_pose_selection", {selected_start_pose_ids: currentView.start_pose_setup.profiles.map((profile) => profile.start_pose_id).filter((id) => selected.has(id))});
});
document.querySelector("#plan-back").addEventListener("click", () => showStep("environment", true));
document.querySelector("#review-back").addEventListener("click", () => submitIntent("edit_campaign_draft", {}));
document.querySelector("#results-next").addEventListener("click", () => showStep("next", true));
document.querySelector("#catalog-fields").addEventListener("change", (event) => {
  if (event.target.matches("select[data-axis]")) submitIntent("update_draft", {draft_id: currentView.draft.draft_id, selection: {[event.target.dataset.axis]: event.target.value}});
});
document.querySelector("#workspace-select").addEventListener("change", (event) => {
  submitIntent("update_draft", {draft_id: currentView.draft.draft_id, selection: {workspace: event.target.value}});
});
document.querySelector("#authoring-mode").addEventListener("change", (event) => {
  if (event.target.name === "authoring_mode") submitIntent("update_draft", {draft_id: currentView.draft.draft_id, authoring_mode: event.target.value});
});
["count", "repeat"].forEach((field) => document.querySelector(`#${field}-input`).addEventListener("change", (event) => {
  const value = event.target.valueAsNumber;
  if (!Number.isInteger(value) || value < Number(event.target.min) || value > Number(event.target.max)) return event.target.reportValidity();
  submitIntent("update_draft", {draft_id: currentView.draft.draft_id, [field === "count" ? "requested_count" : "repeat"]: value});
}));
document.querySelector("#seed-input").addEventListener("change", (event) => {
  const normalizedSeed = event.target.valueAsNumber;
  if (!Number.isSafeInteger(normalizedSeed) || normalizedSeed < 0 || normalizedSeed > Number(event.target.max)) return event.target.reportValidity();
  submitIntent("update_draft", {draft_id: currentView.draft.draft_id, normalized_seed: normalizedSeed});
});
document.querySelector("#motion-preset-select")?.addEventListener("change", (event) => {
  if (!currentView || !canIntent("update_draft")) return;
  const preset = (currentView.motion_presets || []).find((item) => item.id === event.target.value);
  if (event.target.value && !preset) return;
  submitIntent("update_draft", {draft_id: currentView.draft.draft_id,
    motion_preset: preset ? {id: preset.id, digest: preset.digest} : null});
});
document.querySelector("#experiment-design-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!currentView || !canIntent("update_draft") || currentView.draft.authoring_mode !== "ASSISTED") return;
  const form = event.currentTarget;
  const columnsInput = document.querySelector("#design-columns-input");
  const rowsInput = document.querySelector("#design-rows-input");
  const yawInput = document.querySelector("#design-yaw-input");
  [columnsInput, rowsInput, yawInput].forEach((input) => input.setCustomValidity(""));
  const [columns, rows, yaw_cdf_strata] = [columnsInput, rowsInput, yawInput].map((input) => input.valueAsNumber);
  if (![columns, rows, yaw_cdf_strata].every((value) => Number.isInteger(value) && value >= 1 && value <= 100)) return form.reportValidity();
  if (columns * rows > 100) rowsInput.setCustomValidity("Nₓ × Nᵧ는 100 이하여야 합니다.");
  if (yaw_cdf_strata > columns * rows) yawInput.setCustomValidity("N_yaw는 Nₓ × Nᵧ 이하여야 합니다.");
  if (!form.checkValidity()) return form.reportValidity();
  submitIntent("update_draft", {
    draft_id: currentView.draft.draft_id,
    state_space_design_factors: {columns, rows, yaw_cdf_strata},
  });
});
document.querySelector("#current-object-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!currentView || !canIntent("update_draft") || !directPoseDomain(currentView)) return;
  const form = event.currentTarget;
  const values = ["#current-object-x", "#current-object-y", "#current-object-yaw"].map((selector) => document.querySelector(selector).valueAsNumber);
  const yawInput = document.querySelector("#current-object-yaw");
  const yawDomain = directPoseDomain(currentView).yaw_deg;
  yawInput.setCustomValidity(values[2] >= yawDomain.minimum && values[2] < yawDomain.maximum_exclusive ? "" : `Yaw는 ${yawDomain.minimum}° 이상 ${yawDomain.maximum_exclusive}° 미만이어야 합니다.`);
  if (!values.every(Number.isFinite) || !form.checkValidity()) return form.reportValidity();
  submitIntent("update_draft", {
    draft_id: currentView.draft.draft_id,
    current_object_pose: {
      place_id: currentView.draft.selection.workspace,
      x_mm: values[0], y_mm: values[1], yaw_deg: values[2],
    },
  });
});
document.querySelector("#refresh-object-position")?.addEventListener("click", () => submitIntent("refresh_object_position"));
document.querySelector("#split-select").addEventListener("change", (event) => submitIntent("update_draft", {draft_id: currentView.draft.draft_id, split: event.target.value}));
document.querySelector("#cell-grid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-cell-id]");
  const cell = button && currentView?.draft.cells.find((item) => item.cell_id === button.dataset.cellId);
  if (!cell || currentView.draft.authoring_mode !== "DIRECT_EDIT") return;
  const anchor = currentView.draft.current_object_pose;
  const pairIndex = nextDirectPairIndex(currentView);
  const pairEndpoint = workspaceRoute(currentView)[pairIndex];
  const samePose = (pose) => pose && (!Array.isArray(currentView.draft.direct_pairs)
    || pose.place_id === pairEndpoint?.workspace_id)
    && ["x_mm", "y_mm", "yaw_deg"].every((field) => pose[field] === cell[field]);
  if (Array.isArray(currentView.draft.direct_pairs)) {
    const startPoseId = document.querySelector("#direct-start-select").value;
    const pair = currentView.draft.direct_pairs.find((item) => samePose(item)
      && (isPickPlace(currentView) || item.start_pose_id === startPoseId));
    const nextPair = pair ?? {start_pose_id: startPoseId, place_id: pairEndpoint.workspace_id, x_mm: cell.x_mm, y_mm: cell.y_mm, yaw_deg: cell.yaw_deg};
    return submitIntent("update_draft", {draft_id: currentView.draft.draft_id, [pair ? "remove_pair" : "add_pair"]: nextPair});
  }
  if (samePose(anchor)) return;
  const pose = currentView.draft.direct_poses?.find(samePose);
  submitIntent("update_draft", {
    draft_id: currentView.draft.draft_id,
    [pose ? "remove_pose" : "add_pose"]: pose ?? {
      place_id: currentView.draft.selection.workspace,
      x_mm: cell.x_mm, y_mm: cell.y_mm, yaw_deg: cell.yaw_deg,
    },
  });
});
document.querySelector("#direct-pose-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const pairMode = Array.isArray(currentView?.draft.direct_pairs);
  const pairIndex = pairMode ? nextDirectPairIndex(currentView) : 0;
  const endpoint = workspaceRoute(currentView)[pairIndex];
  const domain = directPoseDomain(currentView, pairIndex);
  if (!currentView || !canIntent("update_draft") || !domain || pairMode && !endpoint) return;
  const form = event.currentTarget;
  const values = ["#direct-x-input", "#direct-y-input", "#direct-yaw-input"].map((selector) => document.querySelector(selector).valueAsNumber);
  const yawInput = document.querySelector("#direct-yaw-input");
  const yawDomain = domain.yaw_deg;
  yawInput.setCustomValidity(values[2] >= yawDomain.minimum && values[2] < yawDomain.maximum_exclusive ? "" : `Yaw는 ${yawDomain.minimum}° 이상 ${yawDomain.maximum_exclusive}° 미만이어야 합니다.`);
  if (!values.every(Number.isFinite) || !form.checkValidity()) return form.reportValidity();
  submitIntent("update_draft", {
    draft_id: currentView.draft.draft_id,
    [pairMode ? "add_pair" : "add_pose"]: pairMode ? {
      start_pose_id: document.querySelector("#direct-start-select").value,
      place_id: endpoint.workspace_id,
      x_mm: values[0], y_mm: values[1], yaw_deg: values[2],
    } : {place_id: currentView.draft.selection.workspace, x_mm: values[0], y_mm: values[1], yaw_deg: values[2]},
  });
});
document.querySelector("#direct-pose-list").addEventListener("click", (event) => {
  const pairButton = event.target.closest("[data-pair-index]");
  const pair = pairButton && currentView?.draft.direct_pairs?.[Number(pairButton.dataset.pairIndex)];
  if (pair && canIntent("update_draft")) return submitIntent("update_draft", {draft_id: currentView.draft.draft_id, remove_pair: pair});
  const button = event.target.closest("[data-pose-index]");
  const pose = button && currentView?.draft.direct_poses?.[Number(button.dataset.poseIndex)];
  if (pose && canIntent("update_draft")) submitIntent("update_draft", {draft_id: currentView.draft.draft_id, remove_pose: pose});
});
document.querySelector("#compile-campaign").addEventListener("click", () => submitIntent("compile_draft"));
document.querySelector("#collection-advice-refresh").addEventListener("click", () => submitIntent("refresh_collection_advice", {}));
for (const choice of ["apply", "keep"]) {
  document.querySelector(`#collection-advice-${choice}`).addEventListener("click", () => submitIntent("choose_collection_advice", {
    choice: choice.toUpperCase(), expected_recommendation_digest: currentView.collection_advice.recommendation_digest,
  }));
}
document.querySelector("#review-actions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-op]");
  if (button) submitIntent(button.dataset.op);
});
cancelButton.addEventListener("click", submitImmediateCancel);
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-recovery-op]");
  if (button) submitIntent(button.dataset.recoveryOp, {});
});
document.querySelector("#review-queue").addEventListener("click", (event) => {
  const button = event.target.closest("[data-review-choice]");
  if (!button) return;
  const choice = button.dataset.reviewChoice;
  const select = document.querySelector("#candidate-reason");
  const reason = choice === "PASS" ? null : select?.value || null;
  if (choice !== "PASS" && !reason) return select.reportValidity();
  submitIntent("review_candidate", {review_binding_digest: currentView.candidate_review.review_binding_digest, choice, reason});
});
document.querySelector("#same-settings-action").addEventListener("click", (event) => {
  const button = event.target.closest("[data-op]");
  if (button) submitIntent(button.dataset.op);
});
window.addEventListener("offline", () => failClose("BRIDGE_UNAVAILABLE", "BROWSER_OFFLINE", true));
window.addEventListener("online", () => {
  if (viewStale && recoveryAttempts < 3) {
    recoveryAttempts += 1;
    loadView({automaticRecovery: true});
  }
});

loadView();

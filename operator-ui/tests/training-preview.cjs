// Native PreparedApprovalBatch.preview, including MAPPED over a Curator DERIVED parent.
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const {test} = require("node:test");

async function preview(episodes) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      textContent: "", addEventListener() {},
      replaceChildren(...children) { this.textContent = children.map((row) => row.textContent).join("\n"); },
    });
    return elements.get(id);
  };
  const requests = [];
  vm.runInNewContext(readFileSync(join(__dirname, "../training.js"), "utf8"), {
    document: {querySelector: () => ({content: "fixture-token"}), getElementById: element,
      createElement: () => ({})},
    fetch: async (path) => {
      requests.push(path);
      return {ok: true, json: async () => ({projection: {
        kind: "TRAINING_REVIEW", status: "PREVIEW_NOT_APPROVED", operator_label: "fixture-human",
        available_ops: ["approve_training_batch", "refuse_training_batch"],
        preview: {dataset_identity: {dataset_id: "fixture"}, selected_count: episodes.length,
          batch_digest: "fixture-digest", episodes},
      }})};
    },
  });
  await new Promise(setImmediate);
  assert.deepEqual(requests, ["/api/view"]);
  assert.equal(element("training-approve").hidden, false);
  assert.equal(element("training-refuse").hidden, false);
  return {summary: element("training-count").textContent, rows: element("training-episodes").textContent};
}

const raw = {episode_index: 0, episode_id: "raw-0", technical_status: "PASS",
  semantic_status: "PASS", reviewer_id: "parent-reviewer"};
const derived = {...raw, episode_id: "derived-0", semantic_status: "NOT_ASSERTED",
  parent_semantic_status: "PASS", parent_dataset_identity: {
    dataset_id: "parent-r1", dataset_digest: "sha256:parent", dataset_root: "/private/parent"},
  curator_review: {clips: [{path: "/private/review.mp4"}], coverage: {
    covered_episodes: [0, 2], episodes: [0, 1, 2], unique_selected_frames: 30, population_frames: 90,
  }}};

test("raw preview retains native PASS and separate training approval", async () => {
  const {summary, rows} = await preview([raw]);
  assert.match(summary, /기술 검사 PASS · 내용 판정 PASS · 학습 사용 승인 별도/);
  assert.match(rows, /내용 PASS \(parent-reviewer\)/);
  assert.doesNotMatch(rows, /부모|Curator/);
});

test("derived preview distinguishes child status, parent review and batch coverage without paths", async () => {
  const {summary, rows} = await preview([derived]);
  assert.match(summary, /내용 판정 NOT_ASSERTED/);
  assert.doesNotMatch(summary, /내용 판정 PASS/);
  assert.match(rows, /내용 NOT_ASSERTED \(별도 판정 없음\)/);
  assert.match(rows, /부모 내용 PASS \(parent-reviewer\)/);
  assert.match(rows, /부모 데이터셋 parent-r1 \(sha256:parent\)/);
  assert.match(rows, /Curator 묶음 검토 범위: 에피소드 2\/3 · 프레임 30\/90/);
  assert.doesNotMatch(summary + rows, /\/private|dataset_root|review\.mp4/);
});

test("mixed and missing native statuses never become a blanket PASS", async () => {
  const mixed = await preview([raw, derived]);
  assert.match(mixed.summary, /내용 판정 PASS, NOT_ASSERTED/);
  const missing = await preview([{episode_id: "unknown", episode_index: 0}]);
  assert.doesNotMatch(missing.summary + missing.rows, /PASS|undefined/);
  assert.match(missing.summary, /확인되지 않음/);
});

const mapped = {...raw, episode_index: 1, episode_id: "mapped-1", semantic_status: "NOT_ASSERTED",
  parent_semantic_status: "PASS", parent_dataset_identity: derived.parent_dataset_identity,
  source_episode_index: 7, mapping: {manifest_digest: "sha256:mapping", path: "/private/mapping.json"}};

test("raw mapping scopes image preservation to this step and keeps original review", async () => {
  const {summary, rows} = await preview([mapped]);
  assert.match(summary, /내용 판정 NOT_ASSERTED/);
  assert.match(rows, /부모 내용 PASS \(parent-reviewer\)/);
  assert.match(rows, /매핑 부모 에피소드 7 → 1 · 이번 매핑 단계의 이미지 변환 없음/);
  assert.doesNotMatch(rows, /Curator|기록 원본|\/private|mapping\.json/);
});

test("mapped derivative separates recorded original review from processed parent and mapping", async () => {
  const nested = {...mapped, parent_semantic_status: "NOT_ASSERTED",
    parent_dataset_identity: {dataset_id: "processed-r1", dataset_digest: "sha256:processed", dataset_root: "/private/processed"},
    original_parent_semantic_status: "PASS", original_parent_dataset_identity: derived.parent_dataset_identity,
    original_source_episode_index: 9, curator_review: derived.curator_review};
  const {summary, rows} = await preview([nested]);
  assert.match(summary, /내용 판정 NOT_ASSERTED/);
  assert.match(rows, /Curator 가공 부모 내용 NOT_ASSERTED \(별도 판정 없음\)/);
  assert.match(rows, /Curator 가공 부모 데이터셋 processed-r1 \(sha256:processed\)/);
  assert.match(rows, /기록 원본 내용 PASS \(parent-reviewer\)/);
  assert.match(rows, /기록 원본 데이터셋 parent-r1 \(sha256:parent\)/);
  assert.match(rows, /기록 원본 에피소드 9 · 매핑 부모 에피소드 7 → 1/);
  assert.match(rows, /이번 매핑 단계의 이미지 변환 없음/);
  assert.match(rows, /Curator 묶음 검토 범위: 에피소드 2\/3 · 프레임 30\/90/);
  assert.doesNotMatch(summary + rows, /내용 판정 PASS|가공 부모 내용 PASS|NOT_ASSERTED \(parent-reviewer\)| · 이미지 변환 없음|\/private|dataset_root|review\.mp4|mapping\.json/);
});

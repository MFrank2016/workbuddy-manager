import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createPollGuard, classifyTerminal } from "../poll-guard.ts";

test("re-entry invalidates the previous generation", () => {
  const g = createPollGuard();
  const first = g.begin();
  const second = g.begin();
  assert.equal(g.isStale(first), true, "旧代必须失效");
  assert.equal(g.isStale(second), false, "最新代必须有效");
});

test("invalidate (stopPoll/close) makes every in-flight tick stale", () => {
  const g = createPollGuard();
  const g1 = g.begin();
  g.invalidate();
  assert.equal(g.isStale(g1), true);
});

test("consecutive re-entries leave exactly one active generation", () => {
  const g = createPollGuard();
  const generations = [g.begin(), g.begin(), g.begin()];
  assert.equal(generations.filter((x) => !g.isStale(x)).length, 1);
});

test("reopen after invalidate starts a fresh usable generation", () => {
  const g = createPollGuard();
  g.begin();
  g.invalidate();
  const fresh = g.begin();
  assert.equal(g.isStale(fresh), false);
});

test("markDone is one-way", () => {
  const g = createPollGuard();
  assert.equal(g.isDone(), false);
  g.markDone();
  assert.equal(g.isDone(), true);
});

test("terminal classification: first success announces, later ones are quiet/ignored", () => {
  assert.equal(classifyTerminal("success", false), "announce");
  assert.equal(classifyTerminal("success", true), "ignore");
  assert.equal(classifyTerminal("already_handled", false), "quiet");
  assert.equal(classifyTerminal("already_handled", true), "ignore");
  assert.equal(classifyTerminal("waiting", false), "ignore");
});

test("AddAccountDialog actually uses the guard", () => {
  const src = readFileSync(
    new URL(
      "../../components/common/accounts/AddAccountDialog.tsx",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(src, /createPollGuard/);
  assert.match(src, /classifyTerminal/);
});

test("accountApi.poll forwards the client id", () => {
  const src = readFileSync(
    new URL("../../lib/api.ts", import.meta.url),
    "utf8",
  );
  assert.match(src, /client:\s*client/);
});
